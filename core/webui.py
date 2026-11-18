"""Small view-only API surface, using the documented AstrBot web helpers."""

from __future__ import annotations

import asyncio
import functools
import json
import re
from dataclasses import asdict

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

from .maintenance import MaintenanceUnavailable, run_blocking
from .persistence import strict_json_loads
from .queue import MAX_BATCH_SIZE, pending_queue_summary
from .service import UserFacingError
from .utils import safe_text


def web_endpoint(method):
    @functools.wraps(method)
    async def wrapped(self):
        if not self._running or self._terminating:
            return error_response("插件尚未就绪或已停用。", status_code=503)
        username = request.username
        # The host's plugin-scope dependency authenticates this principal.
        if not isinstance(username, str) or not username:
            return error_response("请从已登录的 AstrBot 控制台打开插件。", status_code=401)
        try:
            async with self.operation():
                return await method(self, username)
        except asyncio.CancelledError:
            raise
        except MaintenanceUnavailable:
            return error_response("依赖维护中或插件已停用。", status_code=503)
        except (UserFacingError, ValueError) as exc:
            return error_response(safe_text(str(exc), 240), status_code=400)
        except Exception as exc:
            logger.error("[YouTubeMusic] Web 请求失败：%s", type(exc).__name__)
            return error_response("操作未完成，请查看 AstrBot 日志。", status_code=500)

    return wrapped


class WebUIMixin:
    def _init_webui(self):
        self._event_queues: set[asyncio.Queue] = set()
        self._revision = 0
        self._dependency_update_task = None
        self.dependency_manager.on_update = lambda state: self._broadcast()
        routes = [
            ("status", self.web_status, ["GET"]),
            ("events", self.web_events, ["GET"]),
            ("search", self.web_search, ["GET"]),
            ("queue/add", self.web_enqueue, ["POST"]),
            ("queue/batch", self.web_batch_enqueue, ["POST"]),
            ("queue/action", self.web_queue_action, ["POST"]),
            ("library", self.web_library, ["GET"]),
            ("library/action", self.web_library_action, ["POST"]),
            ("maintenance", self.web_maintenance, ["GET"]),
            ("cache/cleanup", self.web_cache_cleanup, ["POST"]),
            ("dependencies/update", self.web_dependency_update, ["POST"]),
            ("dependencies/cancel", self.web_dependency_cancel, ["POST"]),
        ]
        for endpoint, handler, methods in routes:
            self.context.register_web_api(
                f"/astrbot_plugin_youtube_music/{endpoint}",
                handler,
                methods,
                f"YouTube Music {endpoint}",
            )

    def _broadcast(self):
        if self._terminating:
            return
        self._revision += 1
        for queue in self._event_queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(self._revision)

    def _close_events(self):
        for queue in self._event_queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
        self._event_queues.clear()

    async def _body(self, fields):
        length = request.headers.get("content-length")
        if length is not None:
            length = length.strip()
            if not re.fullmatch(r"[0-9]{1,12}", length):
                raise UserFacingError("无效的请求体长度。")
            if int(length) > 8192:
                raise UserFacingError("请求体过大。")
        raw = await request.body()
        if len(raw) > 8192:
            raise UserFacingError("请求体过大。")
        try:
            body = strict_json_loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise UserFacingError("请求必须为 JSON 对象。") from None
        if not isinstance(body, dict):
            raise UserFacingError("请求必须为 JSON 对象。")
        if body.keys() - fields:
            raise UserFacingError("请求包含不支持的字段。")
        return body

    @web_endpoint
    async def web_status(self, username):
        payload = self.state.snapshot()
        sessions = [
            {"key": key, "label": session["label"], "count": len(payload["queues"].get(key, []))}
            for key, session in payload["sessions"].items()
            if self._group_allowed(session["group_id"])
        ]
        queues = {
            session["key"]: {
                "items": payload["queues"].get(session["key"], []),
                "paused": session["key"] in payload["paused"],
                "current": self._current.get(session["key"]),
                "error": self._errors.get(session["key"], ""),
                "summary": pending_queue_summary(payload["queues"].get(session["key"], [])),
            }
            for session in sessions
        }
        update = self.dependency_manager.state
        return json_response(
            {
                "sessions": sessions,
                "queues": queues,
                "revision": self._revision,
                "limits": {
                    "queue": self.settings.max_session_queue,
                    "duration": self.settings.max_duration_seconds,
                    "batch": MAX_BATCH_SIZE,
                },
                "dependency_update": {
                    key: update.get(key)
                    for key in (
                        "status",
                        "phase",
                        "component",
                        "progress",
                        "message",
                        "recovery_required",
                        "recovery_directory",
                        "recovery_targets",
                        "downloaded_bytes",
                        "total_bytes",
                        "speed_bytes_per_second",
                        "started_at",
                        "finished_at",
                        "updated",
                    )
                },
                "active_jobs": self._limiter.active,
            }
        )

    @web_endpoint
    async def web_events(self, username):
        if len(self._event_queues) >= 20:
            return error_response("实时连接数已达上限。", status_code=429)

        async def events():
            if self._terminating or len(self._event_queues) >= 20:
                return
            queue = asyncio.Queue(maxsize=1)
            self._event_queues.add(queue)
            try:
                yield f"data: {json.dumps({'revision': self._revision})}\n\n"
                while not self._terminating:
                    try:
                        revision = await asyncio.wait_for(queue.get(), 20)
                    except TimeoutError:
                        yield 'data: {"heartbeat": true}\n\n'
                        continue
                    if revision is None or self._terminating:
                        break
                    yield f"data: {json.dumps({'revision': revision})}\n\n"
            finally:
                self._event_queues.discard(queue)

        return stream_response(events())

    @web_endpoint
    async def web_search(self, username):
        query = request.query.get("q", "")
        if not isinstance(query, str) or not query.strip():
            raise UserFacingError("请输入歌曲或艺人。")
        songs = await self.search(("webui", username), query)
        return json_response({"songs": [asdict(song) for song in songs]})

    @web_endpoint
    async def web_enqueue(self, username):
        body = await self._body({"session", "source", "video_id", "index"})
        if not isinstance(body.get("session"), str):
            raise UserFacingError("请选择会话。")
        source = body.get("source", "search")
        if not isinstance(source, str) or source not in {"search", "favorites", "history"}:
            raise UserFacingError("歌曲来源无效。")
        owner = ("webui", username)
        if source in {"favorites", "history"}:
            songs = await run_blocking(
                self.library.songs_for_source, owner, source, [body.get("video_id")]
            )
            song = songs[0]
        else:
            song = self.candidate(owner, body.get("index"))
            if body.get("video_id") != song.video_id:
                raise UserFacingError("搜索结果已更新，请重新选择。")
        position = await self.enqueue(body.get("session"), song, owner, username)
        return json_response({"position": position})

    @web_endpoint
    async def web_batch_enqueue(self, username):
        body = await self._body({"session", "source", "video_ids"})
        if not isinstance(body.get("session"), str) or not body["session"]:
            raise UserFacingError("请选择会话。")
        owner = ("webui", username)
        songs = await run_blocking(
            self.library.songs_for_source, owner, body.get("source"), body.get("video_ids")
        )
        result = await self.enqueue_many(
            body["session"], songs, owner, username, skip_existing=True
        )
        return json_response(result)

    @web_endpoint
    async def web_queue_action(self, username):
        body = await self._body({"session", "action", "entry_id", "direction"})
        key = body.get("session")
        if not isinstance(key, str):
            raise UserFacingError("请选择会话。")
        if not all(
            isinstance(body.get(field, ""), str) for field in ("action", "entry_id", "direction")
        ):
            raise UserFacingError("队列参数无效。")
        await self.queue_action(
            key,
            body.get("action"),
            admin=True,
            entry_id=body.get("entry_id", ""),
            direction=body.get("direction", ""),
        )
        return json_response({"ok": True})

    @web_endpoint
    async def web_library(self, username):
        owner = ("webui", username)
        favorites = await run_blocking(self.library.favorites, owner)
        history = await run_blocking(self.library.history, owner)
        return json_response(
            {"favorites": [asdict(song) for song in favorites], "history": history}
        )

    @web_endpoint
    async def web_library_action(self, username):
        body = await self._body({"action", "source", "index", "video_id"})
        owner = ("webui", username)
        action = body.get("action")
        if action != "favorite" and "source" in body:
            raise UserFacingError("此操作不支持歌曲来源参数。")
        if action == "favorite":
            source = body.get("source", "search")
            if source == "history":
                if "index" in body:
                    raise UserFacingError("历史收藏使用歌曲 ID，不支持搜索编号。")
                await run_blocking(self.library.favorite_from_history, owner, body.get("video_id"))
            elif source == "search":
                song = self.candidate(owner, body.get("index"))
                if body.get("video_id") != song.video_id:
                    raise UserFacingError("搜索结果已更新，请重新选择。")
                await run_blocking(self.library.add_favorite, owner, song)
            else:
                raise UserFacingError("歌曲来源无效。")
        elif action == "remove":
            video = body.get("video_id")
            if not isinstance(video, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video):
                raise UserFacingError("无效的视频 ID。")
            await run_blocking(self.library.remove_favorite, owner, video)
        elif action == "clear_history":
            await run_blocking(self.library.clear_history, owner)
        else:
            raise UserFacingError("无效的收藏操作。")
        return json_response({"ok": True})

    @web_endpoint
    async def web_maintenance(self, username):
        dependencies = await self.dependency_manager.inspect()
        entries = await run_blocking(self.audio_cache.list_entries)
        return json_response(
            {
                "dependencies": dependencies,
                "cache": {
                    "items": len(entries),
                    "bytes": sum(row["size_bytes"] for row in entries),
                },
            }
        )

    @web_endpoint
    async def web_cache_cleanup(self, username):
        await self._body(set())
        return json_response(await run_blocking(self.audio_cache.clear_unprotected))

    @web_endpoint
    async def web_dependency_update(self, username):
        body = await self._body({"component", "confirmed"})
        component = body.get("component")
        if (
            not isinstance(component, str)
            or component not in {"yt-dlp", "Deno", "FFmpeg", "all"}
            or body.get("confirmed") is not True
        ):
            raise UserFacingError("请明确确认允许的依赖更新。")
        if self._dependency_update_task and not self._dependency_update_task.done():
            raise UserFacingError("已有更新任务。")

        async def update():
            logger.info(
                "[YouTubeMusic] 官方身份 %s 请求更新 %s", safe_text(username, 80), component
            )
            try:
                await self.dependency_manager.update(component)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[YouTubeMusic] 依赖更新失败：%s", type(exc).__name__)
            finally:
                self._broadcast()

        self._dependency_update_task = asyncio.create_task(
            update(), name="youtube-dependency-update"
        )
        return json_response({"started": True})

    @web_endpoint
    async def web_dependency_cancel(self, username):
        await self._body(set())
        if self._dependency_update_task and not self._dependency_update_task.done():
            self._dependency_update_task.cancel()
            await asyncio.gather(self._dependency_update_task, return_exceptions=True)
        return json_response({"ok": True})
