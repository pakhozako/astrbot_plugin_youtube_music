"""Use real AstrBot Star, FunctionTool and public Web request/response helpers."""

import asyncio
import copy
import importlib
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault(
    "ASTRBOT_ROOT", str(Path(tempfile.gettempdir()) / "youtube-music-runtime-tests")
)

from astrbot.api.star import Context, StarTools  # noqa: E402 - set test data directory before AstrBot import
from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

package = types.ModuleType("youtube_music_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
module = importlib.import_module("youtube_music_test.main")
Plugin = module.YouTubeMusicPlugin
Song = importlib.import_module("youtube_music_test.core.utils").Song


def workflow_song(index, duration=1):
    video = f"track{index:06d}"
    return Song(
        video, f"Track {index}", "Artist", duration, f"https://www.youtube.com/watch?v={video}"
    )


class QueueSummaryTests(unittest.TestCase):
    def test_pending_summary_counts_only_known_positive_integer_durations(self):
        rows = [
            {"song": {"duration": value}}
            for value in (60, 120, 3600, None, True, False, 0, -1, 1.5, "60", float("inf"))
        ]
        rows.extend(({}, {"song": None}, None))
        before = copy.deepcopy(rows)
        self.assertEqual(
            module.pending_queue_summary(rows),
            {"count": 14, "duration_seconds": 3780, "unknown_duration_count": 11},
        )
        self.assertEqual(rows, before)
        self.assertEqual(
            module.pending_queue_summary([]),
            {"count": 0, "duration_seconds": 0, "unknown_duration_count": 0},
        )
        self.assertEqual(module.MAX_BATCH_SIZE, 20)


class ChatEvent:
    def __init__(self, sender="456", group="123", admin=False, platform="qq", message=""):
        self.sender, self.group, self.admin, self.platform = sender, group, admin, platform
        self.message_str = message
        self.unified_msg_origin = (
            f"{platform}:GroupMessage:{group}" if group else f"{platform}:FriendMessage:{sender}"
        )

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return f"User {self.sender}"

    def get_group_id(self):
        return self.group

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return "aiocqhttp"

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text


def web_request(username="admin", body=None, query="", method="POST"):
    scope = {
        "type": "http",
        "method": method,
        "scheme": "http",
        "http_version": "1.1",
        "path": "/test",
        "query_string": query.encode(),
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    receive = AsyncMock(
        return_value={
            "type": "http.request",
            "body": json.dumps(body or {}).encode(),
            "more_body": False,
        }
    )
    return PluginRequest(Request(scope, receive), plugin_name=module.PLUGIN_NAME, username=username)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        context = Context.__new__(Context)
        context.registered_web_apis = []
        tools = []
        context.provider_manager = SimpleNamespace(
            llm_tools=SimpleNamespace(
                func_list=tools,
                remove_func=lambda name: tools.__setitem__(
                    slice(None), [tool for tool in tools if tool.name != name]
                ),
            )
        )
        context.send_message = AsyncMock(return_value=True)
        self.context = context
        self.config = {
            "queue_admin_user_ids": "456",
            "request_cooldown_seconds": 0,
            "send_concurrency": 1,
        }
        with patch.object(StarTools, "get_data_dir", return_value=self.root):
            self.plugin = Plugin(context, self.config)
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)
        self.event = ChatEvent()
        self.key, self.owner, self.admin = await self.plugin._event_identity(self.event)
        self.song = Song(
            "abcdefghijk", "Track", "Artist", 1, "https://www.youtube.com/watch?v=abcdefghijk"
        )

    async def add(self, owner=None, song=None):
        with patch.object(self.plugin, "_start_worker"):
            return await self.plugin.enqueue(
                self.key, song or self.song, owner or self.owner, "User"
            )

    async def wait_for(self, predicate):
        for _ in range(400):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("Timed out waiting for worker")

    async def fail_queued_head(self, *, submitted=False, pause_save_fails=False):
        row = self.plugin.state.snapshot("queues", self.key)[0]
        self.context.send_message.side_effect = None if submitted else TimeoutError()
        self.plugin.service.download_and_convert = AsyncMock(
            side_effect=lambda song, *_args, **_kwargs: (self.root / "track.wav", song, {})
        )
        persistence = importlib.import_module("youtube_music_test.core.persistence")
        write = persistence.atomic_write

        def fail_state_save(path, payload):
            if path == self.plugin.state.path:
                removed_head = not any(
                    item["id"] == row["id"] for item in payload["queues"].get(self.key, [])
                )
                if (
                    submitted
                    and removed_head
                    or (pause_save_fails and self.key in payload["paused"])
                ):
                    raise OSError("state storage unavailable")
            return write(path, payload)

        with patch.object(persistence, "atomic_write", side_effect=fail_state_save):
            self.plugin._start_worker(self.key)
            await self.wait_for(lambda: self.key not in self.plugin._workers)
        self.assertEqual(self.plugin._error_entry_ids[self.key], row["id"])
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertFalse(self.plugin.library.history(self.owner))
        return row, self.plugin._errors[self.key]

    async def test_real_registration_has_only_core_tools_and_routes(self):
        self.assertEqual(len(self.context.registered_web_apis), 12)
        self.assertEqual(
            [tool.name for tool in self.context.provider_manager.llm_tools.func_list],
            ["youtube_music"],
        )
        self.assertFalse(hasattr(self.plugin, "control_state"))
        self.assertFalse(hasattr(self.plugin, "named_playlists"))

    async def test_stopped_routes_reject_reads_and_writes(self):
        await self.add()
        before = self.plugin.state.path.read_bytes()
        await self.plugin.terminate()
        with bind_request_context(web_request(body={"session": self.key, "action": "clear"})):
            response = await self.plugin.web_queue_action()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.plugin.state.path.read_bytes(), before)

    async def test_repeated_initialization_keeps_the_live_stores_and_cache(self):
        await self.add()
        stores = (self.plugin.state, self.plugin.library, self.plugin.audio_cache)
        before = self.plugin.state.path.read_bytes()
        protected = {"abcdefghijk"}
        self.plugin.audio_cache.protected_video_ids = protected
        with (
            patch.object(self.plugin, "_load_stores") as load,
            patch.object(self.plugin.audio_cache, "cleanup") as cleanup,
            patch.object(module, "cleanup_stale_owned_job_dirs") as cleanup_jobs,
        ):
            await self.plugin.initialize()
            await self.plugin.initialize()
        load.assert_not_called()
        cleanup.assert_not_called()
        cleanup_jobs.assert_not_called()
        for attribute, store in zip(("state", "library", "audio_cache"), stores, strict=True):
            self.assertIs(getattr(self.plugin, attribute), store)
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertIs(self.plugin.service.audio_cache, stores[2])
        self.assertEqual(self.plugin.audio_cache.protected_video_ids, protected)
        self.assertTrue(self.plugin._running)
        self.assertFalse(self.plugin._requests)

    async def test_concurrent_initialization_loads_stores_only_once(self):
        started, release = threading.Event(), threading.Event()
        with patch.object(StarTools, "get_data_dir", return_value=self.root / "concurrent-init"):
            other = Plugin(self.context, self.config)
        self.addAsyncCleanup(other.terminate)
        load = other._load_stores

        def slow_load():
            started.set()
            release.wait(3)
            return load()

        with patch.object(other, "_load_stores", side_effect=slow_load) as mocked_load:
            first = asyncio.create_task(other.initialize())
            second = asyncio.create_task(other.initialize())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await self.wait_for(lambda: len(other._requests) == 2)
                self.assertTrue(other._initialize_lock.locked())
                self.assertEqual(mocked_load.call_count, 1)
                self.assertFalse(first.done())
                self.assertFalse(second.done())
            finally:
                release.set()
                await asyncio.gather(first, second)
        self.assertEqual(mocked_load.call_count, 1)
        self.assertFalse(other._requests)
        self.assertFalse(other._initialize_lock.locked())
        self.assertIs(other.service.audio_cache, other.audio_cache)
        self.assertTrue(other._running)

    async def test_termination_cancels_initialization_and_its_lock_waiters(self):
        started, release = threading.Event(), threading.Event()
        with patch.object(StarTools, "get_data_dir", return_value=self.root / "stopping-init"):
            other = Plugin(self.context, self.config)
        self.addAsyncCleanup(other.terminate)
        load = other._load_stores

        def slow_load():
            started.set()
            release.wait(3)
            return load()

        with patch.object(other, "_load_stores", side_effect=slow_load) as mocked_load:
            first = asyncio.create_task(other.initialize())
            second = asyncio.create_task(other.initialize())
            stopping = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await self.wait_for(lambda: len(other._requests) == 2)
                stopping = asyncio.create_task(other.terminate())
                await self.wait_for(second.done)
                self.assertTrue(second.cancelled())
                self.assertFalse(stopping.done())
                self.assertEqual(mocked_load.call_count, 1)
            finally:
                release.set()
                results = await asyncio.gather(first, second, return_exceptions=True)
                if stopping is not None:
                    await stopping
        self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
        self.assertFalse(other._requests)
        self.assertFalse(other._initialize_lock.locked())
        self.assertFalse(other._running)
        with self.assertRaises(module.UserFacingError):
            await other.initialize()

    async def test_termination_waits_for_initialization_and_cannot_restart_old_instance(self):
        started, release = threading.Event(), threading.Event()
        with patch.object(StarTools, "get_data_dir", return_value=self.root / "initializing"):
            other = Plugin(self.context, self.config)
        self.addAsyncCleanup(other.terminate)
        load = other._load_stores

        def slow_load():
            started.set()
            release.wait(3)
            return load()

        with patch.object(other, "_load_stores", side_effect=slow_load):
            initializing = asyncio.create_task(other.initialize())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                stopping = asyncio.create_task(other.terminate())
                await asyncio.sleep(0.02)
                self.assertFalse(stopping.done())
            finally:
                release.set()
            await stopping
            with self.assertRaises(asyncio.CancelledError):
                await initializing
        self.assertFalse(other._running)
        self.assertFalse(other._requests)
        with self.assertRaises(module.UserFacingError):
            await other.initialize()

    async def test_queue_worker_selects_head_only_after_queue_mutation(self):
        await self.add()
        second = Song(
            "bcdefghijkl", "Second", "Artist", 1, "https://www.youtube.com/watch?v=bcdefghijkl"
        )
        await self.add(song=second)
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", second, {})
        )
        async with self.plugin._queue_lock:
            self.plugin._start_worker(self.key)
            await asyncio.sleep(0.02)
            self.assertNotIn(self.key, self.plugin._current)
            await module.run_blocking(
                self.plugin.state.update, lambda data: data["queues"][self.key].pop(0)
            )
        await self.wait_for(lambda: self.plugin.service.download_and_convert.await_count == 1)
        self.assertEqual(self.plugin.service.download_and_convert.call_args.args[0], second)
        await self.plugin.queue_action(self.key, "pause", admin=True)

    async def test_web_json_unknown_fields_are_rejected_without_mutation(self):
        await self.add()
        before = self.plugin.state.path.read_bytes()
        cases = [
            (self.plugin.web_queue_action, {"session": self.key, "action": "clear", "extra": 1}),
            (
                self.plugin.web_enqueue,
                {"session": self.key, "index": 1, "url": "https://evil.test"},
            ),
            (self.plugin.web_library_action, {"action": "clear_history", "owner": "someone"}),
            (
                self.plugin.web_dependency_update,
                {"component": "all", "confirmed": True, "url": "x"},
            ),
        ]
        for handler, body in cases:
            with bind_request_context(web_request(body=body)):
                response = await handler()
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.plugin.state.path.read_bytes(), before)

    async def test_web_identity_fails_closed(self):
        for username, code in ((None, 401), ("", 401), ("operator", 200), ("api_key_1", 200)):
            with bind_request_context(web_request(username=username)):
                response = await self.plugin.web_status()
            self.assertEqual(response.status_code, code)

    async def test_official_principals_have_separate_libraries(self):
        await module.run_blocking(
            self.plugin.library.add_favorite, ("webui", "operator"), self.song
        )
        for username, count in (("operator", 1), ("other", 0)):
            with bind_request_context(web_request(username=username, query="username=operator")):
                response = await self.plugin.web_library()
            self.assertEqual(len(json.loads(response.body)["favorites"]), count)

    async def test_unexpected_chat_error_does_not_expose_secrets(self):
        with patch.object(self.plugin, "dispatch_music", side_effect=RuntimeError("token=secret")):
            replies = [reply async for reply in self.plugin.song_command(self.event)]
        self.assertEqual(replies, ["请求未完成，请查看插件日志。"])

    async def test_admission_failures_are_expected_chat_and_web_errors(self):
        queue = importlib.import_module("youtube_music_test.core.queue")
        maintenance = importlib.import_module("youtube_music_test.core.maintenance")
        for error_type, message, code in (
            (queue.QueueFull, "处理任务已满，请稍后重试。", 400),
            (queue.QueueWaitTimeout, "等待处理超时，请稍后重试。", 400),
            (maintenance.MaintenanceUnavailable, "依赖正在维护，等待超时；请稍后重试。", 503),
        ):
            with self.subTest(error_type=error_type):
                self.event.message_str = "/点歌 Track"
                with patch.object(self.plugin._limiter, "slot", side_effect=error_type(message)):
                    replies = [reply async for reply in self.plugin.song_command(self.event)]
                    self.assertEqual(replies, [message])
                    with bind_request_context(web_request(query="q=Track")):
                        response = await self.plugin.web_search()
                self.assertEqual(response.status_code, code)
                self.assertIn(
                    message if code == 400 else "依赖维护中", str(json.loads(response.body))
                )
                self.assertFalse(self.plugin._search_busy)

    async def test_stale_web_search_cards_cannot_enqueue_or_favorite_another_song(self):
        self.plugin.service.search = AsyncMock(return_value=[self.song])
        await self.plugin.search(("webui", "admin"), "new search")
        for handler, body in (
            (self.plugin.web_enqueue, {"session": self.key, "index": 1, "video_id": "bcdefghijkl"}),
            (
                self.plugin.web_library_action,
                {"action": "favorite", "index": 1, "video_id": "bcdefghijkl"},
            ),
        ):
            with bind_request_context(web_request(body=body)):
                response = await handler()
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.plugin.state.snapshot()["queues"].get(self.key, []))
        self.assertFalse(self.plugin.library.favorites(("webui", "admin")))

    async def test_sse_subscription_is_lazy_and_stops_on_termination(self):
        with bind_request_context(web_request()):
            unopened = await self.plugin.web_events()
        self.assertFalse(self.plugin._event_queues)
        await unopened.body_iterator.aclose()
        self.assertFalse(self.plugin._event_queues)
        with bind_request_context(web_request()):
            response = await self.plugin.web_events()
        iterator = response.body_iterator
        await anext(iterator)
        self.assertEqual(len(self.plugin._event_queues), 1)
        await self.plugin.terminate()
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)
        self.assertFalse(self.plugin._event_queues)

    async def test_members_can_remove_only_their_own_pending_items(self):
        await self.add()
        row = self.plugin.state.snapshot()["queues"][self.key][0]
        with self.assertRaises(module.UserFacingError):
            await self.plugin.queue_action(
                self.key, "remove", owner=(self.key, "999"), entry_id=row["id"]
            )
        with self.assertRaises(module.UserFacingError):
            await self.plugin.queue_action(self.key, "clear", owner=self.owner)
        await self.plugin.queue_action(self.key, "remove", owner=self.owner, entry_id=row["id"])
        self.assertFalse(self.plugin.state.snapshot()["queues"][self.key])

    async def test_duplicate_and_queue_capacity_do_not_write_invalid_state(self):
        await self.add()
        before = self.plugin.state.path.read_bytes()
        with self.assertRaises(module.UserFacingError):
            await self.add()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        from dataclasses import replace

        self.plugin.settings = replace(self.plugin.settings, max_session_queue=1)
        second = Song(
            "bcdefghijkl", "Second", "Artist", 1, "https://www.youtube.com/watch?v=bcdefghijkl"
        )
        with self.assertRaises(module.UserFacingError):
            await self.add(song=second)
        self.assertEqual(self.plugin.state.path.read_bytes(), before)

    async def test_single_enqueue_keeps_integer_result_and_shared_admission(self):
        result = {
            "added": 1,
            "positions": [{"video_id": self.song.video_id, "position": 3}],
            "skipped": [],
            "queue_size": 3,
        }
        with patch.object(self.plugin, "enqueue_many", new=AsyncMock(return_value=result)) as many:
            position = await self.plugin.enqueue(self.key, self.song, self.owner, "User")
        self.assertEqual(position, 3)
        many.assert_awaited_once_with(self.key, [self.song], self.owner, "User")

    async def test_known_session_uses_only_a_scoped_snapshot(self):
        with patch.object(self.plugin.state, "snapshot", wraps=self.plugin.state.snapshot) as read:
            session = self.plugin._known_session(self.key)
            read.assert_called_once_with("sessions", self.key)
        self.assertEqual(session["group_id"], self.event.group)
        with patch.object(self.plugin.state, "snapshot") as read:
            with self.assertRaises(module.UserFacingError):
                self.plugin._known_session([])
            read.assert_not_called()

    async def test_batch_boundary_order_has_one_transaction_start_and_broadcast(self):
        songs = [workflow_song(index) for index in range(module.MAX_BATCH_SIZE, 0, -1)]
        with (
            patch.object(self.plugin.state, "update", wraps=self.plugin.state.update) as update,
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
            patch(
                "youtube_music_test.core.persistence.atomic_write",
                wraps=importlib.import_module("youtube_music_test.core.persistence").atomic_write,
            ) as write,
        ):
            result = await self.plugin.enqueue_many(
                self.key, songs, self.owner, "User https://private.test/?token=secret"
            )
        self.assertEqual(
            result,
            {
                "added": 20,
                "positions": [
                    {"video_id": song.video_id, "position": index}
                    for index, song in enumerate(songs, 1)
                ],
                "skipped": [],
                "queue_size": 20,
            },
        )
        update.assert_called_once()
        write.assert_called_once()
        start.assert_called_once_with(self.key)
        broadcast.assert_called_once_with()
        rows = self.plugin.state.snapshot("queues", self.key)
        self.assertEqual(
            [row["song"]["video_id"] for row in rows], [song.video_id for song in songs]
        )
        self.assertEqual(len({row["id"] for row in rows}), 20)
        self.assertEqual(len({row["added_at"] for row in rows}), 1)
        self.assertTrue(
            all(row["owner"] == self.plugin.library.owner_id(self.owner) for row in rows)
        )
        self.assertTrue(all(row["requested_by"] == "User [链接]" for row in rows))
        self.assertEqual(
            json.loads(self.plugin.state.path.read_text(encoding="utf-8"))["queues"][self.key],
            rows,
        )

    async def test_invalid_batch_inputs_are_rejected_before_any_transaction(self):
        before = self.plugin.state.path.read_bytes()
        cases = [
            (None, False),
            (iter([self.song]), False),
            ([], False),
            ([self.song, self.song], False),
            ([workflow_song(index) for index in range(module.MAX_BATCH_SIZE + 1)], False),
            ([self.song, None], False),
            ([self.song], "yes"),
            ([self.song, replace(workflow_song(1), duration=None)], False),
            ([self.song, replace(workflow_song(1), duration=True)], False),
            ([self.song, replace(workflow_song(1), duration=1.5)], False),
            ([self.song, replace(workflow_song(1), duration=181)], False),
            ([self.song, replace(workflow_song(1), title="Invalid\nTitle")], False),
            ([self.song, replace(workflow_song(1), url="https://untrusted.test/track")], False),
        ]
        with (
            patch.object(self.plugin.state, "update", wraps=self.plugin.state.update) as update,
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            for songs, skip_existing in cases:
                with self.subTest(songs=songs, skip_existing=skip_existing):
                    with self.assertRaises(module.UserFacingError):
                        await self.plugin.enqueue_many(
                            self.key, songs, self.owner, "User", skip_existing=skip_existing
                        )
            with self.assertRaises(module.UserFacingError):
                await self.plugin.enqueue_many(self.key, [self.song], ("owner", None), "User")
        update.assert_not_called()
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))

    async def test_batch_explicitly_skips_queued_songs_and_reports_actual_positions(self):
        await self.add()
        first, second = workflow_song(1), workflow_song(2)
        with (
            patch.object(self.plugin.state, "update", wraps=self.plugin.state.update) as update,
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            result = await self.plugin.enqueue_many(
                self.key, [first, self.song, second], self.owner, "User", skip_existing=True
            )
        self.assertEqual(
            result,
            {
                "added": 2,
                "positions": [
                    {"video_id": first.video_id, "position": 2},
                    {"video_id": second.video_id, "position": 3},
                ],
                "skipped": [self.song.video_id],
                "queue_size": 3,
            },
        )
        update.assert_called_once()
        start.assert_called_once_with(self.key)
        broadcast.assert_called_once_with()
        self.assertEqual(
            [row["song"]["video_id"] for row in self.plugin.state.snapshot("queues", self.key)],
            [self.song.video_id, first.video_id, second.video_id],
        )

    async def test_batch_all_existing_is_a_noop_even_after_a_lower_queue_limit(self):
        other = workflow_song(1)
        await self.add()
        await self.add(song=other)
        self.plugin.settings = replace(self.plugin.settings, max_session_queue=1)
        before = self.plugin.state.path.read_bytes()
        with (
            patch("youtube_music_test.core.persistence.atomic_write") as write,
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            result = await self.plugin.enqueue_many(
                self.key, [other, self.song], self.owner, "User", skip_existing=True
            )
        self.assertEqual(
            result,
            {
                "added": 0,
                "positions": [],
                "skipped": [other.video_id, self.song.video_id],
                "queue_size": 2,
            },
        )
        write.assert_not_called()
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)

    async def test_batch_without_skip_preserves_duplicate_error_and_entire_queue(self):
        await self.add()
        before = self.plugin.state.path.read_bytes()
        with patch.object(self.plugin, "_start_worker") as start:
            with self.assertRaisesRegex(module.UserFacingError, "这首歌已在队列中"):
                await self.plugin.enqueue_many(
                    self.key, [workflow_song(1), self.song], self.owner, "User"
                )
        start.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertEqual(len(self.plugin.state.snapshot("queues", self.key)), 1)

    async def test_batch_capacity_failure_does_not_commit_its_prefix(self):
        await self.add()
        self.plugin.settings = replace(self.plugin.settings, max_session_queue=2)
        before = self.plugin.state.path.read_bytes()
        with (
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            with self.assertRaisesRegex(module.UserFacingError, "整批未加入"):
                await self.plugin.enqueue_many(
                    self.key, [workflow_song(1), workflow_song(2)], self.owner, "User"
                )
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertEqual(len(self.plugin.state.snapshot("queues", self.key)), 1)

    async def test_batch_capacity_is_checked_against_the_transaction_snapshot(self):
        await self.add()
        self.plugin.settings = replace(self.plugin.settings, max_session_queue=2)
        existing, incoming = workflow_song(1), workflow_song(2)
        original = self.plugin.state.update

        def concurrent_update(mutate):
            original(
                lambda data: data["queues"][self.key].append(
                    {
                        "id": "f" * 32,
                        "song": asdict(existing),
                        "owner": self.plugin.library.owner_id(self.owner),
                        "requested_by": "Other writer",
                        "added_at": 1,
                    }
                )
            )
            return original(mutate)

        with (
            patch.object(self.plugin.state, "update", side_effect=concurrent_update),
            patch.object(self.plugin, "_start_worker") as start,
        ):
            with self.assertRaises(module.UserFacingError):
                await self.plugin.enqueue_many(self.key, [incoming], self.owner, "User")
        start.assert_not_called()
        self.assertEqual(
            [row["song"]["video_id"] for row in self.plugin.state.snapshot("queues", self.key)],
            [self.song.video_id, existing.video_id],
        )

    async def test_concurrent_batches_compete_for_capacity_without_partial_admission(self):
        await self.add()
        self.plugin.settings = replace(self.plugin.settings, max_session_queue=3)
        batches = [
            [workflow_song(1), workflow_song(2)],
            [workflow_song(3), workflow_song(4)],
        ]
        with (
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            results = await asyncio.gather(
                *(
                    self.plugin.enqueue_many(self.key, songs, self.owner, "User")
                    for songs in batches
                ),
                return_exceptions=True,
            )
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, module.UserFacingError) for result in results), 1)
        start.assert_called_once_with(self.key)
        broadcast.assert_called_once_with()
        accepted = next(index for index, result in enumerate(results) if isinstance(result, dict))
        self.assertEqual(
            [row["song"]["video_id"] for row in self.plugin.state.snapshot("queues", self.key)],
            [self.song.video_id, *(song.video_id for song in batches[accepted])],
        )

    async def test_batch_global_capacity_failure_preserves_all_sessions(self):
        self.plugin.settings = replace(self.plugin.settings, max_session_queue=50)
        owner = self.plugin.library.owner_id(self.owner)

        def fill_global_capacity(data):
            for index in range(1, 21):
                key = f"{index:024x}"
                data["sessions"][key] = {
                    "umo": f"qq:GroupMessage:{index}",
                    "label": f"QQ Group {index}",
                    "group_id": str(index),
                }
                data["queues"][key] = [
                    {
                        "id": f"{index * 50 + track:032x}",
                        "song": asdict(workflow_song(track)),
                        "owner": owner,
                        "requested_by": "User",
                        "added_at": 1,
                    }
                    for track in range(50)
                ]

        await module.run_blocking(self.plugin.state.update, fill_global_capacity)
        before = self.plugin.state.path.read_bytes()
        with (
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            with self.assertRaises(module.UserFacingError):
                await self.plugin.enqueue_many(
                    self.key, [self.song, workflow_song(100)], self.owner, "User"
                )
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertEqual(
            sum(len(rows) for rows in self.plugin.state.snapshot("queues").values()), 1000
        )
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))

    async def test_batch_fsync_failure_keeps_memory_disk_and_notifications_unchanged(self):
        before = self.plugin.state.path.read_bytes()
        with (
            patch("youtube_music_test.core.persistence.os.fsync", side_effect=OSError("disk full")),
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            with self.assertRaises(OSError):
                await self.plugin.enqueue_many(
                    self.key, [self.song, workflow_song(1)], self.owner, "User"
                )
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertFalse(list(self.root.glob(".*.tmp")))

    async def test_cancelled_batch_finishes_one_started_transaction_without_partial_admission(self):
        started, release = threading.Event(), threading.Event()
        original = self.plugin.state.update
        before = self.plugin.state.path.read_bytes()
        songs = [self.song, workflow_song(1), workflow_song(2)]

        def delayed_update(mutate):
            def delayed_mutate(data):
                result = mutate(data)
                started.set()
                release.wait(3)
                return result

            return original(delayed_mutate)

        with (
            patch.object(self.plugin.state, "update", side_effect=delayed_update),
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            task = asyncio.create_task(
                self.plugin.enqueue_many(self.key, songs, self.owner, "User")
            )
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                self.assertFalse(self.plugin.state.snapshot("queues", self.key))
                self.assertEqual(self.plugin.state.path.read_bytes(), before)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        start.assert_not_called()
        broadcast.assert_not_called()
        rows = self.plugin.state.snapshot("queues", self.key)
        self.assertEqual(
            [row["song"]["video_id"] for row in rows], [song.video_id for song in songs]
        )
        self.assertEqual(
            json.loads(self.plugin.state.path.read_text(encoding="utf-8"))["queues"][self.key],
            rows,
        )

    async def test_batch_rechecks_session_after_waiting_for_queue_lock(self):
        with (
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            async with self.plugin._queue_lock:
                task = asyncio.create_task(
                    self.plugin.enqueue_many(self.key, [self.song], self.owner, "User")
                )
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                await module.run_blocking(
                    self.plugin.state.update, lambda data: data["sessions"].pop(self.key)
                )
                before = self.plugin.state.path.read_bytes()
            with self.assertRaisesRegex(module.UserFacingError, "会话不存在"):
                await task
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)

    async def test_batch_rechecks_running_state_after_waiting_for_queue_lock(self):
        with (
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin, "_broadcast") as broadcast,
        ):
            async with self.plugin._queue_lock:
                task = asyncio.create_task(
                    self.plugin.enqueue_many(self.key, [self.song], self.owner, "User")
                )
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                await self.plugin.terminate()
                before = self.plugin.state.path.read_bytes()
            with self.assertRaisesRegex(module.UserFacingError, "停用"):
                await task
        start.assert_not_called()
        broadcast.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))

    async def test_queue_text_summary_excludes_an_already_submitted_current_song(self):
        await self.add()
        await self.add(song=workflow_song(1, duration=120))
        self.plugin._current[self.key] = {
            "row": {"song": asdict(workflow_song(2, duration=180))},
            "phase": "已提交语音",
            "submitted": True,
        }
        text = self.plugin.queue_text(self.key)
        self.assertIn("待播 2 首 · 总时长 2:01", text)
        self.assertIn("已提交语音", text)
        self.assertNotIn("总时长 5:01", text)

    async def test_group_queue_is_shared_but_adapters_are_isolated(self):
        second, _, _ = await self.plugin._event_identity(ChatEvent(sender="789"))
        other, _, _ = await self.plugin._event_identity(ChatEvent(platform="qq2"))
        self.assertEqual(self.key, second)
        self.assertNotEqual(self.key, other)

    async def test_failed_submission_keeps_head_and_pauses(self):
        await self.add()
        self.context.send_message.side_effect = RuntimeError("transport failed")
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {})
        )
        self.plugin._start_worker(self.key)
        await self.wait_for(lambda: self.key not in self.plugin._workers)
        payload = self.plugin.state.snapshot()
        self.assertEqual(len(payload["queues"][self.key]), 1)
        self.assertIn(self.key, payload["paused"])
        self.assertFalse(self.plugin.library.history(self.owner))

    async def test_unrelated_edits_preserve_ambiguous_submission_error(self):
        other_owner = (self.key, "999")
        await self.add()
        await self.add(song=workflow_song(1), owner=other_owner)
        await self.add(song=workflow_song(2), owner=other_owner)
        failed, error = await self.fail_queued_head()
        _, other, last = self.plugin.state.snapshot("queues", self.key)
        actions = [
            {"action": "move", "entry_id": failed["id"], "direction": "up", "admin": True},
            {"action": "move", "entry_id": other["id"], "direction": "up", "admin": True},
            {"action": "remove", "entry_id": last["id"], "owner": other_owner},
            {"action": "cancel_mine", "owner": other_owner},
        ]
        for action in actions:
            with self.subTest(action=action["action"]):
                await self.plugin.queue_action(self.key, **action)
                self.assertEqual(self.plugin._errors[self.key], error)
                self.assertEqual(self.plugin._error_entry_ids[self.key], failed["id"])
                self.assertIn(self.key, self.plugin.state.snapshot("paused"))
                self.assertNotIn(self.key, self.plugin._workers)
                self.assertIn(error, self.plugin.queue_text(self.key))
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [failed])

    async def test_error_blocks_restart_even_when_pause_persistence_failed(self):
        other_owner = (self.key, "999")
        await self.add()
        await self.add(song=workflow_song(1), owner=other_owner)
        await self.add(song=workflow_song(2), owner=other_owner)
        failed, error = await self.fail_queued_head(pause_save_fails=True)
        self.assertNotIn(self.key, self.plugin.state.snapshot("paused"))
        _, other, last = self.plugin.state.snapshot("queues", self.key)
        actions = [
            {"action": "move", "entry_id": last["id"], "direction": "down", "admin": True},
            {"action": "remove", "entry_id": other["id"], "owner": other_owner},
            {"action": "cancel_mine", "owner": other_owner},
        ]
        for action in actions:
            with self.subTest(action=action["action"]):
                await self.plugin.queue_action(self.key, **action)
                await asyncio.sleep(0.02)
                self.assertEqual(self.plugin._errors[self.key], error)
                self.assertEqual(self.plugin._error_entry_ids[self.key], failed["id"])
                self.assertNotIn(self.key, self.plugin._workers)
                self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [failed])

    async def test_unrelated_edits_do_not_acknowledge_submitted_head_save_failure(self):
        await self.add()
        await self.add(song=workflow_song(1))
        failed, error = await self.fail_queued_head(submitted=True)
        self.assertIn("消息已提交", error)
        other = self.plugin.state.snapshot("queues", self.key)[1]
        await self.plugin.queue_action(
            self.key, "move", entry_id=other["id"], direction="up", admin=True
        )
        await self.plugin.queue_action(self.key, "remove", entry_id=other["id"], owner=self.owner)
        self.assertEqual(self.plugin._errors[self.key], error)
        self.assertEqual(self.plugin._error_entry_ids[self.key], failed["id"])
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [failed])
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertNotIn(self.key, self.plugin._workers)

    async def test_skip_of_reordered_other_head_does_not_resolve_failed_entry(self):
        await self.add()
        await self.add(song=workflow_song(1))
        failed, error = await self.fail_queued_head()
        other = self.plugin.state.snapshot("queues", self.key)[1]
        await self.plugin.queue_action(
            self.key, "move", entry_id=other["id"], direction="up", admin=True
        )
        await self.plugin.queue_action(self.key, "skip", admin=True)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [failed])
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertEqual(self.plugin._errors[self.key], error)
        self.assertEqual(self.plugin._error_entry_ids[self.key], failed["id"])
        self.assertNotIn(self.key, self.plugin._workers)
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_removing_failed_entry_resolves_error_without_resuming_paused_queue(self):
        await self.add()
        await self.add(song=workflow_song(1))
        failed, _ = await self.fail_queued_head()
        other = self.plugin.state.snapshot("queues", self.key)[1]
        await self.plugin.queue_action(
            self.key, "move", entry_id=other["id"], direction="up", admin=True
        )
        await self.plugin.queue_action(self.key, "remove", entry_id=failed["id"], owner=self.owner)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [other])
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertNotIn(self.key, self.plugin._workers)
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_cancel_mine_resolves_error_only_when_failed_entry_is_removed(self):
        await self.add()
        await self.add(song=workflow_song(1), owner=(self.key, "999"))
        failed, error = await self.fail_queued_head()
        await self.plugin.queue_action(self.key, "cancel_mine", owner=(self.key, "999"))
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [failed])
        self.assertEqual(self.plugin._errors[self.key], error)
        await self.plugin.queue_action(self.key, "cancel_mine", owner=self.owner)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertNotIn(self.key, self.plugin._workers)
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_failed_removal_save_does_not_clear_error_marker(self):
        await self.add()
        failed, error = await self.fail_queued_head()
        before = self.plugin.state.path.read_bytes()
        with patch(
            "youtube_music_test.core.persistence.atomic_write", side_effect=OSError("disk failed")
        ):
            with self.assertRaises(OSError):
                await self.plugin.queue_action(
                    self.key, "remove", entry_id=failed["id"], owner=self.owner
                )
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), [failed])
        self.assertEqual(self.plugin._errors[self.key], error)
        self.assertEqual(self.plugin._error_entry_ids[self.key], failed["id"])
        self.assertNotIn(self.key, self.plugin._workers)

    async def test_error_without_known_failed_entry_requires_explicit_acknowledgement(self):
        await self.add()
        await self.fail_queued_head()
        error = self.plugin._errors[self.key]
        self.plugin._error_entry_ids.pop(self.key)
        await self.plugin.queue_action(self.key, "skip", admin=True)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertEqual(self.plugin._errors[self.key], error)
        self.assertNotIn(self.key, self.plugin._workers)
        await self.plugin.queue_action(self.key, "clear", admin=True)
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_skipping_failed_entry_resolves_error_and_starts_next_song(self):
        await self.add()
        next_song = workflow_song(1)
        await self.add(song=next_song)
        await self.fail_queued_head()
        self.context.send_message.side_effect = None
        await self.plugin.queue_action(self.key, "skip", admin=True)
        await self.wait_for(lambda: bool(self.plugin.library.history(self.owner)))
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertEqual(
            self.plugin.library.history(self.owner)[0]["song"]["video_id"], next_song.video_id
        )
        self.assertEqual(self.context.send_message.await_count, 2)
        await self.plugin.queue_action(self.key, "pause", admin=True)

    async def test_explicit_resume_resolves_error_and_retries_failed_song(self):
        await self.add()
        await self.fail_queued_head()
        self.context.send_message.side_effect = None
        await self.plugin.queue_action(self.key, "resume", admin=True)
        await self.wait_for(lambda: bool(self.plugin.library.history(self.owner)))
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertEqual(self.context.send_message.await_count, 2)
        await self.plugin.queue_action(self.key, "pause", admin=True)

    async def test_clear_resolves_error_and_keeps_empty_queue_paused(self):
        await self.add()
        await self.fail_queued_head()
        await self.plugin.queue_action(self.key, "clear", admin=True)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertNotIn(self.key, self.plugin._workers)
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_forget_resolves_post_submission_error_without_replaying(self):
        await self.add()
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {})
        )
        with patch.object(
            self.plugin.library, "add_history_for_owner_id", side_effect=OSError("disk failed")
        ):
            self.plugin._start_worker(self.key)
            await self.wait_for(lambda: self.key not in self.plugin._workers)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertIn(self.key, self.plugin._errors)
        self.assertIn(self.key, self.plugin._error_entry_ids)
        await self.plugin.queue_action(self.key, "forget", admin=True)
        self.assertIsNone(self.plugin.state.snapshot("sessions", self.key))
        self.assertNotIn(self.key, self.plugin.state.snapshot("paused"))
        self.assertNotIn(self.key, self.plugin._errors)
        self.assertNotIn(self.key, self.plugin._error_entry_ids)
        self.assertNotIn(self.key, self.plugin._workers)
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_voice_success_records_history_and_removes_exact_head(self):
        await self.add()
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {})
        )
        self.plugin._start_worker(self.key)
        await self.wait_for(lambda: bool(self.plugin.library.history(self.owner)))
        self.assertFalse(self.plugin.state.snapshot()["queues"][self.key])
        await self.plugin.queue_action(self.key, "pause", admin=True)
        self.assertEqual(self.context.send_message.await_count, 1)
        self.assertEqual(self.plugin.library.history(self.owner)[0]["play_count"], 1)

    async def test_link_fallback_does_not_record_audio_history(self):
        await self.add()
        self.context.send_message.side_effect = [RuntimeError("voice failed"), True]
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {})
        )
        self.plugin._start_worker(self.key)
        await self.wait_for(lambda: not self.plugin.state.snapshot()["queues"][self.key])
        await self.plugin.queue_action(self.key, "pause", admin=True)
        self.assertEqual(self.context.send_message.await_count, 2)
        self.assertFalse(self.plugin.library.history(self.owner))

    async def test_cancel_mine_preserves_the_wait_for_an_already_submitted_song(self):
        from dataclasses import replace

        submitted_song = replace(self.song, duration=30)
        await self.add()
        pending = Song(
            "bcdefghijkl", "Mine", "Artist", 1, "https://www.youtube.com/watch?v=bcdefghijkl"
        )
        other = Song(
            "cdefghijklm", "Other", "Artist", 1, "https://www.youtube.com/watch?v=cdefghijklm"
        )
        await self.add(song=pending)
        await self.add(song=other, owner=(self.key, "999"))
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", submitted_song, {})
        )
        self.plugin._start_worker(self.key)
        await self.wait_for(
            lambda: (
                self.plugin._current.get(self.key, {}).get("submitted")
                and bool(self.plugin.library.history(self.owner))
            )
        )
        worker = self.plugin._workers[self.key]
        await self.plugin.queue_action(self.key, "cancel_mine", owner=self.owner)
        self.assertIs(self.plugin._workers[self.key], worker)
        self.assertFalse(worker.done())
        self.assertTrue(self.plugin._current[self.key]["submitted"])
        payload = self.plugin.state.snapshot()
        self.assertNotIn(self.key, payload["paused"])
        self.assertEqual(
            [row["song"]["video_id"] for row in payload["queues"][self.key]], [other.video_id]
        )
        self.assertEqual(self.context.send_message.await_count, 1)
        await self.plugin.queue_action(self.key, "pause", admin=True)

    async def test_timeout_does_not_send_a_second_message(self):
        self.context.send_message.side_effect = TimeoutError()
        with self.assertRaises(module.UserFacingError):
            await self.plugin._send_song(
                {"umo": "qq:GroupMessage:123"}, self.song, self.root / "track.wav"
            )
        self.assertEqual(self.context.send_message.await_count, 1)

    async def test_send_concurrency_applies_across_sessions_and_fallbacks(self):
        self.assertIs(
            self.plugin._search_recall.semaphore,
            self.plugin.service._stage_limit("send_concurrency", 1),
        )
        active, peak = 0, 0

        async def send(*args):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.02)
            active -= 1
            return True

        self.context.send_message.side_effect = send
        await asyncio.gather(
            *(
                self.plugin._send_song(
                    {"umo": f"qq:GroupMessage:{i}"}, self.song, self.root / "track.wav"
                )
                for i in range(5)
            )
        )
        self.assertEqual(peak, 1)

    async def test_candidates_and_cooldown_share_one_bounded_registry(self):
        self.plugin.service.search = AsyncMock(return_value=[self.song])
        for index in range(1000):
            await self.plugin.search(("owner", str(index)), "music")
        self.assertEqual(len(self.plugin._candidates), 500)
        self.assertFalse(hasattr(self.plugin, "_last_search_at"))
        with patch.object(module.time, "monotonic", return_value=module.time.monotonic() + 10000):
            self.plugin._prune_candidates()
        self.assertFalse(self.plugin._candidates)

    async def test_remembering_a_known_session_does_not_rewrite_store(self):
        with patch("youtube_music_test.core.persistence.atomic_write") as write:
            for _ in range(10):
                await self.plugin._event_identity(self.event)
            write.assert_not_called()

    async def test_invalid_web_queue_inputs_return_400(self):
        for body in ({"session": []}, {"session": self.key, "action": []}):
            with bind_request_context(web_request(body=body)):
                response = await self.plugin.web_queue_action()
            self.assertEqual(response.status_code, 400)

    async def test_ai_cannot_auto_choose_search_or_control_other_members_queue(self):
        with self.assertRaises(module.UserFacingError):
            await self.plugin.ai_music_action(self.event, "enqueue", "some music")
        with self.assertRaises(module.UserFacingError):
            await self.plugin.ai_music_action(ChatEvent(sender="999"), "next")

    async def test_ai_search_words_cannot_be_reinterpreted_as_queue_commands(self):
        await self.add()
        self.plugin.service.search = AsyncMock(return_value=[self.song])
        await self.plugin.ai_music_action(self.event, "search", "下一首")
        self.plugin.service.search.assert_awaited_once_with("下一首")
        self.assertEqual(len(self.plugin.state.snapshot()["queues"][self.key]), 1)

    async def test_schema_configuration_is_authoritative_after_restart(self):
        await self.plugin.terminate()
        self.config["send_strategy"] = "link_only"
        self.config["queue_wait_timeout_seconds"] = 30
        with patch.object(StarTools, "get_data_dir", return_value=self.root):
            second = Plugin(self.context, self.config)
        await second.initialize()
        self.addAsyncCleanup(second.terminate)
        self.assertEqual(second.settings.send_strategy, "link_only")
        self.assertEqual(second.settings.queue_wait_timeout_seconds, 30)

    async def test_persisted_queue_requires_resume_after_restart(self):
        await self.add()
        await self.plugin.terminate()
        with patch.object(StarTools, "get_data_dir", return_value=self.root):
            second = Plugin(self.context, self.config)
        await second.initialize()
        self.addAsyncCleanup(second.terminate)
        self.assertIn(self.key, second.state.snapshot()["paused"])
        self.assertFalse(second._workers)
