"""Updater source, archive, transaction and cancellation security regressions."""

import asyncio
import hashlib
import io
import os
import stat
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.dependency_manager import DependencyManager, DependencyUpdateError
from core.process import ProcessResult
from core.settings import Settings

URL = "https://github.com/denoland/deno/releases/download/v2.0.0/deno.zip"


class Response:
    def __init__(self, chunks=(b"data",), headers=None, status=200):
        self.chunks, self.headers, self.status = chunks, headers or {}, status
        self.content = self

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            await asyncio.sleep(0)
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.urls.append(url)
        return next(self.responses)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = DependencyManager(self.root, Settings.from_config({}, data_dir=self.root))

    async def test_redirect_rejects_external_http_credentials_and_wrong_repository(self):
        for target in (
            "http://github.com/denoland/deno/releases/download/v2/a",
            "https://evil.test/a",
            "https://127.0.0.1/a",
            "https://github.com/evil/repo/releases/download/v2/a",
            "https://user:password@release-assets.githubusercontent.com/a",
        ):
            session = Session(Response(status=302, headers={"Location": target}))
            with self.assertRaises(DependencyUpdateError):
                await self.manager._get(session, URL, max_bytes=100, label="test")
            self.assertEqual(session.urls, [URL])

    async def test_pinned_cdn_redirect_is_allowed(self):
        target = (
            "https://release-assets.githubusercontent.com/github-production-release-asset/a?sig=x"
        )
        session = Session(Response(status=302, headers={"Location": target}), Response())
        self.assertEqual(
            await self.manager._get(session, URL, max_bytes=100, label="test"), b"data"
        )
        self.assertEqual(session.urls, [URL, target])

    def test_gyan_sources_are_version_pinned_and_never_arbitrary_paths(self):
        self.manager._validate_source("https://www.gyan.dev/ffmpeg/builds/release-version")
        self.manager._validate_source(
            "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256"
        )
        for path in (
            "ffmpeg-release-essentials.zip",
            "packages/evil.zip",
            "packages/ffmpeg-9.0.2-full_build.zip",
        ):
            with self.assertRaises(DependencyUpdateError):
                self.manager._validate_source(f"https://www.gyan.dev/ffmpeg/builds/{path}")

    async def test_redirect_loop_is_bounded(self):
        session = Session(*(Response(status=302, headers={"Location": URL}) for _ in range(6)))
        with self.assertRaises(DependencyUpdateError):
            await self.manager._get(session, URL, max_bytes=100, label="test")
        self.assertEqual(len(session.urls), 6)

    async def test_invalid_length_encoding_http_and_oversized_metadata(self):
        for response in (
            Response(headers={"Content-Length": "-1"}),
            Response(headers={"Content-Length": "101"}),
            Response(headers={"Content-Length": "8"}),
            Response(headers={"Content-Encoding": "gzip"}),
            Response(status=429),
            Response(chunks=(b"x" * 101,)),
            Response(chunks=()),
        ):
            with self.assertRaises(DependencyUpdateError):
                await self.manager._get(Session(response), URL, max_bytes=100, label="test")

    async def test_failed_cancelled_and_truncated_downloads_remove_partial_files(self):
        for response, error in (
            (Response(headers={"Content-Length": "8"}), DependencyUpdateError),
            (Response(chunks=(b"x" * 101,)), DependencyUpdateError),
            (Response(chunks=(b"data", TimeoutError())), TimeoutError),
            (Response(chunks=(b"data", asyncio.CancelledError())), asyncio.CancelledError),
        ):
            path = self.root / "download"
            with self.assertRaises(error):
                await self.manager._download(
                    Session(response), URL, path, max_bytes=100, label="test"
                )
            self.assertFalse(path.exists())

    async def test_download_digest_final_progress_and_phase_metrics_reset(self):
        import hashlib

        path = self.root / "download"
        result = await self.manager._download(
            Session(Response(headers={"Content-Length": "4"})),
            URL,
            path,
            max_bytes=100,
            label="test",
        )
        self.assertEqual(result["sha256"], hashlib.sha256(b"data").hexdigest())
        self.assertEqual(self.manager.state["downloaded_bytes"], 4)
        self.assertEqual(self.manager.state["progress"], 100)
        self.manager._emit(phase="verify")
        self.assertEqual(self.manager.state["downloaded_bytes"], 0)
        self.assertIsNone(self.manager.state["speed_bytes_per_second"])

    async def test_ytdlp_version_checks_ignore_external_configs_and_plugins(self):
        paths = (
            self.manager.settings.yt_dlp_path,
            self.manager.settings.deno_path,
            self.manager.settings.ffmpeg_path,
            self.manager.settings.ffprobe_path,
        )
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic executable")
        results = [
            ProcessResult((), 0, version, "")
            for version in (
                "2026.11.01",
                "deno 2.6.0",
                "ffmpeg version 8.0.1",
                "ffprobe version 8.0.1",
            )
        ]
        with (
            patch("core.dependency_manager.run_process", new=AsyncMock(side_effect=results)) as run,
            patch.object(DependencyManager, "supported_environment", return_value=(True, "test")),
        ):
            report = await self.manager.inspect()
        self.assertTrue(report["all_ready"])
        self.assertEqual(
            run.await_args_list[0].args[0],
            [str(paths[0]), "--ignore-config", "--no-plugin-dirs", "--version"],
        )
        with patch(
            "core.dependency_manager.run_process", new=AsyncMock(return_value=results[0])
        ) as run:
            self.assertEqual(await self.manager._verify_binary(paths[0], paths[0]), "2026.11.01")
        self.assertEqual(
            run.await_args.args[0],
            [str(paths[0]), "--ignore-config", "--no-plugin-dirs", "--version"],
        )

    def test_hashes_are_strict_unique_and_exact_asset_names(self):
        digest = "a" * 64
        self.assertEqual(self.manager._extract_hash(digest + "\n"), digest)
        self.assertEqual(self.manager._extract_hash(f"{digest}  file.zip\n", "file.zip"), digest)
        for value, name in (
            (f"garbage {digest} end", None),
            (f"{digest}  other.zip", "file.zip"),
            (f"{digest}  dir/file.zip", "file.zip"),
            (f"{digest}  file.zip\n{digest}  file.zip", "file.zip"),
            (digest + "\x00", None),
        ):
            with self.assertRaises(DependencyUpdateError):
                self.manager._extract_hash(value, name)

    def test_asset_requires_exact_repository_release_tag_and_name(self):
        release = {
            "tag_name": "v2.0.0",
            "assets": [{"name": "deno.zip", "browser_download_url": URL}],
        }
        self.assertEqual(self.manager._asset(release, "deno.zip", "denoland/deno"), URL)
        for url in (
            URL.replace("denoland/deno", "evil/repo"),
            URL.replace("v2.0.0", "v1"),
            URL + "?x=1",
        ):
            release["assets"][0]["browser_download_url"] = url
            with self.assertRaises(DependencyUpdateError):
                self.manager._asset(release, "deno.zip", "denoland/deno")

    def test_current_deno_checksum_formats_are_platform_specific_and_fail_closed(self):
        digest = "a" * 64
        text = f"\r\nAlgorithm : SHA256\r\nHash      : {digest.upper()}\r\nPath      : C:\\a\\deno\\target\\release\\deno.zip\r\n\r\n"
        self.assertEqual(self.manager._deno_hash(text, "deno.zip", "windows"), digest)
        self.assertEqual(
            self.manager._deno_hash(f"{digest}  deno.zip\n", "deno.zip", "linux"), digest
        )
        for invalid in (
            text.replace("SHA256", "SHA1"),
            text.replace("deno.zip", "other.zip"),
            text + f"Hash : {digest}\n",
            digest,
        ):
            with self.assertRaises(DependencyUpdateError):
                self.manager._deno_hash(invalid, "deno.zip", "windows")

    def test_malicious_zip_entries_are_rejected(self):
        for name, mode in (
            ("../deno", stat.S_IFREG),
            ("C:/deno", stat.S_IFREG),
            ("deno", stat.S_IFLNK),
            ("deno", stat.S_IFCHR),
        ):
            path = self.root / "bad.zip"
            with zipfile.ZipFile(path, "w") as archive:
                info = zipfile.ZipInfo(name)
                info.external_attr = (mode | 0o755) << 16
                archive.writestr(info, b"x" * 65536)
            with self.assertRaises(DependencyUpdateError):
                self.manager._read_zip_binary(path, "deno", require_bin_dir=False)

    def test_archive_count_expansion_and_duplicate_limits(self):
        path = self.root / "bad.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("deno", b"x" * 65536)
            archive.writestr("DENO", b"x" * 65536)
        with self.assertRaises(DependencyUpdateError):
            self.manager._read_zip_binary(path, "deno", require_bin_dir=False)
        with patch.object(DependencyManager, "MAX_ARCHIVE_MEMBERS", 1):
            with self.assertRaises(DependencyUpdateError):
                self.manager._read_zip_binary(path, "deno", require_bin_dir=False)
        with patch.object(DependencyManager, "MAX_EXPANDED_BYTES", 10):
            with self.assertRaises(DependencyUpdateError):
                self.manager._read_zip_binary(path, "deno", require_bin_dir=False)

    def test_tar_paths_links_and_expanded_limits(self):
        path = self.root / "bad.tar.xz"
        for name, kind in (("../bin/ffmpeg", tarfile.REGTYPE), ("bin/ffmpeg", tarfile.SYMTYPE)):
            with tarfile.open(path, "w:xz") as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = 65536 if kind == tarfile.REGTYPE else 0
                archive.addfile(member, io.BytesIO(b"x" * member.size))
            with self.assertRaises(DependencyUpdateError):
                self.manager._read_tar_binary(path, "ffmpeg")


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings.from_config({}, data_dir=self.root)
        self.manager = DependencyManager(self.root, self.settings)
        self.paths = [
            self.settings.yt_dlp_path,
            self.settings.deno_path,
            self.settings.ffmpeg_path,
            self.settings.ffprobe_path,
        ]
        self.originals = {path: path.name.encode() for path in self.paths}
        for path, data in self.originals.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.enterContext(
            patch.object(DependencyManager, "supported_environment", return_value=(True, "test"))
        )
        self.enterContext(
            patch(
                "core.dependency_manager.shutil.disk_usage",
                return_value=SimpleNamespace(free=8 * 1024**3),
            )
        )
        for method, paths in (
            ("_stage_ytdlp", self.paths[:1]),
            ("_stage_deno", self.paths[1:2]),
            ("_stage_ffmpeg", self.paths[2:]),
        ):

            async def stage(_session, directory, targets, selected=paths):
                for target in selected:
                    staged = directory / f"stage-{target.name}"
                    staged.write_bytes(b"x" * 65536)
                    targets[target] = staged

            self.enterContext(patch.object(self.manager, method, side_effect=stage))

        async def verify(command, **_kwargs):
            name = Path(command[0]).name.removeprefix("stage-")
            text = (
                "2026.11.01"
                if name.startswith("yt-dlp")
                else "deno 2.6.0 (release, x86_64-pc-windows-msvc)"
                if name.startswith("deno")
                else "ffprobe version 8.0.1"
                if name.startswith("ffprobe")
                else "ffmpeg version 8.0.1"
            )
            return ProcessResult((), 0, text, "")

        self.verifier = self.enterContext(
            patch("core.dependency_manager.run_process", new=AsyncMock(side_effect=verify))
        )

    def assert_restored(self):
        for path, data in self.originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(list(self.root.rglob("*.new")))

    def staging(self):
        return list((self.root / "dependency-staging").glob("*"))

    async def test_success_verifies_staged_and_installed_binaries_and_reports_versions(self):
        result = await self.manager.update("all")
        self.assertEqual(self.verifier.await_count, 8)
        self.assertEqual(result["updated"][self.settings.deno_path.name], "2.6.0")
        self.assertEqual(self.manager.state["status"], "success")
        self.assertFalse(self.staging())
        self.assertFalse(self.manager.dependency_gate.maintaining)

    async def test_staged_failure_never_overwrites_originals(self):
        self.verifier.return_value = ProcessResult((), 0, "garbage", "")
        self.verifier.side_effect = None
        with self.assertRaises(DependencyUpdateError):
            await self.manager.update("all")
        self.assert_restored()
        self.assertFalse(self.staging())

    async def test_install_verification_failure_rolls_back_and_removes_new_target(self):
        self.paths[-1].unlink()
        self.originals.pop(self.paths[-1])
        self.verifier.side_effect = [
            ProcessResult((), 0, text, "")
            for text in (
                "2026.11.01",
                "deno 2.6.0",
                "ffmpeg version 8.0.1",
                "ffprobe version 8.0.1",
                "2026.10.01",
            )
        ]
        with self.assertRaises(DependencyUpdateError):
            await self.manager.update("all")
        self.assert_restored()
        self.assertFalse(self.paths[-1].exists())
        self.assertFalse(self.staging())

    async def test_partial_install_rolls_back_all_commits(self):
        original = self.manager._atomic_copy

        def copy(source, target, **kwargs):
            if source.name == f"stage-{self.paths[2].name}":
                raise PermissionError("locked")
            original(source, target, **kwargs)

        with patch.object(DependencyManager, "_atomic_copy", side_effect=copy):
            with self.assertRaises(DependencyUpdateError):
                await self.manager.update("all")
        self.assert_restored()

    async def test_cancel_during_install_waits_for_thread_then_restores(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager._atomic_copy

        def copy(source, target, **kwargs):
            original(source, target, **kwargs)
            if source.name == f"stage-{self.paths[0].name}":
                entered.set()
                release.wait(5)

        with patch.object(DependencyManager, "_atomic_copy", side_effect=copy):
            task = asyncio.create_task(self.manager.update("all"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 4))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assert_restored()
        self.assertFalse(self.staging())

    async def test_failed_rollback_retains_backup_and_blocks_restart(self):
        original = self.manager._atomic_copy

        def copy(source, target, **kwargs):
            if source.name.startswith("backup-") or source.name == f"stage-{self.paths[2].name}":
                raise PermissionError("locked")
            original(source, target, **kwargs)

        with patch.object(DependencyManager, "_atomic_copy", side_effect=copy):
            with self.assertRaises(DependencyUpdateError):
                await self.manager.update("all")
        self.assertTrue(self.manager.state["recovery_required"])
        backups = list(self.root.rglob("backup-*"))
        self.assertTrue(backups)
        restarted = DependencyManager(self.root, self.settings)
        with self.assertRaisesRegex(DependencyUpdateError, "恢复"):
            await restarted.update("Deno")
        self.assertTrue(all(path.exists() for path in backups))

    async def test_total_deadline_includes_waiting_and_leaves_failed_state(self):
        self.manager.settings = replace(self.settings, dependency_update_timeout_seconds=0.02)
        async with self.manager.dependency_gate.use():
            with self.assertRaises(DependencyUpdateError):
                await asyncio.create_task(self.manager.update("Deno"))
        self.assertEqual(self.manager.state["phase"], "超时")
        self.assertFalse(self.manager.dependency_gate.maintaining)
        self.assert_restored()

    async def test_total_deadline_during_staging_cleans_files_before_releasing_gate(self):
        self.manager.settings = replace(self.settings, dependency_update_timeout_seconds=0.05)

        async def stage(_session, directory, _targets):
            (directory / "partial").write_bytes(b"partial")
            await asyncio.sleep(10)

        with patch.object(self.manager, "_stage_deno", side_effect=stage):
            with self.assertRaises(DependencyUpdateError):
                await self.manager.update("Deno")
        self.assert_restored()
        self.assertFalse(self.staging())
        self.assertFalse(self.manager.dependency_gate.maintaining)
        self.assertEqual(self.manager.state["phase"], "超时")

    async def test_preflight_and_installation_run_in_workers(self):
        main_thread = threading.get_ident()
        threads = []
        prepare, install = self.manager._prepare_update, self.manager._install_targets

        def preflight(*args):
            threads.append(threading.get_ident())
            time.sleep(0.03)
            return prepare(*args)

        def installation(*args):
            threads.append(threading.get_ident())
            time.sleep(0.03)
            return install(*args)

        with (
            patch.object(self.manager, "_prepare_update", side_effect=preflight),
            patch.object(self.manager, "_install_targets", side_effect=installation),
        ):
            await self.manager.update("all")
        self.assertEqual(len(threads), 2)
        self.assertNotIn(main_thread, threads)

    async def test_current_deno_and_ffmpeg_extraction_run_in_workers(self):
        archive_path = self.root / "source.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            for name in ("deno.exe", "bin/ffmpeg.exe", "bin/ffprobe.exe"):
                archive.writestr(name, b"x" * 65536)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        self.manager._github_release = AsyncMock(return_value={"tag_name": "v2.9.8"})
        self.manager._download = AsyncMock(return_value={"path": archive_path, "sha256": digest})
        directory = self.root / "stage"
        directory.mkdir()
        self.manager._get = AsyncMock(
            side_effect=[
                f"Algorithm : SHA256\nHash : {digest}\nPath : C:\\target\\release\\deno-x86_64-pc-windows-msvc.zip\n".encode(),
                b"9.0.2",
                digest.encode(),
            ]
        )
        threads = []
        original = DependencyManager._stage_archive_binary

        def stage(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with (
            patch.object(
                DependencyManager,
                "platform_profile",
                return_value={"system": "windows", "arch": "x86_64"},
            ),
            patch.object(DependencyManager, "_asset", return_value=URL),
            patch.object(DependencyManager, "_stage_archive_binary", side_effect=stage),
        ):
            await DependencyManager._stage_deno(self.manager, None, directory, {})
            await DependencyManager._stage_ffmpeg(self.manager, None, directory, {})
        self.assertEqual(len(threads), 3)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_cancel_during_failed_rollback_preserves_result_and_backup(self):
        entered, release = threading.Event(), threading.Event()
        original = self.manager._atomic_copy

        def copy(source, target, **kwargs):
            if source.name == f"stage-{self.paths[2].name}":
                raise PermissionError("locked")
            original(source, target, **kwargs)

        def restore(*_args):
            entered.set()
            release.wait(5)
            return [self.paths[0]]

        with (
            patch.object(DependencyManager, "_atomic_copy", side_effect=copy),
            patch.object(DependencyManager, "_rollback", side_effect=restore),
        ):
            task = asyncio.create_task(self.manager.update("all"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 4))
                task.cancel()
                release.set()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertTrue(self.manager.state["recovery_required"])
        self.assertIn("回滚未完成", self.manager.state["message"])
        self.assertTrue(list(self.root.rglob("backup-*")))


class PlatformTests(unittest.TestCase):
    def test_supported_platforms_and_conservative_rejection(self):
        for system, architecture, libc, expected in (
            ("Windows", "AMD64", ("unknown", None), True),
            ("Windows", "ARM64", ("unknown", None), False),
            ("Linux", "x86_64", ("glibc", (2, 28)), True),
            ("Linux", "aarch64", ("glibc", (2, 35)), True),
            ("Linux", "x86_64", ("musl", (1, 2)), False),
            ("Linux", "x86_64", ("glibc", (2, 27)), False),
            ("Linux", "x86_64", ("unknown", None), False),
            ("Darwin", "arm64", ("unknown", None), False),
        ):
            with (
                patch.dict(
                    os.environ, {"PROCESSOR_ARCHITEW6432": "", "PROCESSOR_ARCHITECTURE": ""}
                ),
                patch("core.dependency_manager.platform.system", return_value=system),
                patch("core.dependency_manager.platform.machine", return_value=architecture),
                patch.object(DependencyManager, "_linux_libc", return_value=libc),
                patch.object(
                    DependencyManager, "python3_compatibility", return_value=(True, "Python test")
                ),
            ):
                self.assertEqual(
                    DependencyManager.supported_environment()[0],
                    expected,
                    (system, architecture, libc),
                )


class PublisherSignatureTests(unittest.TestCase):
    FINGERPRINT = "AC0CBBE6848D6A873464AF4E57CF65933B5A7581"

    def verify(self, status, *, fingerprint=None, returncode=0):
        fingerprint = fingerprint or self.FINGERPRINT
        listing = "pub::::::::::\n" + ":".join(["fpr", *[""] * 8, fingerprint, ""]) + "\n"
        results = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout=listing, stderr=""),
            SimpleNamespace(returncode=returncode, stdout=status, stderr=""),
        ]
        with patch("core.dependency_manager.subprocess.run", side_effect=results) as run:
            DependencyManager._verify_yt_dlp_manifest_signature(
                b"synthetic detached signature",
                b"synthetic checksum manifest",
                gpg_executable="gpg",
            )
        return run

    def signature_status(self, signer=None, primary=None):
        signer = signer or self.FINGERPRINT
        primary = primary or self.FINGERPRINT
        return f"[GNUPG:] VALIDSIG {signer} 2026-11-18 1 0 4 0 1 8 00 {primary}\n"

    def test_primary_and_pinned_signing_subkey_are_accepted_without_network_key_lookup(self):
        for signer in (self.FINGERPRINT, "B" * 40):
            run = self.verify(self.signature_status(signer=signer))
            self.assertEqual(run.call_count, 3)
            command = run.call_args.args[0]
            self.assertIn("--no-options", command)
            self.assertIn("--no-auto-key-retrieve", command)
            self.assertIn("--no-auto-key-import", command)
            self.assertEqual(command[command.index("--homedir") + 1], ".")

    def test_untrusted_primary_missing_invalid_expired_or_failed_signature_is_rejected(self):
        for status, returncode in (
            (self.signature_status(primary="B" * 40), 0),
            ("[GNUPG:] GOODSIG 1234 Publisher\n", 0),
            (self.signature_status() + "[GNUPG:] BADSIG 1234 Publisher\n", 0),
            (self.signature_status() + "[GNUPG:] EXPKEYSIG 1234 Publisher\n", 0),
            (self.signature_status(), 1),
        ):
            with self.subTest(status=status, returncode=returncode):
                with self.assertRaises(DependencyUpdateError):
                    self.verify(status, returncode=returncode)
        with self.assertRaises(DependencyUpdateError):
            self.verify(self.signature_status(), fingerprint="B" * 40)

    def test_missing_gnupg_fails_closed_before_executing(self):
        with (
            patch("core.dependency_manager.shutil.which", return_value=None),
            patch("core.dependency_manager.subprocess.run") as run,
        ):
            with self.assertRaisesRegex(DependencyUpdateError, "GnuPG"):
                DependencyManager._verify_yt_dlp_manifest_signature(b"signature", b"manifest")
            run.assert_not_called()
