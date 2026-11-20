"""Media engine tests with controlled subprocess output; no YouTube network."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.maintenance import MaintenanceGate
from core.process import ProcessOutputLimit, ProcessResult, run_process
from core.service import UserFacingError, YouTubeMusicService
from core.settings import Settings
from core.utils import Song


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self):
        service = YouTubeMusicService(
            Settings.from_config({}, data_dir=Path(tempfile.gettempdir()))
        )
        service.dependency_gate = MaintenanceGate()
        return service

    async def test_search_filters_duplicates_unknown_duration_and_over_limit(self):
        service = self.service()
        payload = {
            "entries": [
                {"id": "abcdefghijk", "title": "Track", "duration": 60},
                {"id": "abcdefghijk", "title": "duplicate", "duration": 60},
                {"id": "bcdefghijkl", "title": "long", "duration": 999},
                {"id": "cdefghijklm", "title": "unknown"},
                {"id": "../unsafe", "duration": 60},
            ]
        }
        service._run_ytdlp = AsyncMock(return_value=ProcessResult((), 0, json.dumps(payload), ""))
        songs = await service.search("Track")
        self.assertEqual([song.video_id for song in songs], ["abcdefghijk"])
        self.assertEqual(songs[0].url, "https://www.youtube.com/watch?v=abcdefghijk")

    async def test_search_id_dedup_keeps_the_most_relevant_valid_metadata(self):
        payload = {
            "entries": [
                {
                    "id": "abcdefghijk",
                    "title": "Unrelated",
                    "channel": "Uploader",
                    "duration": 60,
                },
                {
                    "id": "abcdefghijk",
                    "title": "Hello",
                    "artist": "Adele",
                    "duration": 61,
                    "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
                },
                {
                    "id": "bcdefghijkl",
                    "title": "Hello (Cover)",
                    "artist": "Other Artist",
                    "duration": 60,
                },
            ]
        }
        for candidate_pool in (False, True):
            with self.subTest(candidate_pool=candidate_pool):
                service = self.service()
                service._run_ytdlp = AsyncMock(
                    return_value=ProcessResult((), 0, json.dumps(payload), "")
                )
                songs = await service.search("Adele Hello", candidate_pool=candidate_pool)
                self.assertEqual(
                    [(row.video_id, row.title) for row in songs],
                    [("abcdefghijk", "Hello"), ("bcdefghijkl", "Hello (Cover)")],
                )
                self.assertEqual(songs[0].artist, "Adele")
                self.assertEqual(songs[0].duration, 61)
                self.assertEqual(
                    songs[0].thumbnail_url, "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"
                )

    async def test_invalid_metadata_url_never_executes_a_process(self):
        service = self.service()
        service._run_ytdlp = AsyncMock()
        with self.assertRaises(UserFacingError):
            await service.get_metadata("https://evil.test/track")
        service._run_ytdlp.assert_not_awaited()

    async def test_ytdlp_ignores_ambient_configuration_and_plugins(self):
        service = self.service()
        with patch(
            "core.service.run_process",
            new=AsyncMock(return_value=ProcessResult((), 0, "{}", "")),
        ) as process:
            await service._run_ytdlp(["--skip-download", "ytsearch3:Track"], 10, "search")
        args = process.call_args.args[0]
        self.assertEqual(
            args[:3], [str(service.settings.yt_dlp_path), "--ignore-config", "--no-plugin-dirs"]
        )
        self.assertEqual(args[-2:], ["--skip-download", "ytsearch3:Track"])

    async def test_ytdlp_preserves_system_proxy_fallback_and_explicit_override(self):
        service = self.service()
        for proxy in ("", "http://proxy.invalid:8080"):
            with self.subTest(proxy=proxy):
                service.settings = replace(service.settings, youtube_proxy=proxy)
                with patch.dict("os.environ", {"HTTPS_PROXY": "http://ambient.invalid:8080"}):
                    with patch(
                        "core.service.run_process",
                        new=AsyncMock(return_value=ProcessResult((), 0, "{}", "")),
                    ) as process:
                        await service._run_ytdlp(
                            ["--skip-download", "ytsearch3:Track"], 10, "search"
                        )
                args = process.call_args.args[0]
                if proxy:
                    self.assertEqual(args[args.index("--proxy") + 1], proxy)
                else:
                    self.assertNotIn("--proxy", args)
                self.assertEqual(
                    process.call_args.kwargs["env"]["HTTPS_PROXY"], "http://ambient.invalid:8080"
                )

    async def test_metadata_rejects_unknown_and_excessive_duration(self):
        service = self.service()
        for duration in (None, 999):
            payload = {"id": "abcdefghijk", "title": "Track", "duration": duration}
            service._run_ytdlp = AsyncMock(
                return_value=ProcessResult((), 0, json.dumps(payload), "")
            )
            with self.assertRaises(UserFacingError):
                await service.get_metadata("https://youtu.be/abcdefghijk")

    async def test_metadata_identity_must_match_the_requested_video(self):
        service = self.service()
        for video_id in ("bcdefghijkl", "../invalid", None):
            with self.subTest(video_id=video_id):
                output = json.dumps({"id": video_id, "title": "Track", "duration": 60})
                service._run_ytdlp = AsyncMock(return_value=ProcessResult((), 0, output, ""))
                with self.assertRaisesRegex(UserFacingError, "视频身份"):
                    await service.get_metadata("https://youtu.be/abcdefghijk")

    async def test_probe_rejects_nonfinite_duration(self):
        service = self.service()
        for value in ("NaN", "Infinity"):
            output = json.dumps({"format": {"duration": value}, "streams": []})
            with patch(
                "core.service.run_process",
                new=AsyncMock(return_value=ProcessResult((), 0, output, "")),
            ):
                with self.assertRaises(UserFacingError):
                    await service._probe(Path("track.wav"))
        with patch(
            "core.service.run_process",
            new=AsyncMock(
                return_value=ProcessResult(
                    (), 0, json.dumps({"format": {"duration": True}, "streams": []}), ""
                )
            ),
        ):
            with self.assertRaisesRegex(UserFacingError, "媒体时长信息无效"):
                await service._probe(Path("track.wav"))

    async def test_probe_disallows_network_protocols_for_local_media(self):
        service = self.service()
        output = json.dumps({"format": {"duration": "1", "format_name": "wav"}, "streams": []})
        with patch(
            "core.service.run_process",
            new=AsyncMock(return_value=ProcessResult((), 0, output, "")),
        ) as process:
            await service._probe(Path("track.wav"))
        args = process.call_args.args[0]
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertLess(args.index("-protocol_whitelist"), args.index("track.wav"))
        formats = set(args[args.index("-format_whitelist") + 1].split(","))
        self.assertTrue({"mov", "matroska", "webm", "mp3", "wav"}.issubset(formats))
        self.assertFalse({"hls", "concat", "dash", "sdp"} & formats)
        self.assertLess(args.index("-format_whitelist"), args.index("track.wav"))

    async def test_probe_rejects_playlist_and_network_demuxer_results(self):
        service = self.service()
        for name in ("hls", "concat", "dash", "sdp", "mov,hls", "", None):
            with self.subTest(format_name=name):
                output = json.dumps(
                    {
                        "format": {"duration": "1", "format_name": name},
                        "streams": [{"codec_type": "audio"}],
                    }
                )
                with patch(
                    "core.service.run_process",
                    new=AsyncMock(return_value=ProcessResult((), 0, output, "")),
                ):
                    with self.assertRaisesRegex(UserFacingError, "媒体容器不受支持"):
                        await service._probe(Path("source.m3u8"))

    async def test_probe_rejects_malformed_media_shapes(self):
        service = self.service()
        for payload in (
            {"format": "invalid", "streams": []},
            {"format": {"duration": "1"}, "streams": {}},
            {"format": {"duration": "1"}, "streams": ["audio"]},
        ):
            with self.subTest(payload=payload):
                output = json.dumps(payload)
                with patch(
                    "core.service.run_process",
                    new=AsyncMock(return_value=ProcessResult((), 0, output, "")),
                ):
                    with self.assertRaisesRegex(UserFacingError, "音轨信息无效"):
                        await service._probe(Path("track.wav"))

    async def test_conversion_enforces_wav_limit_while_running(self):
        service = self.service()
        song = Song(
            "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "source.webm").write_bytes(b"audio" * 20)
            service._run_ytdlp = AsyncMock()
            service._probe = AsyncMock(
                return_value={"duration": 1, "streams": [{"codec_type": "audio"}]}
            )
            cap = service.settings.max_wav_size_mb * 1024 * 1024
            with patch(
                "core.service.run_process",
                new=AsyncMock(
                    side_effect=ProcessOutputLimit(
                        "conversion overflow", actual_bytes=cap + 1, max_bytes=cap
                    )
                ),
            ) as process:
                with self.assertRaisesRegex(UserFacingError, "WAV 文件超过上限"):
                    await service._download_resolved(song, job, on_stage=None, on_output_line=None)
        self.assertEqual(process.call_args.kwargs["monitor_directory"], job)
        self.assertEqual(process.call_args.kwargs["monitor_pattern"], "record.wav")
        self.assertEqual(process.call_args.kwargs["max_directory_size_bytes"], cap)
        args = process.call_args.args[0]
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file,pipe")
        self.assertLess(args.index("-protocol_whitelist"), args.index("-i"))
        formats = set(args[args.index("-format_whitelist") + 1].split(","))
        self.assertTrue({"mov", "matroska", "webm", "mp3", "wav"}.issubset(formats))
        self.assertFalse({"hls", "concat", "dash", "sdp"} & formats)
        self.assertLess(args.index("-format_whitelist"), args.index("-i"))

    async def test_conversion_runner_stops_a_growing_wav_before_completion(self):
        service = self.service()
        service.settings = replace(
            service.settings, max_wav_size_mb=1, conversion_timeout_seconds=5
        )
        song = Song(
            "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        cap = service.settings.max_wav_size_mb * 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            (job / "source.webm").write_bytes(b"audio" * 20)
            service._run_ytdlp = AsyncMock()
            service._probe = AsyncMock(
                return_value={"duration": 1, "streams": [{"codec_type": "audio"}]}
            )

            async def controlled_conversion(_args, **kwargs):
                return await run_process(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys, time; "
                        "Path('record.wav').write_bytes(b'x' * int(sys.argv[1])); "
                        "time.sleep(30)",
                        str(cap + 1),
                    ],
                    **kwargs,
                )

            with patch("core.service.run_process", side_effect=controlled_conversion):
                with self.assertRaisesRegex(UserFacingError, "WAV 文件超过上限"):
                    await service._download_resolved(song, job, on_stage=None, on_output_line=None)
            self.assertEqual(service._probe.await_count, 1)

    async def test_converted_wav_unknown_or_excessive_duration_is_rejected(self):
        service = self.service()
        song = Song(
            "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
        )
        stream = {
            "codec_type": "audio",
            "codec_name": "pcm_s16le",
            "sample_rate": "16000",
            "channels": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)

            async def download(*args, **kwargs):
                (job / "source.webm").write_bytes(b"audio" * 20)

            async def convert(*args, **kwargs):
                (job / "record.wav").write_bytes(b"RIFF" + b"x" * 60)

            service._run_ytdlp = AsyncMock(side_effect=download)
            for output_duration in (None, 999):
                service._probe = AsyncMock(
                    side_effect=[
                        {"duration": 1, "streams": [stream]},
                        {"duration": output_duration, "streams": [stream]},
                    ]
                )
                with patch("core.service.run_process", new=AsyncMock(side_effect=convert)):
                    with self.assertRaisesRegex(UserFacingError, "WAV 时长"):
                        await service._download_resolved(
                            song, job, on_stage=None, on_output_line=None
                        )

    def test_wav_format_rejects_stereo_wrong_sample_rate_and_codec(self):
        valid = {
            "codec_type": "audio",
            "codec_name": "pcm_s16le",
            "sample_rate": "16000",
            "channels": 1,
        }
        self.assertIsNotNone(YouTubeMusicService._validated_wav_stream({"streams": [valid]}))
        for key, value in (("channels", 2), ("sample_rate", "48000"), ("codec_name", "mp3")):
            self.assertIsNone(
                YouTubeMusicService._validated_wav_stream({"streams": [{**valid, key: value}]})
            )
        for channels in ("invalid", True, {}, float("inf"), 1.5):
            with self.subTest(channels=channels):
                self.assertIsNone(
                    YouTubeMusicService._validated_wav_stream(
                        {"streams": [{**valid, "channels": channels}]}
                    )
                )
        for streams in ({}, ["audio"], [None]):
            self.assertIsNone(YouTubeMusicService._validated_wav_stream({"streams": streams}))

    def test_proxy_errors_hide_socks_credentials(self):
        value = YouTubeMusicService._redact_error(
            "socks5://user:secret@proxy.test password=private"
        )
        self.assertNotIn("secret@", value)
        self.assertNotIn("private", value)
