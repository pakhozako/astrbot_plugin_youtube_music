"""Allowlisted dependency updates with conservative platform and publisher verification.

Only yt-dlp, Deno and FFmpeg/ffprobe runtime binaries are managed. This module never
accepts a URL or destination path from the caller. yt-dlp updates verify the publisher-signed
checksum manifest; Deno/FFmpeg currently use fixed upstream release endpoints and SHA-256
asset digests/checksums where provided, not an independently verified publisher signature.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import aiohttp

from .maintenance import MaintenanceGate, run_blocking, uses_dependencies
from .process import ProcessError, ProcessTimeout, run_process


class DependencyUpdateError(RuntimeError):
    pass


class DependencyManager:
    USER_AGENT = "AstrBot-YouTube-Music-Dependency-Manager/0.3.0"
    MAX_CHECKSUM_BYTES = 1024 * 1024
    MAX_YTDLP_BYTES = 100 * 1024 * 1024
    MAX_DENO_ZIP_BYTES = 150 * 1024 * 1024
    MAX_FFMPEG_ZIP_BYTES = 700 * 1024 * 1024
    MAX_ARCHIVE_MEMBERS = 4096
    MAX_EXPANDED_BYTES = 1536 * 1024 * 1024
    REPOSITORIES = {"yt-dlp/yt-dlp", "denoland/deno", "BtbN/FFmpeg-Builds"}

    def __init__(
        self,
        data_dir: Path,
        settings: Any,
    ):
        self.data_dir = Path(data_dir)
        self.settings = settings
        self.on_update: Callable[[dict[str, Any]], None] | None = None
        self._update_lock = asyncio.Lock()
        self.dependency_gate = MaintenanceGate()
        self.state: dict[str, Any] = {
            "status": "idle",
            "phase": "空闲",
            "component": "",
            "progress": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed_bytes_per_second": None,
            "started_at": None,
            "finished_at": None,
            "message": "尚未下载依赖。",
        }

    @staticmethod
    def _clean_error(text: Any) -> str:
        value = " ".join(str(text or "").split())
        value = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", value)
        value = re.sub(
            r"(?i)\b(cookie|token|password|passwd|secret|authorization|proxy)=([^&\s]+)",
            r"\1=[REDACTED]",
            value,
        )
        value = re.sub(r"https?://\S+", "[URL REDACTED]", value)
        return value[:300]

    def _emit(self, **values: Any) -> None:
        if values.get("phase", self.state["phase"]) != self.state["phase"]:
            self.state.update(
                progress=None, downloaded_bytes=0, total_bytes=None, speed_bytes_per_second=None
            )
        self.state.update(values)
        if self.on_update:
            try:
                self.on_update(dict(self.state))
            except Exception:
                pass

    @staticmethod
    def _linux_libc() -> tuple[str, tuple[int, ...] | None]:
        """Return a conservative libc identity; unknown libc is not assumed compatible."""
        name, version = platform.libc_ver()
        folded = str(name or "").casefold()
        version_tuple = None
        match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", str(version or ""))
        if match:
            version_tuple = tuple(int(part or 0) for part in match.groups())
        if (
            "musl" in folded
            or any(Path("/lib").glob("ld-musl-*.so.1"))
            or any(Path("/lib64").glob("ld-musl-*.so.1"))
        ):
            return "musl", version_tuple
        if "glibc" in folded or "gnu libc" in folded:
            return "glibc", version_tuple
        try:
            libc_name, libc_version = os.confstr("CS_GNU_LIBC_VERSION").split(maxsplit=1)
            m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", libc_version)
            if libc_name.casefold() == "glibc" and m:
                return "glibc", tuple(int(part or 0) for part in m.groups())
        except (AttributeError, OSError, ValueError):
            pass
        # Do not run ldd (which may be a shell script) to guess libc on an unknown system.
        return "unknown", version_tuple

    @staticmethod
    def platform_profile() -> dict[str, str] | None:
        system = platform.system().casefold()
        arch = (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or platform.machine()
        ).casefold()
        if arch in {"amd64", "x86_64", "x64"}:
            arch = "x86_64"
        elif arch in {"aarch64", "arm64"}:
            arch = "aarch64"
        else:
            return None
        if system == "windows" and arch == "x86_64":
            return {
                "system": "windows",
                "arch": arch,
                "label": "Windows x64",
                "exe": ".exe",
                "libc": "windows",
            }
        if system == "linux" and arch in {"x86_64", "aarch64"}:
            libc_name, libc_version = DependencyManager._linux_libc()
            if libc_name != "glibc":
                return None
            # The bundled BtbN Linux assets target glibc 2.28+ (RHEL/CentOS 8 baseline).
            if libc_version is None or libc_version < (2, 28):
                return None
            return {
                "system": "linux",
                "arch": arch,
                "label": f"Linux {arch} glibc {'.'.join(map(str, libc_version))}",
                "exe": "",
                "libc": "glibc",
                "libc_version": ".".join(map(str, libc_version)),
            }
        return None

    @staticmethod
    def python3_compatibility() -> tuple[bool, str]:
        """Check the interpreter used by yt-dlp's /usr/bin/env python3 zipapp shebang."""
        executable = shutil.which("python3")
        if not executable:
            return (
                False,
                "Linux ARM64 的 yt-dlp 使用 Python zipapp，但 PATH 中未找到 python3；请安装 Python 3.11+ 并确认 PATH。",
            )
        try:
            proc = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, timeout=5, check=False
            )
            output = (proc.stdout or proc.stderr or "").strip()
            match = re.search(r"Python\s+(\d+)\.(\d+)(?:\.(\d+))?", output)
            if proc.returncode != 0 or not match:
                return (
                    False,
                    "无法读取 python3 版本；Linux ARM64 的 yt-dlp zipapp 要求 Python 3.11+。",
                )
            version = tuple(int(part or 0) for part in match.groups())
            if version < (3, 11, 0):
                return (
                    False,
                    f"检测到 {output}；yt-dlp 需要 PATH 中的 Python 3.11+。",
                )
            return True, f"检测到 {output}，满足 yt-dlp Python 3.11+ 要求。"
        except (OSError, subprocess.SubprocessError) as exc:
            return (
                False,
                f"无法检查 python3 版本（{type(exc).__name__}）；Linux ARM64 的 yt-dlp zipapp 要求 Python 3.11+。",
            )

    @staticmethod
    def supported_environment() -> tuple[bool, str]:
        system = platform.system().casefold()
        if system == "linux":
            libc_name, libc_version = DependencyManager._linux_libc()
            if libc_name == "musl":
                return (
                    False,
                    "musl/Alpine 不支持自动更新，需要 glibc 2.28+。",
                )
            if libc_name != "glibc" or libc_version is None:
                return (
                    False,
                    "无法可靠识别 Linux libc 及版本；为避免安装不兼容二进制，自动更新已拒绝。需要 glibc 2.28+。",
                )
            if libc_version < (2, 28):
                return (
                    False,
                    f"检测到 glibc {'.'.join(map(str, libc_version))}；当前 Linux 依赖资产要求 glibc 2.28+。",
                )
        profile = DependencyManager.platform_profile()
        if not profile:
            return False, (
                f"当前平台/架构暂不支持自动更新：{platform.system()} {platform.machine()}。"
                "支持 Windows x64、glibc 2.28+ Linux x86-64/ARM64。其他平台可在文档指定的数据目录安装依赖。"
            )
        if profile["system"] == "linux" and profile["arch"] == "aarch64":
            return DependencyManager.python3_compatibility()
        return True, profile["label"]

    def _proxy(self) -> str | None:
        return self.settings.dependency_proxy or None

    @classmethod
    def _validate_source(cls, url: str, *, redirect: bool = False) -> None:
        parsed = urlparse(url)
        try:
            valid = (
                parsed.scheme == "https"
                and parsed.port in {None, 443}
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and not any(ord(char) <= 32 for char in url)
            )
        except ValueError:
            valid = False
        source = False
        if parsed.hostname == "api.github.com":
            source = any(
                parsed.path == f"/repos/{repo}/releases/latest" for repo in cls.REPOSITORIES
            )
        elif parsed.hostname == "github.com":
            source = any(
                parsed.path.startswith(f"/{repo}/releases/download/") for repo in cls.REPOSITORIES
            )
        elif parsed.hostname == "www.gyan.dev":
            source = parsed.path == "/ffmpeg/builds/release-version" or bool(
                re.fullmatch(
                    r"/ffmpeg/builds/packages/ffmpeg-\d+\.\d+\.\d+-essentials_build\.zip(?:\.sha256)?",
                    parsed.path,
                )
            )
        elif redirect and parsed.hostname in {
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }:
            source = True
        if not valid or not source:
            raise DependencyUpdateError("下载源或重定向不在允许范围。")

    @asynccontextmanager
    async def _response(self, session, url, timeout):
        self._validate_source(url)
        for hop in range(6):
            async with session.get(
                url, proxy=self._proxy(), timeout=timeout, allow_redirects=False
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if hop == 5 or not location:
                        raise DependencyUpdateError("下载重定向过多或缺少目标。")
                    url = urljoin(url, location)
                    self._validate_source(url, redirect=True)
                    continue
                if response.status != 200:
                    raise DependencyUpdateError(f"下载源返回 HTTP {response.status}。")
                if response.headers.get("Content-Encoding", "identity").casefold() != "identity":
                    raise DependencyUpdateError("下载源返回非预期的内容编码。")
                yield response
                return

    @staticmethod
    def _content_length(response, max_bytes: int) -> int | None:
        raw = response.headers.get("Content-Length")
        if raw is None:
            return None
        if not re.fullmatch(r"[0-9]{1,12}", raw) or not 0 < int(raw) <= max_bytes:
            raise DependencyUpdateError("下载内容长度无效或超过上限。")
        return int(raw)

    async def _get(self, session: Any, url: str, *, max_bytes: int, label: str) -> bytes:
        self._emit(
            phase=f"请求：{label}",
            progress=0.0,
            downloaded_bytes=0,
            total_bytes=None,
            message=f"正在连接 {label}…",
        )
        timeout = aiohttp.ClientTimeout(total=180, connect=25, sock_read=45)
        async with self._response(session, url, timeout) as response:
            length = self._content_length(response, max_bytes)
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise DependencyUpdateError(f"{label} 实际内容超过允许大小。")
            if not data or length is not None and len(data) != length:
                raise DependencyUpdateError(f"{label} 内容为空或长度不符。")
            return bytes(data)

    async def _download(
        self, session: Any, url: str, path: Path, *, max_bytes: int, label: str
    ) -> dict[str, Any]:
        await run_blocking(path.parent.mkdir, parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=7200, connect=30, sock_read=90)
        started = time.monotonic()
        downloaded = 0
        digest = hashlib.sha256()
        last_emit = started
        self._emit(
            phase=f"下载：{label}",
            progress=0.0,
            downloaded_bytes=0,
            total_bytes=None,
            speed_bytes_per_second=0.0,
            message=f"正在下载 {label}…",
        )
        try:
            async with self._response(session, url, timeout) as response:
                total = self._content_length(response, max_bytes)
                with path.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        downloaded += len(chunk)
                        if downloaded > max_bytes or total is not None and downloaded > total:
                            raise DependencyUpdateError(f"{label} 下载过程中超出大小上限。")
                        await run_blocking(stream.write, chunk)
                        digest.update(chunk)
                        elapsed = max(0.1, time.monotonic() - started)
                        speed = downloaded / elapsed
                        progress = min(100.0, downloaded * 100 / total) if total else None
                        now = time.monotonic()
                        if now - last_emit >= 0.2:
                            last_emit = now
                            self._emit(
                                progress=progress,
                                downloaded_bytes=downloaded,
                                total_bytes=total,
                                speed_bytes_per_second=speed,
                            )
                    await run_blocking(stream.flush)
                    await run_blocking(os.fsync, stream.fileno())
            if downloaded <= 0 or total is not None and downloaded != total:
                raise DependencyUpdateError(f"{label} 下载为空或长度不符。")
            self._emit(
                progress=100 if total else None,
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed_bytes_per_second=downloaded / max(0.1, time.monotonic() - started),
            )
            return {
                "path": path,
                "sha256": digest.hexdigest(),
                "size_bytes": downloaded,
                "url": url,
            }
        except asyncio.CancelledError:
            try:
                await run_blocking(path.unlink, missing_ok=True)
            except OSError:
                pass
            raise
        except Exception:
            try:
                await run_blocking(path.unlink, missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _extract_hash(text: str, file_name: str | None = None) -> str:
        if file_name is None and re.fullmatch(r"\s*[0-9a-fA-F]{64}\s*", text):
            return text.strip().lower()
        matches = []
        for line in text.splitlines():
            match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
            if match and match.group(2) == file_name:
                matches.append(match.group(1).lower())
        if len(matches) != 1:
            raise DependencyUpdateError("校验文件必须包含唯一且对应当前资产的 SHA-256。")
        return matches[0]

    @classmethod
    def _deno_hash(cls, text: str, asset_name: str, system: str) -> str:
        if system != "windows":
            return cls._extract_hash(text, asset_name)
        # Current Windows releases serialize Get-FileHash as a PowerShell field list.
        fields = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"(Algorithm|Hash|Path)\s*:\s*(.+)", line)
            if not match or match.group(1) in fields:
                raise DependencyUpdateError("Deno Windows 校验字段无效或重复。")
            fields[match.group(1)] = match.group(2)
        if (
            fields.keys() != {"Algorithm", "Hash", "Path"}
            or fields["Algorithm"] != "SHA256"
            or not re.fullmatch(r"[0-9a-fA-F]{64}", fields["Hash"])
            or PureWindowsPath(fields["Path"]).name != asset_name
        ):
            raise DependencyUpdateError("Deno Windows 校验算法、哈希或资产名不匹配。")
        return fields["Hash"].lower()

    @staticmethod
    def _verify_yt_dlp_manifest_signature(
        signature: bytes, manifest: bytes, *, gpg_executable: str | None = None
    ) -> None:
        """Verify yt-dlp's signed checksum manifest with a bundled, fingerprint-pinned key.

        If GnuPG is unavailable, verification fails closed rather than silently falling
        back to an unsigned checksum list.
        """
        expected_fingerprint = "AC0CBBE6848D6A873464AF4E57CF65933B5A7581"
        gpg = gpg_executable or shutil.which("gpg") or shutil.which("gpg.exe")
        if not gpg:
            raise DependencyUpdateError(
                "无法验证 yt-dlp 发布签名：当前系统没有 GnuPG（gpg）。为安全起见已停止更新，安装 GnuPG 后重试。"
            )
        key_path = Path(__file__).resolve().parent / "vendor" / "yt-dlp-public.key"
        if not key_path.is_file():
            raise DependencyUpdateError("插件包缺少固定的 yt-dlp 签名公钥，已拒绝更新。")
        with tempfile.TemporaryDirectory(prefix="yt-dlp-gpg-") as home:
            os.chmod(home, 0o700)
            homedir = Path(home)
            (homedir / "publisher.key").write_bytes(key_path.read_bytes())
            (homedir / "manifest.sig").write_bytes(signature)
            (homedir / "SHA2-256SUMS").write_bytes(manifest)
            # Relative paths also work with MSYS GPG. Dearmoring avoids agent
            # startup and its socket-path limits in deeply nested workspaces.
            command = [gpg, "--homedir", ".", "--no-options", "--batch", "--no-autostart"]
            run_options = {
                "cwd": homedir,
                "capture_output": True,
                "text": True,
                "timeout": 20,
                "check": False,
            }
            if os.name == "nt":
                run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            keyring = ["--no-default-keyring", "--keyring", "./publisher.gpg"]
            try:
                prepared = subprocess.run(
                    command + ["--output", "./publisher.gpg", "--dearmor", "./publisher.key"],
                    **run_options,
                )
                if prepared.returncode != 0:
                    raise DependencyUpdateError("无法准备内置 yt-dlp 签名公钥，已拒绝更新。")
                listing = subprocess.run(
                    command + keyring + ["--with-colons", "--fingerprint"], **run_options
                )
                primary_fingerprints = []
                primary_pending = False
                for line in (listing.stdout or "").splitlines():
                    fields = line.split(":")
                    if fields[0] in {"pub", "sub", "sec", "ssb"}:
                        primary_pending = fields[0] == "pub"
                    elif fields[0] == "fpr" and primary_pending and len(fields) > 9:
                        primary_fingerprints.append(fields[9].upper())
                        primary_pending = False
                if listing.returncode != 0 or primary_fingerprints != [expected_fingerprint]:
                    raise DependencyUpdateError(
                        "yt-dlp 签名公钥指纹与内置受信任指纹不一致，已拒绝更新。"
                    )
                verified = subprocess.run(
                    command
                    + keyring
                    + [
                        "--no-auto-key-retrieve",
                        "--no-auto-key-import",
                        "--status-fd",
                        "1",
                        "--verify",
                        "./manifest.sig",
                        "./SHA2-256SUMS",
                    ],
                    **run_options,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise DependencyUpdateError(
                    "GnuPG 执行失败或超时，无法验证 yt-dlp 发布签名，已拒绝更新。"
                ) from exc
            valid_fingerprints = []
            rejected_signature = False
            for line in (verified.stdout or "").splitlines():
                parts = line.split()
                if len(parts) < 2 or parts[0] != "[GNUPG:]":
                    continue
                if parts[1] in {"BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG"}:
                    rejected_signature = True
                elif parts[1] == "VALIDSIG" and len(parts) >= 11:
                    # A signing subkey is accepted only under the pinned primary key.
                    valid_fingerprints.append(
                        parts[11].upper() if len(parts) > 11 else parts[2].upper()
                    )
            if (
                verified.returncode != 0
                or rejected_signature
                or not valid_fingerprints
                or any(fingerprint != expected_fingerprint for fingerprint in valid_fingerprints)
            ):
                raise DependencyUpdateError(
                    "yt-dlp SHA2-256SUMS 的签名验证失败或签名者不受信任，已拒绝更新。"
                )

    @classmethod
    def _safe_zip_members(cls, archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        valid: list[zipfile.ZipInfo] = []
        total = 0
        names = set()
        for member in archive.infolist():
            raw = member.filename.replace("\\", "/")
            parsed = PurePosixPath(raw)
            mode = stat.S_IFMT(member.external_attr >> 16)
            total += member.file_size
            name = parsed.as_posix().casefold()
            if (
                parsed.is_absolute()
                or ".." in parsed.parts
                or ":" in raw
                or len(raw) > 1024
                or "\x00" in raw
                or name in names
                or member.flag_bits & 1
                or mode not in {0, stat.S_IFREG, stat.S_IFDIR}
                or len(valid) >= cls.MAX_ARCHIVE_MEMBERS
                or total > cls.MAX_EXPANDED_BYTES
            ):
                raise DependencyUpdateError("ZIP 条目路径、类型、数量或展开大小不安全。")
            names.add(name)
            valid.append(member)
        return valid

    @uses_dependencies
    async def inspect(self) -> dict[str, Any]:
        """Check managed executable presence and versions without making network requests."""
        checks = []
        for label, path, args in (
            (
                "yt-dlp",
                Path(self.settings.yt_dlp_path),
                ["--ignore-config", "--no-plugin-dirs", "--version"],
            ),
            ("Deno", Path(self.settings.deno_path), ["--version"]),
            ("FFmpeg", Path(self.settings.ffmpeg_path), ["-version"]),
            ("ffprobe", Path(self.settings.ffprobe_path), ["-version"]),
        ):
            relative = None
            try:
                relative = path.resolve().relative_to(self.data_dir.resolve()).as_posix()
            except (OSError, ValueError):
                # Never expose an absolute path; report path policy failure instead.
                checks.append(
                    {
                        "name": label,
                        "status": "invalid_path",
                        "version": "",
                        "path": "outside_plugin_data",
                    }
                )
                continue
            if not path.is_file():
                checks.append({"name": label, "status": "missing", "version": "", "path": relative})
                continue
            try:
                result = await run_process(
                    [str(path), *args], timeout=12, label=f"{label} 版本检查"
                )
                output = result.stdout or result.stderr
                version = self._parse_version(label, output)
                checks.append({"name": label, "status": "ok", "version": version, "path": relative})
            except ProcessTimeout:
                checks.append({"name": label, "status": "timeout", "version": "", "path": relative})
            except (OSError, ValueError, ProcessError, DependencyUpdateError) as exc:
                checks.append(
                    {
                        "name": label,
                        "status": "error",
                        "version": type(exc).__name__,
                        "path": relative,
                    }
                )
        supported, reason = await run_blocking(self.supported_environment)
        return {
            "platform": sys.platform,
            "architecture": platform.machine(),
            "updater_supported": supported,
            "updater_reason": reason,
            "dependencies": checks,
            "all_ready": bool(checks) and all(row["status"] == "ok" for row in checks),
        }

    async def update(self, component: str) -> dict[str, Any]:
        """Block new binary users, drain existing users, then update exclusively."""
        if self._update_lock.locked():
            raise DependencyUpdateError("已有依赖更新任务。")
        async with self._update_lock:
            self._emit(
                status="waiting",
                phase="等待维护权限",
                component=component,
                progress=None,
                started_at=time.time(),
                finished_at=None,
                updated={},
                error="",
                message="等待现有任务完成。",
            )
            try:
                async with asyncio.timeout(self.settings.dependency_update_timeout_seconds):
                    async with self.dependency_gate.maintenance(
                        timeout=self.settings.queue_wait_timeout_seconds
                    ):
                        return await self._update_unlocked(component)
            except asyncio.CancelledError:
                if self.state["status"] != "cancelled":
                    self._emit(
                        status="cancelled",
                        phase="已取消",
                        finished_at=time.time(),
                        message="依赖更新已取消。",
                    )
                raise
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    message = "依赖更新或维护等待超时。"
                    if self.state.get("recovery_required"):
                        message += " 回滚未完成，备份已保留。"
                    self._emit(
                        status="failed",
                        phase="超时",
                        finished_at=time.time(),
                        error="TimeoutError",
                        message=message,
                    )
                elif self.state["status"] != "failed":
                    self._emit(
                        status="failed",
                        phase="失败",
                        finished_at=time.time(),
                        error=type(exc).__name__,
                        message=self._clean_error(exc),
                    )
                raise DependencyUpdateError(self.state["message"]) from exc

    @staticmethod
    def _parse_version(label: str, output: str) -> str:
        patterns = {
            "yt-dlp": r"^(\d{4}\.\d{2}\.\d{2})$",
            "Deno": r"^deno (\d+\.\d+\.\d+)(?:\s|$)",
            "FFmpeg": r"^ffmpeg version n?(\d+\.\d+(?:\.\d+)?)(?:[-+\s]|$)",
            "ffprobe": r"^ffprobe version n?(\d+\.\d+(?:\.\d+)?)(?:[-+\s]|$)",
        }
        first = output.splitlines()[0] if output else ""
        match = re.match(patterns[label], first)
        if not match:
            raise DependencyUpdateError(f"{label} 版本输出无效。")
        return match.group(1)

    async def _verify_binary(self, target: Path, executable: Path) -> str:
        labels = {
            Path(self.settings.yt_dlp_path): "yt-dlp",
            Path(self.settings.deno_path): "Deno",
            Path(self.settings.ffmpeg_path): "FFmpeg",
            Path(self.settings.ffprobe_path): "ffprobe",
        }
        label = labels[target]
        args = (
            ["--ignore-config", "--no-plugin-dirs", "--version"]
            if label == "yt-dlp"
            else ["--version"]
            if label == "Deno"
            else ["-version"]
        )
        try:
            result = await run_process([str(executable), *args], timeout=20, label=f"{label} 验证")
            return self._parse_version(label, result.stdout or result.stderr)
        except (ProcessError, OSError, ValueError) as exc:
            raise DependencyUpdateError(f"{label} 未通过可执行文件验证。") from exc

    async def _update_unlocked(self, component: str) -> dict[str, Any]:
        supported, reason = await run_blocking(self.supported_environment)
        if not supported:
            raise DependencyUpdateError(reason)
        if component not in {"yt-dlp", "Deno", "FFmpeg", "all"}:
            raise DependencyUpdateError("依赖名称不受支持。")
        managed_root = await run_blocking(self._prepare_update, component)
        task_dir = self.data_dir / "dependency-staging" / uuid.uuid4().hex
        try:
            task_dir.resolve().relative_to(managed_root)
        except ValueError:
            raise DependencyUpdateError("依赖暂存目录不能通过符号链接逃逸。") from None
        self._emit(
            status="running",
            component=component,
            phase="准备下载",
            progress=0.0,
            downloaded_bytes=0,
            total_bytes=None,
            finished_at=None,
            error="",
            updated={},
            recovery_required=False,
            recovery_directory="",
            recovery_targets=[],
            message="开始依赖更新。",
        )
        targets: dict[Path, Path] = {}
        backups: dict[Path, Path] = {}
        committed: list[Path] = []
        retain_recovery = False
        created = False

        def create_staging():
            nonlocal created
            task_dir.mkdir(parents=True, exist_ok=False)
            created = True

        try:
            await run_blocking(create_staging)
            async with aiohttp.ClientSession(
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "application/vnd.github+json",
                    "Accept-Encoding": "identity",
                },
                auto_decompress=False,
            ) as session:
                requested = ["yt-dlp", "Deno", "FFmpeg"] if component == "all" else [component]
                for name in requested:
                    self._emit(
                        phase=f"读取 {name} 官方版本",
                        progress=0.0,
                        message=f"正在核对 {name} 官方稳定版本…",
                    )
                    if name == "yt-dlp":
                        await self._stage_ytdlp(session, task_dir, targets)
                    elif name == "Deno":
                        await self._stage_deno(session, task_dir, targets)
                    elif name == "FFmpeg":
                        await self._stage_ffmpeg(session, task_dir, targets)
            self._emit(phase="验证暂存程序", message="校验完成，验证暂存程序。")
            versions = {}
            for target, staged in targets.items():
                if staged.is_symlink() or not staged.is_file() or staged.stat().st_size < 64 * 1024:
                    raise DependencyUpdateError(f"暂存文件无效：{target.name}")
                staged.resolve().relative_to(task_dir.resolve())
                versions[target.name] = await self._verify_binary(target, staged)
            if Path(self.settings.ffmpeg_path) in targets:
                if (
                    versions[Path(self.settings.ffmpeg_path).name]
                    != versions[Path(self.settings.ffprobe_path).name]
                ):
                    raise DependencyUpdateError("FFmpeg 与 ffprobe 版本不一致。")
            self._emit(
                phase="备份并安装",
                progress=100.0,
                message="下载与校验完成，正在备份并原子替换可执行文件…",
            )
            await run_blocking(self._install_targets, targets, task_dir, backups, committed)
            self._emit(phase="验证安装", message="检查安装后的版本。")
            for target in targets:
                if await self._verify_binary(target, target) != versions[target.name]:
                    raise DependencyUpdateError(f"{target.name} 安装前后版本不一致。")
            self._emit(
                status="success",
                phase="完成",
                progress=100.0,
                finished_at=time.time(),
                message="依赖已完成校验、替换和版本检查。",
                updated=versions,
            )
            return {
                "ok": True,
                "component": component,
                "updated": versions,
                "message": "依赖已完成校验、替换和版本检查。",
            }
        except (Exception, asyncio.CancelledError) as exc:
            # Keep recovery metadata until the cancellation-safe restore has finished.
            retain_recovery = True
            cancelled = isinstance(exc, asyncio.CancelledError)
            self._emit(
                phase="回滚",
                recovery_required=True,
                recovery_directory=task_dir.relative_to(self.data_dir).as_posix(),
                recovery_targets=[target.name for target in committed],
                message="正在恢复原始依赖。",
            )
            failures = []

            def restore():
                failures.extend(self._rollback(targets, backups, committed))

            try:
                await run_blocking(restore)
            except asyncio.CancelledError:
                cancelled = True
            retain_recovery = bool(failures)
            message = "依赖更新已取消。" if cancelled else self._clean_error(exc)
            if failures:
                message += " 回滚未完成，原始备份已保留，请先恢复依赖。"
            self._emit(
                status="cancelled" if cancelled else "failed",
                phase="已取消" if cancelled else "失败",
                finished_at=time.time(),
                error=type(exc).__name__,
                recovery_required=retain_recovery,
                recovery_directory=task_dir.relative_to(self.data_dir).as_posix()
                if failures
                else "",
                recovery_targets=[target.name for target in failures],
                message=message,
            )
            if cancelled:
                raise asyncio.CancelledError from None
            raise DependencyUpdateError(message) from exc
        finally:
            if created and not retain_recovery:
                task_dir.resolve().relative_to(managed_root)
                await run_blocking(shutil.rmtree, task_dir, ignore_errors=True)

    def _prepare_update(self, component: str) -> Path:
        managed_root = self.data_dir.resolve()
        staging_root = self.data_dir / "dependency-staging"
        try:
            staging_root.resolve().relative_to(managed_root)
        except ValueError:
            raise DependencyUpdateError("依赖暂存目录不能通过符号链接逃逸。") from None
        if self.state.get("recovery_required"):
            raise DependencyUpdateError("存在未完成的依赖回滚，请先恢复原始备份后重试。")
        if staging_root.is_dir():
            for directory in staging_root.iterdir():
                if directory.is_symlink():
                    raise DependencyUpdateError("依赖暂存目录含符号链接，已拒绝更新。")
                if directory.is_dir() and any(directory.glob("backup-*")):
                    raise DependencyUpdateError(
                        "依赖暂存目录保留了恢复备份，请先恢复并清理该目录后重试。"
                    )
        managed_targets = {
            "yt-dlp": [self.settings.yt_dlp_path],
            "Deno": [self.settings.deno_path],
            "FFmpeg": [self.settings.ffmpeg_path, self.settings.ffprobe_path],
            "all": [
                self.settings.yt_dlp_path,
                self.settings.deno_path,
                self.settings.ffmpeg_path,
                self.settings.ffprobe_path,
            ],
        }[component]
        for target in managed_targets:
            try:
                Path(target).resolve().relative_to(managed_root)
            except (OSError, ValueError):
                raise DependencyUpdateError(
                    "拒绝更新：可执行文件路径必须位于插件数据目录内，且不能通过符号链接逃逸。"
                ) from None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Check staging and target volumes before any network transfer. The estimates
        # include the downloaded archive, staged binaries, a rollback copy and atomic
        # replacement files; the actual required amount depends on the selected build.
        staging_need = {
            "yt-dlp": 256 * 1024 * 1024,
            "Deno": 512 * 1024 * 1024,
            "FFmpeg": 1536 * 1024 * 1024,
            "all": 2304 * 1024 * 1024,
        }[component]

        def existing_anchor(path: Path) -> Path:
            cursor = path
            while not cursor.exists() and cursor != cursor.parent:
                cursor = cursor.parent
            return cursor

        staging_anchor = existing_anchor(self.data_dir)
        if shutil.disk_usage(staging_anchor).free < staging_need:
            raise DependencyUpdateError(
                f"依赖暂存磁盘空间不足；所选更新至少预留 {staging_need // (1024 * 1024)} MiB 后再试。"
            )
        # Existing target volumes must also have room for a staged replacement and backup.
        target_paths = []
        if component in {"yt-dlp", "all"}:
            target_paths.append(self.settings.yt_dlp_path)
        if component in {"Deno", "all"}:
            target_paths.append(self.settings.deno_path)
        if component in {"FFmpeg", "all"}:
            target_paths.extend([self.settings.ffmpeg_path, self.settings.ffprobe_path])
        for target in target_paths:
            target = Path(target)
            anchor = existing_anchor(target.parent)
            free = shutil.disk_usage(anchor).free
            existing_size = target.stat().st_size if target.is_file() else 0
            target_need = max(128 * 1024 * 1024, existing_size * 2 + 64 * 1024 * 1024)
            if free < target_need:
                raise DependencyUpdateError(
                    f"依赖目标磁盘空间不足：{target.name} 至少需要 {target_need // (1024 * 1024)} MiB 可用空间。"
                )
        return managed_root

    @staticmethod
    def _atomic_copy(
        source: Path,
        target: Path,
        *,
        executable: bool = False,
        committed: list[Path] | None = None,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".new", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            shutil.copystat(source, temporary)
            if executable and os.name != "nt":
                temporary.chmod(temporary.stat().st_mode | 0o111)
            os.replace(temporary, target)
            if committed is not None:
                committed.append(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _install_targets(
        self,
        targets: dict[Path, Path],
        task_dir: Path,
        backups: dict[Path, Path],
        committed: list[Path],
    ) -> None:
        if not targets:
            raise DependencyUpdateError("更新没有生成可安装的暂存文件。")
        for target, staged in targets.items():
            if staged.is_symlink() or not staged.is_file() or staged.stat().st_size < 64 * 1024:
                raise DependencyUpdateError(f"暂存文件无效：{target.name}")
            staged.resolve().relative_to(task_dir.resolve())
        for index, (target, staged) in enumerate(targets.items(), start=1):
            target.resolve().relative_to(self.data_dir.resolve())
            if target.is_symlink():
                raise DependencyUpdateError("拒绝覆盖符号链接形式的依赖文件。")
            if target.is_file():
                backup = task_dir / f"backup-{index}-{target.name}"
                shutil.copy2(target, backup)
                backups[target] = backup
            self._atomic_copy(staged, target, executable=True, committed=committed)

    @classmethod
    def _rollback(
        cls, targets: dict[Path, Path], backups: dict[Path, Path], committed: list[Path]
    ) -> list[Path]:
        failures = []
        for target in reversed(committed):
            backup = backups.get(target)
            try:
                if backup:
                    if not backup.is_file() or backup.is_symlink():
                        raise OSError("Recovery backup is unavailable")
                    cls._atomic_copy(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                failures.append(target)
        return failures

    async def _github_release(self, session: Any, repository: str) -> dict[str, Any]:
        if repository not in self.REPOSITORIES:
            raise DependencyUpdateError("发布仓库不在允许范围。")
        raw = await self._get(
            session,
            f"https://api.github.com/repos/{repository}/releases/latest",
            max_bytes=3 * 1024 * 1024,
            label=f"{repository} 最新稳定发布信息",
        )
        try:
            release = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DependencyUpdateError(f"无法解析 {repository} 官方发布信息。") from exc
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise DependencyUpdateError(f"{repository} 未返回稳定版发布信息。")
        return release

    @staticmethod
    def _asset(release: dict[str, Any], name: str, repository: str) -> str:
        tag = release.get("tag_name")
        if repository not in DependencyManager.REPOSITORIES or not isinstance(tag, str) or not tag:
            raise DependencyUpdateError("发布仓库或版本标签无效。")
        expected = f"https://github.com/{repository}/releases/download/{quote(tag, safe='')}/{quote(name, safe='')}"
        for asset in release.get("assets", []) if isinstance(release.get("assets"), list) else []:
            if (
                isinstance(asset, dict)
                and asset.get("name") == name
                and isinstance(asset.get("browser_download_url"), str)
            ):
                url = asset["browser_download_url"]
                if url == expected:
                    return url
        raise DependencyUpdateError(f"官方发布中缺少允许的资产：{name}")

    async def _stage_ytdlp(self, session: Any, task_dir: Path, targets: dict[Path, Path]) -> None:
        profile = self.platform_profile()
        if not profile:
            raise DependencyUpdateError("当前平台没有经过验证的 yt-dlp 二进制来源。")
        release = await self._github_release(session, "yt-dlp/yt-dlp")
        if profile["system"] == "windows":
            asset_name = "yt-dlp.exe"
        elif profile["arch"] == "x86_64":
            asset_name = "yt-dlp_linux"
        else:
            # Upstream publishes the generic Python zipapp `yt-dlp` rather than a
            # dedicated Linux ARM64 PyInstaller binary. It is still placed under the
            # managed plugin-data path and marked executable; AstrBot's Python host
            # supplies the system interpreter via its shebang.
            asset_name = "yt-dlp"
        checksum_url = self._asset(release, "SHA2-256SUMS", "yt-dlp/yt-dlp")
        signature_url = self._asset(release, "SHA2-256SUMS.sig", "yt-dlp/yt-dlp")
        binary_url = self._asset(release, asset_name, "yt-dlp/yt-dlp")
        checksum = await self._get(
            session, checksum_url, max_bytes=self.MAX_CHECKSUM_BYTES, label="yt-dlp SHA2-256SUMS"
        )
        signature = await self._get(
            session, signature_url, max_bytes=128 * 1024, label="yt-dlp SHA2-256SUMS 签名"
        )
        await run_blocking(self._verify_yt_dlp_manifest_signature, signature, checksum)
        expected = self._extract_hash(checksum.decode("utf-8", errors="replace"), asset_name)
        downloaded = await self._download(
            session,
            binary_url,
            task_dir / asset_name,
            max_bytes=self.MAX_YTDLP_BYTES,
            label=f"yt-dlp {release.get('tag_name', '')}",
        )
        if downloaded["sha256"] != expected:
            raise DependencyUpdateError(f"{asset_name} 的 SHA-256 与同一官方发布校验清单不一致。")
        target = Path(self.settings.yt_dlp_path)
        staged = task_dir / f"stage-{target.name}"
        await run_blocking(self._copy_stage_binary, downloaded["path"], staged)
        targets[target] = staged

    async def _stage_deno(self, session: Any, task_dir: Path, targets: dict[Path, Path]) -> None:
        profile = self.platform_profile()
        if not profile:
            raise DependencyUpdateError("当前平台没有经过验证的 Deno 二进制来源。")
        release = await self._github_release(session, "denoland/deno")
        if profile["system"] == "windows":
            zip_name = "deno-x86_64-pc-windows-msvc.zip"
            binary_name = "deno.exe"
        elif profile["system"] == "linux" and profile["arch"] == "x86_64":
            zip_name = "deno-x86_64-unknown-linux-gnu.zip"
            binary_name = "deno"
        else:
            zip_name = "deno-aarch64-unknown-linux-gnu.zip"
            binary_name = "deno"
        zip_url = self._asset(release, zip_name, "denoland/deno")
        checksum_url = self._asset(release, zip_name + ".sha256sum", "denoland/deno")
        checksum = await self._get(
            session, checksum_url, max_bytes=self.MAX_CHECKSUM_BYTES, label="Deno SHA-256"
        )
        text = checksum.decode("utf-8")
        expected = self._deno_hash(text, zip_name, profile["system"])
        archive = await self._download(
            session,
            zip_url,
            task_dir / zip_name,
            max_bytes=self.MAX_DENO_ZIP_BYTES,
            label=f"Deno {release.get('tag_name', '')}",
        )
        if archive["sha256"] != expected:
            raise DependencyUpdateError("Deno ZIP 的 SHA-256 与官方发布校验文件不一致。")
        staged = task_dir / f"stage-{Path(self.settings.deno_path).name}"
        await run_blocking(
            self._stage_archive_binary, archive["path"], binary_name, staged, require_bin_dir=False
        )
        targets[Path(self.settings.deno_path)] = staged

    async def _stage_ffmpeg(self, session: Any, task_dir: Path, targets: dict[Path, Path]) -> None:
        profile = self.platform_profile()
        if not profile:
            raise DependencyUpdateError("当前平台没有经过验证的 FFmpeg 二进制来源。")
        if profile["system"] == "windows":
            version = (
                (
                    await self._get(
                        session,
                        "https://www.gyan.dev/ffmpeg/builds/release-version",
                        max_bytes=128,
                        label="FFmpeg 稳定版本号",
                    )
                )
                .decode("utf-8", errors="replace")
                .replace("\x00", "")
                .strip()
            )
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise DependencyUpdateError("FFmpeg 官方构建提供方返回的稳定版本号格式无效。")
            base = (
                f"https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{version}-essentials_build.zip"
            )
            raw_hash = await self._get(
                session, base + ".sha256", max_bytes=1024, label="FFmpeg SHA-256"
            )
            expected = self._extract_hash(raw_hash.decode("utf-8", errors="replace"))
            archive = await self._download(
                session,
                base,
                task_dir / "ffmpeg-release-essentials.zip",
                max_bytes=self.MAX_FFMPEG_ZIP_BYTES,
                label=f"FFmpeg {version} essentials ZIP",
            )
            if archive["sha256"] != expected:
                raise DependencyUpdateError("FFmpeg ZIP 的 SHA-256 与构建方校验文件不一致。")
            archive_path = archive["path"]
            suffix = ".exe"
        else:
            release = await self._github_release(session, "BtbN/FFmpeg-Builds")
            arch_label = "linux64" if profile["arch"] == "x86_64" else "linuxarm64"
            # Choose a versioned, static GPL release (not the moving master build).
            available = [
                a.get("name", "") for a in release.get("assets", []) if isinstance(a, dict)
            ]
            pattern = re.compile(rf"^ffmpeg-n(\d+\.\d+)-latest-{arch_label}-gpl-\1\.tar\.xz$")
            candidates = [name for name in available if pattern.match(name)]
            if not candidates:
                raise DependencyUpdateError(
                    f"BtbN 官方稳定资产中没有找到 {arch_label} 版本化静态 FFmpeg 构建。"
                )

            def version_key(name: str) -> tuple[int, int]:
                match = re.match(r"ffmpeg-n(\d+)\.(\d+)-", name)
                return (int(match.group(1)), int(match.group(2))) if match else (0, 0)

            asset_name = sorted(candidates, key=version_key, reverse=True)[0]
            checksums = await self._get(
                session,
                self._asset(release, "checksums.sha256", "BtbN/FFmpeg-Builds"),
                max_bytes=self.MAX_CHECKSUM_BYTES,
                label="BtbN FFmpeg SHA256 清单",
            )
            expected = self._extract_hash(checksums.decode("utf-8", errors="replace"), asset_name)
            downloaded = await self._download(
                session,
                self._asset(release, asset_name, "BtbN/FFmpeg-Builds"),
                task_dir / asset_name,
                max_bytes=self.MAX_FFMPEG_ZIP_BYTES,
                label=f"FFmpeg {asset_name}",
            )
            if downloaded["sha256"] != expected:
                raise DependencyUpdateError("FFmpeg TAR.XZ 的 SHA-256 与官方发布清单不一致。")
            archive_path = downloaded["path"]
            suffix = ""
        staged_ffmpeg = task_dir / f"stage-ffmpeg{suffix}"
        staged_ffprobe = task_dir / f"stage-ffprobe{suffix}"
        await run_blocking(
            self._stage_archive_binary,
            archive_path,
            f"ffmpeg{suffix}",
            staged_ffmpeg,
            tar_archive=not suffix,
        )
        await run_blocking(
            self._stage_archive_binary,
            archive_path,
            f"ffprobe{suffix}",
            staged_ffprobe,
            tar_archive=not suffix,
        )
        targets[Path(self.settings.ffmpeg_path)] = staged_ffmpeg
        targets[Path(self.settings.ffprobe_path)] = staged_ffprobe

    @staticmethod
    def _copy_stage_binary(source: Path, staged: Path) -> None:
        shutil.copyfile(source, staged)
        if os.name != "nt":
            staged.chmod(0o755)

    @classmethod
    def _stage_archive_binary(
        cls,
        archive: Path,
        basename: str,
        staged: Path,
        *,
        require_bin_dir: bool = True,
        tar_archive: bool = False,
    ) -> None:
        binary = (
            cls._read_tar_binary(archive, basename)
            if tar_archive
            else cls._read_zip_binary(archive, basename, require_bin_dir=require_bin_dir)
        )
        staged.write_bytes(binary)
        if os.name != "nt":
            staged.chmod(0o755)

    @classmethod
    def _read_tar_binary(cls, path: Path, basename: str) -> bytes:
        try:
            with tarfile.open(path, mode="r:xz") as archive:
                candidates = []
                total = 0
                names = set()
                for count, member in enumerate(archive, 1):
                    raw = member.name.replace("\\", "/")
                    parsed = PurePosixPath(raw)
                    total += member.size
                    if (
                        parsed.is_absolute()
                        or ".." in parsed.parts
                        or ":" in raw
                        or len(raw) > 1024
                        or "\x00" in raw
                        or raw in names
                        or not (member.isfile() or member.isdir())
                        or count > cls.MAX_ARCHIVE_MEMBERS
                        or total > cls.MAX_EXPANDED_BYTES
                    ):
                        raise DependencyUpdateError("TAR 条目路径、类型、数量或展开大小不安全。")
                    names.add(raw)
                    if (
                        not member.isfile()
                        or parsed.name != basename
                        or "/bin/" not in ("/" + raw.casefold())
                    ):
                        continue
                    if member.size < 64 * 1024 or member.size > 300 * 1024 * 1024:
                        raise DependencyUpdateError(f"FFmpeg 压缩包中的 {basename} 大小异常。")
                    candidates.append(member)
                if len(candidates) != 1:
                    raise DependencyUpdateError(
                        f"FFmpeg TAR.XZ 中应恰好包含一个 bin/{basename}，实际找到 {len(candidates)} 个。"
                    )
                stream = archive.extractfile(candidates[0])
                if stream is None:
                    raise DependencyUpdateError(f"无法读取 FFmpeg {basename}。")
                payload = stream.read(300 * 1024 * 1024 + 1)
                if len(payload) != candidates[0].size:
                    raise DependencyUpdateError(f"FFmpeg {basename} 内容长度异常。")
                return payload
        except tarfile.TarError as exc:
            raise DependencyUpdateError("FFmpeg TAR.XZ 不是有效压缩包。") from exc

    @classmethod
    def _read_zip_binary(cls, path: Path, basename: str, *, require_bin_dir: bool = True) -> bytes:
        try:
            # Bound central-directory parsing before ZipFile allocates member objects.
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 65557))
                tail = stream.read(65557)
            offset = tail.rfind(b"PK\x05\x06")
            if offset < 0 or offset + 22 > len(tail):
                raise DependencyUpdateError("ZIP 目录记录无效。")
            (
                _,
                disk,
                directory_disk,
                count_on_disk,
                count,
                directory_size,
                directory_offset,
                comment_size,
            ) = struct.unpack("<4s4H2LH", tail[offset : offset + 22])
            if (
                disk != 0
                or directory_disk != 0
                or count_on_disk != count
                or not 0 < count <= cls.MAX_ARCHIVE_MEMBERS
                or directory_size > 4 * 1024 * 1024
                or directory_offset + directory_size > size - 22
                or offset + 22 + comment_size != len(tail)
            ):
                raise DependencyUpdateError("ZIP 目录数量、大小或布局不受支持。")
            with zipfile.ZipFile(path) as archive:
                members = cls._safe_zip_members(archive)
                candidates = [
                    m
                    for m in members
                    if PurePosixPath(m.filename.replace("\\", "/")).name.casefold()
                    == basename.casefold()
                    and (
                        not require_bin_dir
                        or "/bin/" in ("/" + m.filename.replace("\\", "/").casefold())
                    )
                ]
                if len(candidates) != 1:
                    expected = f"bin/{basename}" if require_bin_dir else basename
                    raise DependencyUpdateError(
                        f"压缩包中应恰好包含一个 {expected}，实际找到 {len(candidates)} 个。"
                    )
                info = candidates[0]
                if info.file_size < 64 * 1024 or info.file_size > 300 * 1024 * 1024:
                    raise DependencyUpdateError(f"压缩包中的 {basename} 大小异常。")
                with archive.open(info, "r") as stream:
                    payload = stream.read(300 * 1024 * 1024 + 1)
                if len(payload) != info.file_size:
                    raise DependencyUpdateError(f"压缩包中的 {basename} 内容长度异常。")
                return payload
        except zipfile.BadZipFile as exc:
            raise DependencyUpdateError("官方 ZIP 不是有效压缩包。") from exc
