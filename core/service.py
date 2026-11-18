"""YouTube search, metadata, download, WAV preparation and audio-cache reuse."""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .audio_cache import AudioCache, AudioCacheEntry
from .errors import UserFacingError as UserFacingError
from .errors import friendly_process_error
from .maintenance import run_blocking, uses_dependencies
from .process import ProcessError, ProcessOutputLimit, ProcessTimeout, run_process
from .settings import Settings
from .utils import Song, parse_youtube_url, song_from_flat_entry, song_from_full_info

StageCallback = Callable[[str], None]
OutputLineCallback = Callable[[str, str], object]
_LOCAL_AUDIO_FORMATS = "aac,flac,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,ogg,mp3,wav"


@dataclass
class _VideoLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class YouTubeMusicService:
    def __init__(self, settings: Settings, audio_cache: AudioCache | None = None):
        self.settings = settings
        self.audio_cache = audio_cache
        self._cache_locks: dict[str, _VideoLock] = {}
        self._stage_semaphores: dict[str, asyncio.Semaphore] = {}
        self.dependency_gate = None

    def _stage_limit(self, name: str, default: int) -> asyncio.Semaphore:
        return self._stage_semaphores.setdefault(name, asyncio.Semaphore(default))

    def _proxy_args(self) -> list[str]:
        return ["--proxy", self.settings.youtube_proxy] if self.settings.youtube_proxy else []

    @staticmethod
    def _redact_error(value: str) -> str:
        import re

        text = str(value or "")
        text = re.sub(r"(?i)(?:https?|socks5)://\S+", "[URL REDACTED]", text)
        text = re.sub(
            r"(?i)(password|passwd|token|secret|cookie)=([^&\s]+)", r"\1=[REDACTED]", text
        )
        return text

    def child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        prepend = [
            str(self.settings.deno_path.parent),
            str(self.settings.ffmpeg_path.parent),
            str(self.settings.ffprobe_path.parent),
        ]
        existing = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(
            [part for part in prepend if part] + ([existing] if existing else [])
        )
        return env

    def validate_binaries(self) -> list[str]:
        missing: list[str] = []
        for label, path in (
            ("yt-dlp", self.settings.yt_dlp_path),
            ("FFmpeg", self.settings.ffmpeg_path),
            ("ffprobe", self.settings.ffprobe_path),
            ("Deno", self.settings.deno_path),
        ):
            if not path.is_file():
                missing.append(f"{label}: {path}")
        return missing

    async def _run_ytdlp(
        self,
        args: list[str],
        timeout: int,
        label: str,
        *,
        on_output_line: OutputLineCallback | None = None,
        monitor_directory: Path | None = None,
        max_directory_size_bytes: int | None = None,
    ):
        try:
            return await run_process(
                [
                    str(self.settings.yt_dlp_path),
                    "--ignore-config",
                    "--no-plugin-dirs",
                    *self._proxy_args(),
                    *args,
                ],
                timeout=timeout,
                env=self.child_env(),
                label=label,
                on_output_line=on_output_line,
                monitor_directory=monitor_directory,
                monitor_pattern="source.*",
                max_directory_size_bytes=max_directory_size_bytes,
            )
        except (ProcessError, ProcessTimeout, ProcessOutputLimit) as exc:
            raise UserFacingError(
                self._redact_error(
                    friendly_process_error(
                        exc, label, max_download_mb=self.settings.max_download_size_mb
                    )
                )
            ) from exc

    @staticmethod
    def _search_relevance(query: str, song: Song) -> float:
        """Deterministic title/artist relevance score; YouTube order remains the tie-breaker."""
        import re

        normalized = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", str(query).casefold()))
        title = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", str(song.title).casefold()))
        artist = " ".join(re.findall(r"[\w\u4e00-\u9fff]+", str(song.artist).casefold()))
        if not normalized:
            return 0.0
        score = 0.0
        if normalized in title:
            score += 8.0
        if title.startswith(normalized):
            score += 2.0
        tokens = [token for token in normalized.split() if len(token) > 1]
        for token in tokens:
            if token in title:
                score += 2.0
            elif token in artist:
                score += 1.25
        if tokens:
            score += (
                3.0 * sum(1 for token in tokens if token in title or token in artist) / len(tokens)
            )
        return score

    @uses_dependencies
    async def search(
        self,
        query: str,
        *,
        max_duration_seconds: int | None = None,
        result_limit: int | None = None,
    ) -> list[Song]:
        query = query.strip()
        if not query:
            raise UserFacingError("请提供要搜索的歌曲关键词。")
        if len(query) > 200:
            raise UserFacingError("搜索关键词超过 200 字符。")
        if query.startswith("-"):
            query = " " + query

        # Fetch extra candidates because duplicates, unknown-duration, and over-limit items are filtered.
        duration_cap = (
            min(self.settings.max_duration_seconds, max(15, int(max_duration_seconds)))
            if max_duration_seconds is not None
            else self.settings.max_duration_seconds
        )
        result_count = (
            min(self.settings.search_results, max(3, int(result_limit)))
            if result_limit is not None
            else self.settings.search_results
        )
        fetch_count = result_count * 3
        search_target = f"ytsearch{fetch_count}:{query}"
        async with self._stage_limit("search_concurrency", 2):
            result = await self._run_ytdlp(
                [
                    "--flat-playlist",
                    "--dump-single-json",
                    "--skip-download",
                    "--no-warnings",
                    "--no-progress",
                    "--js-runtimes",
                    "deno",
                    search_target,
                ],
                timeout=self.settings.search_timeout_seconds,
                label="YouTube 搜索",
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UserFacingError("搜索结果解析失败，请检查 yt-dlp。") from exc

        entries = payload.get("entries") if isinstance(payload, dict) else None
        songs: list[Song] = []
        seen_ids: set[str] = set()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            song = song_from_flat_entry(entry)
            if not song or song.video_id in seen_ids:
                continue
            seen_ids.add(song.video_id)
            if song.duration is None or song.duration <= 0 or song.duration > duration_cap:
                continue
            songs.append(song)
        songs.sort(key=lambda item: self._search_relevance(query, item), reverse=True)
        return songs[:result_count]

    @uses_dependencies
    async def get_metadata(self, url: str, *, max_duration_seconds: int | None = None) -> Song:
        normalized = parse_youtube_url(url)
        if not normalized:
            raise UserFacingError("目前仅支持 YouTube 视频链接（youtube.com 或 youtu.be）。")
        async with self._stage_limit("search_concurrency", 2):
            result = await self._run_ytdlp(
                [
                    "--dump-single-json",
                    "--skip-download",
                    "--no-playlist",
                    "--no-warnings",
                    "--no-progress",
                    "--js-runtimes",
                    "deno",
                    normalized,
                ],
                timeout=self.settings.metadata_timeout_seconds,
                label="读取视频信息",
            )
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UserFacingError("yt-dlp 没有返回可识别的视频信息。") from exc
        if not isinstance(info, dict) or info.get("_type") == "playlist":
            raise UserFacingError("仅支持单个视频，不支持播放列表。")
        try:
            song = song_from_full_info(info)
        except ValueError as exc:
            raise UserFacingError("视频信息缺少有效的 YouTube 视频身份。") from exc
        if song.url != normalized:
            raise UserFacingError("视频身份与请求链接不一致，已拒绝点播。")
        if not song.duration or song.duration <= 0:
            raise UserFacingError("视频时长未知，已拒绝下载。")
        duration_cap = (
            min(self.settings.max_duration_seconds, max(15, int(max_duration_seconds)))
            if max_duration_seconds is not None
            else self.settings.max_duration_seconds
        )
        if song.duration > duration_cap:
            raise UserFacingError(
                f"歌曲时长为 {song.duration} 秒，超过当前上限 {duration_cap} 秒。"
            )
        return song

    @uses_dependencies
    async def download_and_convert(
        self,
        song: Song,
        job_dir: Path,
        *,
        on_output_line: OutputLineCallback | None = None,
        on_stage: StageCallback | None = None,
        max_duration_seconds: int | None = None,
    ) -> tuple[Path, Song, dict]:
        """Use a validated cached WAV when available, otherwise download, convert and cache it."""
        duration_cap = (
            min(self.settings.max_duration_seconds, max(15, int(max_duration_seconds)))
            if max_duration_seconds is not None
            else self.settings.max_duration_seconds
        )
        async with self._lock_for(song.video_id):
            cached = await self._validated_cache_entry(
                song.video_id, on_stage=on_stage, max_duration_seconds=duration_cap
            )
            if cached:
                return cached.path, cached.song, cached.media
            resolved = await self.get_metadata(song.url, max_duration_seconds=duration_cap)
            if resolved.video_id != song.video_id:
                raise UserFacingError("视频身份与点播项不一致，已拒绝下载。")
            return await self._download_resolved(
                resolved,
                job_dir,
                on_output_line=on_output_line,
                on_stage=on_stage,
                max_duration_seconds=duration_cap,
            )

    @asynccontextmanager
    async def _lock_for(self, video_id: str):
        entry = self._cache_locks.setdefault(video_id, _VideoLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if not entry.users:
                self._cache_locks.pop(video_id)

    async def _validated_cache_entry(
        self,
        video_id: str,
        *,
        on_stage: StageCallback | None,
        max_duration_seconds: int | None = None,
    ) -> AudioCacheEntry | None:
        if not self.audio_cache:
            return None
        entry = await run_blocking(self.audio_cache.lookup, video_id)
        if not entry:
            return None
        if on_stage:
            on_stage("校验已缓存音频")
        try:
            probe = await self._probe(entry.path)
            stream = self._validated_wav_stream(probe)
            duration = float(probe.get("duration") or 0)
            duration_cap = (
                min(self.settings.max_duration_seconds, max(15, int(max_duration_seconds)))
                if max_duration_seconds is not None
                else self.settings.max_duration_seconds
            )
            if not stream or duration <= 0 or duration > duration_cap:
                raise UserFacingError("缓存 WAV 不符合当前格式要求。")
            if entry.path.stat().st_size > self.settings.max_wav_size_mb * 1024 * 1024:
                raise UserFacingError("缓存 WAV 超过当前大小上限。")
            entry.media.update(
                {
                    "duration_seconds": duration,
                    "wav_size_bytes": entry.path.stat().st_size,
                    "sample_rate": 16000,
                    "channels": 1,
                    "codec": "pcm_s16le",
                    "cache_hit": True,
                }
            )
            return entry
        except (OSError, ValueError, TypeError, UserFacingError):
            await run_blocking(self.audio_cache.invalidate, video_id)
            return None

    async def _download_resolved(
        self,
        resolved: Song,
        job_dir: Path,
        *,
        on_output_line: OutputLineCallback | None,
        on_stage: StageCallback | None,
        max_duration_seconds: int | None = None,
    ) -> tuple[Path, Song, dict]:
        duration_cap = (
            min(self.settings.max_duration_seconds, max(15, int(max_duration_seconds)))
            if max_duration_seconds is not None
            else self.settings.max_duration_seconds
        )
        job_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(job_dir / "source.%(ext)s")
        max_source_bytes = self.settings.max_download_size_mb * 1024 * 1024
        if on_stage:
            on_stage("下载音轨")
        async with self._stage_limit("download_concurrency", self.settings.max_concurrent_jobs):
            await self._run_ytdlp(
                [
                    "--no-playlist",
                    "--no-warnings",
                    "--newline",
                    "--progress",
                    "--format",
                    "bestaudio/best",
                    "--match-filter",
                    f"duration <= {duration_cap}",
                    "--max-filesize",
                    f"{self.settings.max_download_size_mb}M",
                    "--ffmpeg-location",
                    str(self.settings.ffmpeg_path.parent),
                    "--js-runtimes",
                    "deno",
                    "--output",
                    output_template,
                    resolved.url,
                ],
                timeout=self.settings.download_timeout_seconds,
                label="下载音轨",
                on_output_line=on_output_line,
                monitor_directory=job_dir,
                max_directory_size_bytes=max_source_bytes,
            )

        source_candidates = [
            path
            for path in job_dir.glob("source.*")
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl", ".json", ".description"))
        ]
        if not source_candidates:
            raise UserFacingError("yt-dlp 下载结束，但没有找到音频文件。")
        source = max(source_candidates, key=lambda path: path.stat().st_size)
        source_size = source.stat().st_size
        if source_size <= 0:
            raise UserFacingError("下载得到的音频文件为空。")
        if source_size > max_source_bytes:
            raise UserFacingError(
                f"下载文件大小为 {source_size / 1024 / 1024:.1f} MiB，超过上限 {self.settings.max_download_size_mb} MiB。"
            )

        if on_stage:
            on_stage("检查源音频")
        input_probe = await self._probe(source)
        duration = input_probe.get("duration")
        if duration is None or duration <= 0:
            raise UserFacingError("无法读取已下载音频的时长。")
        if duration > duration_cap:
            raise UserFacingError("已下载音频超过时长上限，已中止转换。")
        audio_streams = [
            stream
            for stream in input_probe.get("streams", [])
            if stream.get("codec_type") == "audio"
        ]
        if not audio_streams:
            raise UserFacingError("下载的媒体没有音频流。")

        estimated_wav_bytes = int(duration * 16000 * 2) + 4096
        max_wav_bytes = self.settings.max_wav_size_mb * 1024 * 1024
        if estimated_wav_bytes > max_wav_bytes:
            max_duration_by_wav = max(1, (max_wav_bytes - 4096) // (16000 * 2))
            raise UserFacingError(
                f"预计 WAV 超过 {self.settings.max_wav_size_mb} MiB，上限约 {max_duration_by_wav} 秒。"
            )

        if on_stage:
            on_stage("转换为 WAV")
        wav_path = job_dir / "record.wav"
        try:
            async with self._stage_limit("conversion_concurrency", 1):
                await run_process(
                    [
                        str(self.settings.ffmpeg_path),
                        "-hide_banner",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-format_whitelist",
                        _LOCAL_AUDIO_FORMATS,
                        "-i",
                        str(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        "-f",
                        "wav",
                        str(wav_path),
                    ],
                    timeout=self.settings.conversion_timeout_seconds,
                    env=self.child_env(),
                    cwd=str(job_dir),
                    label="FFmpeg WAV 转换",
                    monitor_directory=job_dir,
                    monitor_pattern="record.wav",
                    max_directory_size_bytes=max_wav_bytes,
                )
        except ProcessOutputLimit as exc:
            raise UserFacingError(
                f"WAV 文件超过上限 {self.settings.max_wav_size_mb} MiB，已中止转换。"
            ) from exc
        except (ProcessError, ProcessTimeout) as exc:
            raise UserFacingError(friendly_process_error(exc, "FFmpeg 音频转换")) from exc

        if not wav_path.is_file() or wav_path.stat().st_size <= 44:
            raise UserFacingError("FFmpeg 未生成有效 WAV 文件。")
        if wav_path.stat().st_size > max_wav_bytes:
            raise UserFacingError(
                f"WAV 文件大小为 {wav_path.stat().st_size / 1024 / 1024:.1f} MiB，超过上限 {self.settings.max_wav_size_mb} MiB。"
            )
        if on_stage:
            on_stage("验证 WAV 编码与时长")
        output_probe = await self._probe(wav_path)
        stream = self._validated_wav_stream(output_probe)
        if not stream:
            raise UserFacingError("WAV 格式校验失败：要求 PCM S16LE、16 kHz、单声道。")
        output_duration = output_probe.get("duration")
        if output_duration is None or not 0 < output_duration <= duration_cap:
            raise UserFacingError("转换后的 WAV 时长未知或超过上限。")

        media = {
            "source_size_bytes": source_size,
            "wav_size_bytes": wav_path.stat().st_size,
            "duration_seconds": output_duration,
            "sample_rate": 16000,
            "channels": 1,
            "codec": "pcm_s16le",
            "cache_hit": False,
        }
        if self.audio_cache and resolved.video_id:
            if on_stage:
                on_stage("写入歌曲缓存")
            cached_path = await run_blocking(self.audio_cache.store, resolved, wav_path, media)
            if cached_path:
                media["cache_stored"] = True
                return cached_path, resolved, media
        return wav_path, resolved, media

    @staticmethod
    def _validated_wav_stream(probe: dict) -> dict | None:
        rows = probe.get("streams", [])
        if not isinstance(rows, list) or any(not isinstance(stream, dict) for stream in rows):
            return None
        streams = [stream for stream in rows if stream.get("codec_type") == "audio"]
        if not streams:
            return None
        stream = streams[0]
        raw_channels = stream.get("channels")
        if isinstance(raw_channels, bool) or not isinstance(raw_channels, (int, str)):
            return None
        try:
            channels = int(raw_channels)
        except (ValueError, TypeError, OverflowError):
            return None
        if (
            stream.get("codec_name") != "pcm_s16le"
            or str(stream.get("sample_rate")) != "16000"
            or channels != 1
        ):
            return None
        return stream

    @uses_dependencies
    async def _probe(self, path: Path) -> dict:
        try:
            result = await run_process(
                [
                    str(self.settings.ffprobe_path),
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    _LOCAL_AUDIO_FORMATS,
                    "-show_entries",
                    "format=duration,format_name:stream=codec_type,codec_name,sample_rate,channels",
                    "-of",
                    "json",
                    str(path),
                ],
                timeout=20,
                env=self.child_env(),
                label="ffprobe 媒体检查",
            )
        except (ProcessError, ProcessTimeout) as exc:
            raise UserFacingError(friendly_process_error(exc, "音频格式检查")) from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise UserFacingError("ffprobe 返回了无法解析的 JSON。") from exc
        if not isinstance(payload, dict):
            raise UserFacingError("ffprobe 没有返回媒体信息。")
        fmt = payload.get("format", {})
        streams = payload.get("streams", [])
        if (
            not isinstance(fmt, dict)
            or not isinstance(streams, list)
            or any(not isinstance(stream, dict) for stream in streams)
        ):
            raise UserFacingError("ffprobe 返回的媒体格式或音轨信息无效。")
        payload["streams"] = streams
        if isinstance(fmt.get("duration"), bool):
            raise UserFacingError("媒体时长信息无效。")
        try:
            duration = float(fmt.get("duration")) if fmt.get("duration") is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None and not math.isfinite(duration):
            raise UserFacingError("媒体时长不是有限数值。")
        format_name = fmt.get("format_name")
        if (
            not isinstance(format_name, str)
            or not format_name
            or any(name not in _LOCAL_AUDIO_FORMATS.split(",") for name in format_name.split(","))
        ):
            raise UserFacingError("媒体容器不受支持，已拒绝解析播放列表或未知格式。")
        payload["duration"] = duration
        return payload
