"""Owner-scoped library selection and authenticated Web workflow regressions."""

import copy
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / ".verification" / "library-workflows"
os.environ.setdefault("ASTRBOT_ROOT", str(ROOT / ".verification" / "astrbot"))

from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from starlette.requests import Request  # noqa: E402

from core.errors import UserFacingError  # noqa: E402
from core.library import UserLibrary  # noqa: E402
from core.queue import MAX_BATCH_SIZE  # noqa: E402
from core.utils import Song  # noqa: E402
from core.webui import WebUIMixin  # noqa: E402


def song_for(index):
    video_id = f"video{index:06d}"
    return Song(
        video_id, f"Track {index}", "Artist", 60, f"https://www.youtube.com/watch?v={video_id}"
    )


class LibraryFixture:
    def set_up_library(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="library-", dir=TEST_ROOT)
        self.addCleanup(temporary.cleanup)
        self.library = UserLibrary(Path(temporary.name) / "library.json")
        self.owner = ("webui", "operator")
        self.foreign_owner = ("webui", "other")
        self.song = song_for(1)
        self.second = song_for(2)
        self.foreign_song = replace(song_for(3), title="Private foreign history")

    def save(self, source, song, owner=None):
        owner = owner or self.owner
        if source == "favorites":
            return self.library.add_favorite(owner, song)
        self.library.add_history_for_owner_id(self.library.owner_id(owner), song)

    def disk_bytes(self):
        return self.library.path.read_bytes() if self.library.path.exists() else None


class LibraryWorkflowsTests(LibraryFixture, unittest.TestCase):
    def setUp(self):
        self.set_up_library()

    def test_selection_preserves_request_order_and_copies_one_owner_snapshot(self):
        for source in ("favorites", "history"):
            self.save(source, self.song)
            self.save(source, self.second)
            self.save(source, self.foreign_song, self.foreign_owner)
            before = self.disk_bytes()
            with (
                self.subTest(source=source),
                patch.object(
                    self.library.store, "snapshot", wraps=self.library.store.snapshot
                ) as read,
                patch("core.persistence.copy.deepcopy", wraps=copy.deepcopy) as clone,
            ):
                self.assertEqual(
                    self.library.songs_for_source(
                        self.owner, source, [self.song.video_id, self.second.video_id]
                    ),
                    [self.song, self.second],
                )
                read.assert_called_once_with(source, self.library.owner_id(self.owner), default=[])
                clone.assert_called_once()
                self.assertEqual(
                    [row["song"]["video_id"] for row in clone.call_args.args[0]],
                    [self.second.video_id, self.song.video_id],
                )
            self.assertEqual(self.disk_bytes(), before)

    def test_selection_accepts_twenty_unique_songs_and_rejects_the_next(self):
        self.assertEqual(MAX_BATCH_SIZE, 20)
        songs = [song_for(index) for index in range(MAX_BATCH_SIZE + 1)]
        for song in songs:
            self.save("favorites", song)
        requested = songs[:MAX_BATCH_SIZE][::-1]
        self.assertEqual(
            self.library.songs_for_source(
                self.owner, "favorites", [song.video_id for song in requested]
            ),
            requested,
        )
        before = self.disk_bytes()
        with (
            patch.object(self.library.store, "snapshot") as read,
            self.assertRaises(UserFacingError),
        ):
            self.library.songs_for_source(
                self.owner, "favorites", [song.video_id for song in songs]
            )
        read.assert_not_called()
        self.assertEqual(self.disk_bytes(), before)

    def test_invalid_sources_are_rejected_before_reading_any_library(self):
        for source in (None, [], {}, 1, "search", "History", "favorites ", "playlist"):
            with (
                self.subTest(source=source),
                patch.object(self.library.store, "snapshot") as read,
                self.assertRaises(UserFacingError),
            ):
                self.library.songs_for_source(self.owner, source, [self.song.video_id])
            read.assert_not_called()
        self.assertIsNone(self.disk_bytes())

    def test_invalid_or_duplicate_ids_are_rejected_before_snapshot(self):
        selections = (
            None,
            [],
            (),
            (self.song.video_id,),
            self.song.video_id,
            {"video_id": self.song.video_id},
            [None],
            [True],
            [7],
            [{}],
            [["video000001"]],
            [self.song],
            ["short"],
            ["x" * 21],
            ["https://youtu.be/video000001"],
            [" video000001"],
            ["video/000001"],
            ["video000001\n"],
            [self.song.video_id, self.song.video_id],
            [self.song.video_id, None],
        )
        for selection in selections:
            with (
                self.subTest(selection=selection),
                patch.object(self.library.store, "snapshot") as read,
                self.assertRaises(UserFacingError),
            ):
                self.library.songs_for_source(self.owner, "favorites", selection)
            read.assert_not_called()
        self.assertIsNone(self.disk_bytes())

    def test_missing_foreign_and_removed_ids_fail_the_entire_selection_identically(self):
        for source in ("favorites", "history"):
            self.save(source, self.song)
            self.save(source, self.foreign_song, self.foreign_owner)
            self.save(source, self.second)
            if source == "favorites":
                self.library.remove_favorite(self.owner, self.second.video_id)
            else:
                self.library.clear_history(self.owner)
                self.save(source, self.song)
            before = self.disk_bytes()
            messages = []
            for video_id in ("missing0001", self.foreign_song.video_id, self.second.video_id):
                with self.subTest(source=source, video_id=video_id):
                    with self.assertRaises(UserFacingError) as error:
                        self.library.songs_for_source(
                            self.owner, source, [self.song.video_id, video_id]
                        )
                    messages.append(str(error.exception))
                    self.assertNotIn(video_id, str(error.exception))
                    self.assertNotIn(self.foreign_song.title, str(error.exception))
            self.assertEqual(len(set(messages)), 1)
            self.assertEqual(self.disk_bytes(), before)

    def test_selection_does_not_fall_back_to_another_section_or_owner(self):
        self.save("favorites", self.song)
        self.save("history", self.second)
        self.save("history", self.song, self.foreign_owner)
        for source, video_id in (
            ("history", self.song.video_id),
            ("favorites", self.second.video_id),
        ):
            with self.subTest(source=source), self.assertRaises(UserFacingError):
                self.library.songs_for_source(self.owner, source, [video_id])

    def test_each_selection_observes_latest_source_without_mutating_prior_results(self):
        self.save("history", self.song)
        first = self.library.songs_for_source(self.owner, "history", [self.song.video_id])
        replacement = replace(self.song, title="Updated metadata")
        self.save("history", replacement)
        self.assertEqual(
            self.library.songs_for_source(self.owner, "history", [self.song.video_id]),
            [replacement],
        )
        self.assertEqual(first, [self.song])
        self.library.clear_history(self.owner)
        with self.assertRaises(UserFacingError):
            self.library.songs_for_source(self.owner, "history", [self.song.video_id])

    def test_history_favorite_reuses_resolution_and_existing_add_semantics(self):
        self.save("history", self.song)
        self.save("history", self.song)
        history = self.library.history(self.owner)
        with (
            patch.object(
                self.library, "songs_for_source", wraps=self.library.songs_for_source
            ) as resolve,
            patch.object(self.library, "add_favorite", wraps=self.library.add_favorite) as add,
        ):
            self.assertEqual(
                self.library.favorite_from_history(self.owner, self.song.video_id), (True, 1)
            )
            resolve.assert_called_once_with(self.owner, "history", [self.song.video_id])
            add.assert_called_once_with(self.owner, self.song)
        self.assertEqual(self.library.history(self.owner), history)
        payload = json.loads(self.library.path.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"schema_version", "favorites", "history"})
        self.assertEqual(payload["schema_version"], 1)
        reloaded = UserLibrary(self.library.path)
        self.assertEqual(reloaded.favorites(self.owner), [self.song])
        self.assertEqual(reloaded.history(self.owner), history)

    def test_history_favorite_is_idempotent_even_when_full_and_preserves_existing_metadata(self):
        self.library.max_favorites = 1
        self.save("favorites", self.song)
        self.save("history", replace(self.song, title="New history title"))
        before = self.disk_bytes()
        self.assertEqual(
            self.library.favorite_from_history(self.owner, self.song.video_id), (False, 1)
        )
        self.assertEqual(self.library.favorites(self.owner), [self.song])
        self.assertEqual(self.disk_bytes(), before)

    def test_history_favorite_capacity_error_preserves_history_and_favorites(self):
        self.library.max_favorites = 1
        self.save("favorites", self.song)
        self.save("history", self.second)
        before = self.disk_bytes()
        with self.assertRaises(UserFacingError):
            self.library.favorite_from_history(self.owner, self.second.video_id)
        self.assertEqual(self.disk_bytes(), before)
        self.assertEqual(self.library.favorites(self.owner), [self.song])
        self.assertEqual(self.library.history(self.owner)[0]["song"], asdict(self.second))

    def test_invalid_missing_and_foreign_history_never_create_a_favorite(self):
        self.save("history", self.foreign_song, self.foreign_owner)
        before = self.disk_bytes()
        for video_id in (None, [], True, "short", "missing0001", self.foreign_song.video_id):
            with (
                self.subTest(video_id=video_id),
                patch.object(self.library, "add_favorite") as add,
                self.assertRaises(UserFacingError),
            ):
                self.library.favorite_from_history(self.owner, video_id)
            add.assert_not_called()
        self.assertEqual(self.disk_bytes(), before)
        self.assertEqual(self.library.favorites(self.owner), [])

    def test_concurrent_history_favorites_are_idempotent(self):
        self.save("history", self.song)
        with ThreadPoolExecutor(8) as executor:
            results = list(
                executor.map(
                    lambda _: self.library.favorite_from_history(self.owner, self.song.video_id),
                    range(16),
                )
            )
        self.assertEqual(results.count((True, 1)), 1)
        self.assertEqual(results.count((False, 1)), 15)
        self.assertEqual(self.library.favorites(self.owner), [self.song])
        self.assertEqual(self.library.history(self.owner)[0]["play_count"], 1)


def web_request(body, *, username="operator", query=""):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "query_string": query.encode(),
            "headers": [(b"content-type", b"application/json")],
        },
        AsyncMock(
            return_value={
                "type": "http.request",
                "body": json.dumps(body).encode(),
                "more_body": False,
            }
        ),
    )
    return PluginRequest(request, plugin_name="astrbot_plugin_youtube_music", username=username)


class LibraryWebHarness(WebUIMixin):
    _running = True
    _terminating = False

    def __init__(self, library):
        self.library = library
        self.candidate = Mock(return_value=song_for(1))
        self.enqueue = AsyncMock(return_value=1)
        self.enqueue_many = AsyncMock(
            return_value={"added": 1, "positions": [], "skipped": [], "queue_size": 1}
        )

    @asynccontextmanager
    async def operation(self):
        yield


class LibraryWebWorkflowsTests(LibraryFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.set_up_library()
        self.web = LibraryWebHarness(self.library)
        self.session = "qq:GroupMessage:123"

    async def call(self, handler, body, *, username="operator", query=""):
        with bind_request_context(web_request(body, username=username, query=query)):
            return await handler()

    def batch_body(self, source="favorites", video_ids=None):
        return {
            "session": self.session,
            "source": source,
            "video_ids": [self.song.video_id] if video_ids is None else video_ids,
        }

    async def test_batch_uses_owner_metadata_in_requested_order_and_passes_through_result(self):
        result = {
            "added": 1,
            "positions": [{"video_id": self.second.video_id, "position": 3}],
            "skipped": [self.song.video_id],
            "queue_size": 3,
        }
        self.web.enqueue_many.return_value = result
        for source in ("favorites", "history"):
            self.save(source, self.song)
            self.save(source, self.second)
            self.save(source, self.foreign_song, self.foreign_owner)
            before = self.disk_bytes()
            self.web.enqueue_many.reset_mock()
            with self.subTest(source=source):
                response = await self.call(
                    self.web.web_batch_enqueue,
                    self.batch_body(source, [self.second.video_id, self.song.video_id]),
                    query="owner=other&username=other",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body), result)
                self.web.enqueue_many.assert_awaited_once_with(
                    self.session,
                    [self.second, self.song],
                    self.owner,
                    "operator",
                    skip_existing=True,
                )
                self.web.enqueue.assert_not_awaited()
                self.web.candidate.assert_not_called()
                self.assertEqual(self.disk_bytes(), before)

    async def test_batch_rejects_client_song_objects_and_impersonation_fields(self):
        self.save("favorites", self.song)
        before = self.disk_bytes()
        for field, value in (
            ("song", asdict(self.song)),
            ("songs", [asdict(self.song)]),
            ("owner", "other"),
            ("username", "other"),
            ("url", self.song.url),
            ("index", 1),
        ):
            with self.subTest(field=field):
                response = await self.call(
                    self.web.web_batch_enqueue, {**self.batch_body(), field: value}
                )
                self.assertEqual(response.status_code, 400)
        self.web.enqueue_many.assert_not_awaited()
        self.assertEqual(self.disk_bytes(), before)

    async def test_batch_rejects_invalid_source_and_selections_without_enqueue(self):
        self.save("favorites", self.song)
        before = self.disk_bytes()
        bodies = [
            self.batch_body(source) for source in (None, [], {}, "search", "History", "favorites ")
        ]
        bodies.extend(
            self.batch_body(video_ids=ids)
            for ids in (
                [],
                self.song.video_id,
                {},
                [None],
                [True],
                [{}],
                ["short"],
                [self.song.video_id, self.song.video_id],
                [self.song.video_id, None],
                [song_for(index).video_id for index in range(MAX_BATCH_SIZE + 1)],
            )
        )
        bodies.extend(
            (
                {"session": self.session, "source": "favorites"},
                {"session": self.session, "video_ids": [self.song.video_id]},
                {**self.batch_body(), "video_ids": None},
            )
        )
        for body in bodies:
            with self.subTest(body=body):
                response = await self.call(self.web.web_batch_enqueue, body)
                self.assertEqual(response.status_code, 400)
        self.web.enqueue_many.assert_not_awaited()
        self.web.enqueue.assert_not_awaited()
        self.assertEqual(self.disk_bytes(), before)

    async def test_batch_rejects_invalid_sessions_before_reading_library(self):
        for session in (None, "", [], {}, True, 1):
            with (
                self.subTest(session=session),
                patch.object(self.library, "songs_for_source") as resolve,
            ):
                response = await self.call(
                    self.web.web_batch_enqueue, {**self.batch_body(), "session": session}
                )
                self.assertEqual(response.status_code, 400)
                resolve.assert_not_called()
        self.web.enqueue_many.assert_not_awaited()
        self.assertIsNone(self.disk_bytes())

    async def test_batch_missing_foreign_or_removed_song_rejects_all_without_disclosure(self):
        for source in ("favorites", "history"):
            self.save(source, self.song)
            self.save(source, self.foreign_song, self.foreign_owner)
            self.save(source, self.second)
            if source == "favorites":
                self.library.remove_favorite(self.owner, self.second.video_id)
            else:
                self.library.clear_history(self.owner)
                self.save(source, self.song)
            before = self.disk_bytes()
            errors = []
            for video_id in ("missing0001", self.foreign_song.video_id, self.second.video_id):
                with self.subTest(source=source, video_id=video_id):
                    response = await self.call(
                        self.web.web_batch_enqueue,
                        self.batch_body(source, [self.song.video_id, video_id]),
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn(video_id, response.body.decode())
                    self.assertNotIn(self.foreign_song.title, response.body.decode())
                    errors.append(json.loads(response.body))
            self.assertEqual(errors[0], errors[1])
            self.assertEqual(errors[1], errors[2])
            self.assertEqual(self.disk_bytes(), before)
        self.web.enqueue_many.assert_not_awaited()
        self.web.enqueue.assert_not_awaited()

    async def test_batch_queue_failure_is_reported_without_retry_or_library_changes(self):
        self.save("favorites", self.song)
        self.save("favorites", self.second)
        self.web.enqueue_many.side_effect = UserFacingError("Queue capacity exceeded")
        before = self.disk_bytes()
        response = await self.call(
            self.web.web_batch_enqueue,
            self.batch_body(video_ids=[self.song.video_id, self.second.video_id]),
        )
        self.assertEqual(response.status_code, 400)
        self.web.enqueue_many.assert_awaited_once()
        self.web.enqueue.assert_not_awaited()
        self.assertEqual(self.disk_bytes(), before)

    async def test_batch_authentication_and_stopped_lifecycle_fail_before_library_read(self):
        for username in (None, ""):
            with (
                self.subTest(username=username),
                patch.object(self.library, "songs_for_source") as resolve,
            ):
                response = await self.call(
                    self.web.web_batch_enqueue, self.batch_body(), username=username
                )
                self.assertEqual(response.status_code, 401)
                resolve.assert_not_called()
        for attribute in ("_running", "_terminating"):
            setattr(self.web, attribute, attribute == "_terminating")
            with patch.object(self.library, "songs_for_source") as resolve:
                response = await self.call(self.web.web_batch_enqueue, self.batch_body())
                self.assertEqual(response.status_code, 503)
                resolve.assert_not_called()
            setattr(self.web, attribute, attribute == "_running")
        self.web.enqueue_many.assert_not_awaited()
        self.assertIsNone(self.disk_bytes())

    async def test_single_library_enqueue_also_uses_only_the_current_owner_source(self):
        for source in ("favorites", "history"):
            self.save(source, self.song)
            self.save(source, self.foreign_song, self.foreign_owner)
            self.web.enqueue.reset_mock()
            body = {
                "session": self.session,
                "source": source,
                "video_id": self.song.video_id,
            }
            with (
                self.subTest(source=source),
                patch.object(
                    self.library, "songs_for_source", wraps=self.library.songs_for_source
                ) as resolve,
            ):
                response = await self.call(self.web.web_enqueue, body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body), {"position": 1})
                resolve.assert_called_once_with(self.owner, source, [self.song.video_id])
                self.web.enqueue.assert_awaited_once_with(
                    self.session, self.song, self.owner, "operator"
                )
                self.web.enqueue.reset_mock()
                response = await self.call(
                    self.web.web_enqueue, {**body, "video_id": self.foreign_song.video_id}
                )
                self.assertEqual(response.status_code, 400)
                self.web.enqueue.assert_not_awaited()
        self.web.candidate.assert_not_called()
        self.web.enqueue_many.assert_not_awaited()

    async def test_history_favorite_uses_own_metadata_without_search_and_is_idempotent(self):
        self.save("history", self.song)
        self.save("history", replace(self.song, title="Foreign metadata"), self.foreign_owner)
        before_history = self.library.history(self.owner)
        body = {"action": "favorite", "source": "history", "video_id": self.song.video_id}
        response = await self.call(
            self.web.web_library_action, body, query="owner=other&username=other"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"ok": True})
        self.assertEqual(self.library.favorites(self.owner), [self.song])
        self.assertEqual(self.library.favorites(self.foreign_owner), [])
        self.assertEqual(self.library.history(self.owner), before_history)
        before = self.disk_bytes()
        response = await self.call(self.web.web_library_action, body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"ok": True})
        self.assertEqual(self.disk_bytes(), before)
        self.web.candidate.assert_not_called()

    async def test_history_favorite_rejects_search_indices_and_unsupported_sources(self):
        self.save("history", self.song)
        before = self.disk_bytes()
        body = {"action": "favorite", "source": "history", "video_id": self.song.video_id}
        cases = [{**body, "index": index} for index in (None, 0, 1, "1", True)]
        cases.extend(
            {**body, "source": source} for source in (None, [], {}, "favorites", "History")
        )
        for invalid in cases:
            with self.subTest(body=invalid):
                response = await self.call(self.web.web_library_action, invalid)
                self.assertEqual(response.status_code, 400)
        self.web.candidate.assert_not_called()
        self.assertEqual(self.library.favorites(self.owner), [])
        self.assertEqual(self.disk_bytes(), before)

    async def test_nonfavorite_actions_reject_source_before_mutating_library(self):
        self.save("favorites", self.song)
        self.save("history", self.song)
        before = self.disk_bytes()
        for action in ("remove", "clear_history"):
            for source in ("search", "history", None):
                with self.subTest(action=action, source=source):
                    response = await self.call(
                        self.web.web_library_action,
                        {"action": action, "source": source, "video_id": self.song.video_id},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.disk_bytes(), before)

    async def test_history_favorite_rejects_missing_foreign_removed_and_invalid_ids(self):
        self.save("history", self.foreign_song, self.foreign_owner)
        self.save("history", self.song)
        self.library.clear_history(self.owner)
        before = self.disk_bytes()
        for video_id in (
            None,
            [],
            True,
            "short",
            "missing0001",
            self.foreign_song.video_id,
            self.song.video_id,
        ):
            with self.subTest(video_id=video_id):
                response = await self.call(
                    self.web.web_library_action,
                    {"action": "favorite", "source": "history", "video_id": video_id},
                )
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(self.foreign_song.title, response.body.decode())
                if isinstance(video_id, str) and len(video_id) >= 6:
                    self.assertNotIn(video_id, response.body.decode())
                self.assertEqual(self.disk_bytes(), before)
        self.assertEqual(self.library.favorites(self.owner), [])
        self.web.candidate.assert_not_called()

    async def test_history_favorite_capacity_error_and_duplicate_keep_existing_data(self):
        self.library.max_favorites = 1
        self.save("favorites", self.song)
        self.save("history", self.song)
        self.save("history", self.second)
        before = self.disk_bytes()
        for video_id, status in ((self.second.video_id, 400), (self.song.video_id, 200)):
            with self.subTest(video_id=video_id):
                response = await self.call(
                    self.web.web_library_action,
                    {"action": "favorite", "source": "history", "video_id": video_id},
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(self.disk_bytes(), before)
        self.assertEqual(self.library.favorites(self.owner), [self.song])
        self.web.candidate.assert_not_called()

    async def test_search_favorite_keeps_default_and_explicit_source_confirmation(self):
        for source_fields in ({}, {"source": "search"}):
            self.web.candidate.reset_mock()
            with self.subTest(source_fields=source_fields):
                response = await self.call(
                    self.web.web_library_action,
                    {
                        "action": "favorite",
                        "index": 1,
                        "video_id": self.song.video_id,
                        **source_fields,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body), {"ok": True})
                self.web.candidate.assert_called_once_with(self.owner, 1)
                self.assertEqual(self.library.favorites(self.owner), [self.song])

    async def test_stale_search_confirmation_cannot_favorite_another_song(self):
        for source_fields in ({}, {"source": "search"}):
            self.web.candidate.reset_mock()
            with self.subTest(source_fields=source_fields):
                response = await self.call(
                    self.web.web_library_action,
                    {
                        "action": "favorite",
                        "index": 1,
                        "video_id": self.second.video_id,
                        **source_fields,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.web.candidate.assert_called_once_with(self.owner, 1)
                self.assertEqual(self.library.favorites(self.owner), [])
                self.assertIsNone(self.disk_bytes())


if __name__ == "__main__":
    unittest.main()
