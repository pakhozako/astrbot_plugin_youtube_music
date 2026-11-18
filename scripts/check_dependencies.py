"""Opt-in live updater check in a disposable directory, never production data."""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.dependency_manager import DependencyManager  # noqa: E402
from core.process import run_process  # noqa: E402
from core.settings import Settings  # noqa: E402


async def check():
    with tempfile.TemporaryDirectory(prefix="youtube-music-dependency-check-") as directory:
        root = Path(directory)
        settings = Settings.from_config({"dependency_update_timeout_seconds": 900}, data_dir=root)
        manager = DependencyManager(root, settings)
        last = None

        def progress(state):
            nonlocal last
            percentage = state.get("progress")
            key = (state["phase"], int(percentage // 10) if percentage is not None else None)
            if key != last:
                last = key
                print(
                    json.dumps(
                        {
                            "status": state["status"],
                            "phase": state["phase"],
                            "progress": percentage,
                            "downloaded_bytes": state["downloaded_bytes"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        manager.on_update = progress
        try:
            result = await manager.update("all")
            health = await manager.inspect()
            audio = root / "smoke.wav"
            await run_process(
                [
                    str(settings.ffmpeg_path),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(audio),
                ],
                timeout=20,
                label="FFmpeg 本地音频验证",
            )
            probe = await run_process(
                [
                    str(settings.ffprobe_path),
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(audio),
                ],
                timeout=20,
                label="ffprobe 本地音频验证",
            )
            media = json.loads(probe.stdout)
            stream = media["streams"][0]
            audio_ok = (
                stream["codec_name"] == "pcm_s16le"
                and stream["sample_rate"] == "16000"
                and stream["channels"] == 1
                and 0.99 <= float(media["format"]["duration"]) <= 1.01
            )
            print(
                json.dumps(
                    {
                        "result": result,
                        "health": health,
                        "audio_smoke": {
                            "ok": audio_ok,
                            "codec": stream["codec_name"],
                            "sample_rate": stream["sample_rate"],
                            "channels": stream["channels"],
                            "duration": media["format"]["duration"],
                            "bytes": audio.stat().st_size,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if health["all_ready"] and audio_ok else 1
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "error": type(exc).__name__,
                        "state": manager.state,
                        "isolated_test": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live",
        action="store_true",
        help="Download and run verified upstream binaries in disposable data",
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Explicit --live is required")
    raise SystemExit(asyncio.run(check()))
