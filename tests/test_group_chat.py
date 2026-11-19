"""Group-chat candidates, bounded artwork and personal replay use existing workflows."""

import asyncio
import base64
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import test_runtime
from astrbot.api.web import bind_request_context
from test_runtime import ChatEvent, Song, module, web_request, workflow_song
from test_search_recall import OneBot, qq_event

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4ioAAAAASUVORK5CYII="
)


class CoverResponse:
    def __init__(self, chunks=(PNG,), *, status=200, headers=None, length=None, error=None):
        self.status = status
        self.headers = headers or {"Content-Type": "image/png"}
        self.content_length = length
        self.content = self
        self.chunks = chunks
        self.error = error
        self.read_chunks = 0
        self.closed = False
        self.entered = asyncio.Event()
        self.release = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def iter_chunked(self, size):
        self.chunk_size = size
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error:
            raise self.error
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk


class CoverClient:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


class GroupChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.plugin.settings = replace(
            self.plugin.settings, search_results=3, search_result_auto_recall=False
        )
        self.plugin._start_worker = lambda key: None
        self.plugin.service.search = AsyncMock(return_value=[self.song])

    async def dispatch(self, argument, event=None):
        return await self.plugin.dispatch_music(event or self.event, argument)

    async def test_concurrent_group_members_keep_reverse_completed_results(self):
        other = ChatEvent(sender="999")
        _, other_owner, _ = await self.plugin._event_identity(other)
        first_song, second_song = workflow_song(1), workflow_song(2)
        started = {name: asyncio.Event() for name in ("First", "Second")}
        release = {name: asyncio.Event() for name in started}

        async def search(query, **kwargs):
            self.assertEqual(kwargs, {"candidate_pool": True})
            started[query].set()
            await release[query].wait()
            return [first_song if query == "First" else second_song]

        self.plugin.service.search.side_effect = search
        first = asyncio.create_task(self.dispatch("First"))
        second = asyncio.create_task(self.dispatch("Second", other))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 2)
        release["Second"].set()
        second_reply = await second
        release["First"].set()
        first_reply = await first
        self.assertEqual(self.plugin.candidate(self.owner, 1), first_song)
        self.assertEqual(self.plugin.candidate(other_owner, 1), second_song)
        self.assertIn("456", first_reply.text)
        self.assertIn("999", second_reply.text)
        self.assertNotEqual(first_reply.token, second_reply.token)
        other_code = self.plugin._candidates[other_owner]["generation"][:8]
        with self.assertRaisesRegex(module.UserFacingError, "结果码"):
            self.plugin.candidate(self.owner, 1, code=other_code)
        self.assertFalse(self.plugin._search_busy)
        self.assertFalse(self.plugin._search_tasks)

    async def test_same_member_across_groups_platforms_and_private_chats_is_isolated(self):
        events = [
            self.event,
            ChatEvent(group="321"),
            ChatEvent(platform="qq2"),
            ChatEvent(group=""),
        ]
        owners = []
        for index, event in enumerate(events, 1):
            _, owner, _ = await self.plugin._event_identity(event)
            owners.append(owner)
            song = workflow_song(index)
            self.plugin.service.search.return_value = [song]
            await self.dispatch(f"Query {index}", event)
        self.assertEqual(len(set(owners)), len(events))
        for index, owner in enumerate(owners, 1):
            self.assertEqual(self.plugin.candidate(owner, 1), workflow_song(index))

    async def test_result_codes_validate_generation_and_expiration(self):
        await self.dispatch("Original")
        old_code = self.plugin._candidates[self.owner]["generation"][:8]
        self.assertEqual(self.plugin.candidate(self.owner, 1, code=old_code.upper()), self.song)
        await self.dispatch("Replacement")
        new_code = self.plugin._candidates[self.owner]["generation"][:8]
        for code in (old_code, "bad", "00000000"):
            with self.subTest(code=code), self.assertRaises(module.UserFacingError):
                self.plugin.candidate(self.owner, 1, code=code)
        self.assertEqual(self.plugin.candidate(self.owner, 1, code=new_code), self.song)
        self.plugin._candidates[self.owner]["at"] -= self.plugin.settings.result_ttl_minutes * 60
        with self.assertRaises(module.UserFacingError):
            self.plugin.candidate(self.owner, 1, code=new_code)
        self.assertNotIn(self.owner, self.plugin._candidates)

    async def test_global_numbering_and_three_pages_reuse_one_candidate_pool(self):
        self.plugin.service.search.return_value = [workflow_song(index) for index in range(1, 15)]
        first = await self.dispatch("Many tracks")
        generation = self.plugin._candidates[self.owner]["generation"]
        second = await self.dispatch("下一页")
        third = await self.dispatch("翻页 3")
        self.assertIn("第 1/3 页", first.text)
        self.assertIn("共 9 首", first.text)
        self.assertIn("\n1. Track 1", first.text)
        self.assertIn("\n4. Track 4", second.text)
        self.assertNotIn("\n1. Track", second.text)
        self.assertIn("\n7. Track 7", third.text)
        self.assertIn("\n9. Track 9", third.text)
        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(second.token, third.token)
        self.assertEqual(self.plugin._candidates[self.owner]["generation"], generation)
        self.plugin.service.search.assert_awaited_once_with("Many tracks", candidate_pool=True)
        self.assertEqual(
            self.plugin.candidate(self.owner, 9, code=generation[:8]), workflow_song(9)
        )
        self.assertIn("第 2/3 页", (await self.dispatch("上一页")).text)

    async def test_invalid_pages_leave_the_display_token_unchanged(self):
        await self.dispatch("Track")
        token = self.plugin._candidates[self.owner]["token"]
        for argument in ("上一页", "下一页", "翻页 0", "翻页 4", "翻页 nope"):
            with self.subTest(argument=argument), self.assertRaises(module.UserFacingError):
                await self.dispatch(argument)
            self.assertEqual(self.plugin._candidates[self.owner]["token"], token)

    async def test_research_uses_previous_keywords_and_accepts_new_keywords(self):
        await self.dispatch("Old words")
        generation = self.plugin._candidates[self.owner]["generation"]
        await self.dispatch("重搜")
        self.plugin.service.search.assert_awaited_with("Old words", candidate_pool=True)
        self.assertNotEqual(self.plugin._candidates[self.owner]["generation"], generation)
        await self.dispatch("重新搜索 New words")
        self.plugin.service.search.assert_awaited_with("New words", candidate_pool=True)
        await self.dispatch("重新搜索")
        self.plugin.service.search.assert_awaited_with("New words", candidate_pool=True)

    async def test_research_without_previous_keywords_fails_clearly(self):
        with self.assertRaisesRegex(module.UserFacingError, "没有可重搜"):
            await self.dispatch("重搜")
        self.plugin.service.search.assert_not_awaited()

    async def test_failed_search_invalidates_old_candidates_but_can_be_researched(self):
        await self.dispatch("Old words")
        self.plugin.service.search.side_effect = module.UserFacingError("Network failure")
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("Retry words")
        with self.assertRaises(module.UserFacingError):
            self.plugin.candidate(self.owner, 1)
        self.assertFalse(self.plugin._search_tasks)
        self.plugin.service.search.side_effect = None
        await self.dispatch("重搜")
        self.plugin.service.search.assert_awaited_with("Retry words", candidate_pool=True)
        self.assertEqual(self.plugin.candidate(self.owner, 1), self.song)

    async def test_same_owner_cannot_overlap_searches(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def search(*args, **kwargs):
            entered.set()
            await release.wait()
            return [self.song]

        self.plugin.service.search.side_effect = search
        pending = asyncio.create_task(self.dispatch("First"))
        await entered.wait()
        with self.assertRaisesRegex(module.UserFacingError, "频繁"):
            await self.dispatch("Second")
        release.set()
        await pending
        self.plugin.service.search.assert_awaited_once()

    async def test_cancel_search_affects_only_requester_and_not_the_queue(self):
        await self.plugin.enqueue(self.key, self.song, self.owner, "Member")
        before = self.plugin.state.snapshot("queues", self.key)
        other = ChatEvent(sender="999")
        _, other_owner, _ = await self.plugin._event_identity(other)
        other_song = workflow_song(2)
        self.plugin.service.search.return_value = [other_song]
        await self.dispatch("Other search", other)
        entered, release = asyncio.Event(), asyncio.Event()

        async def search(*args, **kwargs):
            entered.set()
            await release.wait()
            return [self.song]

        self.plugin.service.search.side_effect = search
        pending = asyncio.create_task(self.dispatch("Pending search"))
        await entered.wait()
        self.assertIn("没有", await self.dispatch("取消搜索", other))
        self.assertFalse(pending.done())
        self.assertIn("已取消你的搜索", await self.dispatch("取消搜索"))
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.plugin._search_busy)
        self.assertFalse(self.plugin._search_tasks)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), before)
        self.assertEqual(self.plugin.candidate(other_owner, 1), other_song)
        with self.assertRaises(module.UserFacingError):
            self.plugin.candidate(self.owner, 1)

    async def test_numbered_selection_and_favorite_accept_result_codes(self):
        await self.dispatch("Track")
        code = self.plugin._candidates[self.owner]["generation"][:8]
        self.assertEqual(await self.dispatch(f"收藏 1 {code}"), "已收藏。")
        reply = await self.dispatch(f"1 {code}")
        self.assertIn("已加入队列 #1", reply)
        self.assertEqual(self.plugin.library.favorites(self.owner), [self.song])
        self.assertEqual(
            self.plugin.state.snapshot("queues", self.key)[0]["song"]["video_id"],
            self.song.video_id,
        )

    async def test_numeric_leading_keywords_remain_searches(self):
        for query in ("1999 Prince", "1 Track", "2 Live album", "1234 abcdef12"):
            with self.subTest(query=query):
                await self.dispatch(query)
                self.plugin.service.search.assert_awaited_with(query, candidate_pool=True)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key, default=[]))

    async def test_artist_alias_uses_literal_keywords_without_search_operators(self):
        await self.dispatch("歌手 A & B")
        self.plugin.service.search.assert_awaited_once_with("A & B", candidate_pool=True)
        self.assertEqual(self.plugin._candidates[self.owner]["query"], "A & B")

    async def test_original_ai_and_web_search_parameters_stay_unchanged(self):
        await self.plugin.ai_music_action(self.event, "search", "AI query")
        self.plugin.service.search.assert_awaited_with("AI query")
        with bind_request_context(web_request(query="q=Web+query", method="GET")):
            response = await self.plugin.web_search()
        self.assertEqual(response.status_code, 200)
        self.plugin.service.search.assert_awaited_with("Web query")
        await self.plugin.search(self.owner, "Direct call")
        self.plugin.service.search.assert_awaited_with("Direct call")

    async def test_candidate_and_history_view_registries_are_bounded_and_expire(self):
        now = module.time.monotonic()
        for index in range(505):
            owner = ("session", str(index))
            self.plugin._candidates[owner] = {"at": now, "songs": [self.song]}
            self.plugin._history_views[owner] = {"at": now, "songs": [self.song]}
        self.plugin._prune_candidates()
        self.assertEqual(len(self.plugin._candidates), 500)
        self.assertEqual(len(self.plugin._history_views), 500)
        self.assertNotIn(("session", "0"), self.plugin._candidates)
        with patch.object(module.time, "monotonic", return_value=now + 7201):
            self.plugin._prune_candidates()
        self.assertFalse(self.plugin._candidates)
        self.assertFalse(self.plugin._history_views)


class HistoryRandomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.plugin._start_worker = lambda key: None

    async def history(self, song, owner=None):
        await module.run_blocking(
            self.plugin.library.add_history_for_owner_id,
            self.plugin.library.owner_id(owner or self.owner),
            song,
        )

    async def dispatch(self, argument, event=None):
        return await self.plugin.dispatch_music(event or self.event, argument)

    async def test_numbered_replay_uses_display_identity_after_new_history_is_inserted(self):
        first, second = workflow_song(1), workflow_song(2)
        await self.history(first)
        self.assertIn("1. Track 1", await self.dispatch("最近"))
        await self.history(second)
        reply = await self.dispatch("重播 1")
        self.assertIn("Track 1", reply)
        queued = self.plugin.state.snapshot("queues", self.key)
        self.assertEqual(queued[0]["song"]["video_id"], first.video_id)
        self.assertEqual(
            self.plugin.library.history(self.owner)[0]["song"]["video_id"], second.video_id
        )

    async def test_plain_replay_uses_latest_personal_history_without_requiring_a_display(self):
        await self.history(workflow_song(1))
        await self.history(workflow_song(2))
        self.assertIn("Track 2", await self.dispatch("重播"))
        self.assertEqual(
            self.plugin.state.snapshot("queues", self.key)[0]["song"]["video_id"],
            workflow_song(2).video_id,
        )

    async def test_numbered_replay_requires_current_personal_history_display(self):
        await self.history(self.song)
        with self.assertRaisesRegex(module.UserFacingError, "最近"):
            await self.dispatch("重播 1")
        await self.dispatch("最近")
        other = ChatEvent(sender="999")
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("重播 1", other)
        self.plugin._history_views[self.owner]["at"] -= self.plugin.settings.result_ttl_minutes * 60
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("重播 1")

    async def test_cleared_or_evicted_history_cannot_replay_stale_display(self):
        await self.history(self.song)
        await self.dispatch("最近")
        await self.dispatch("清除历史")
        self.assertNotIn(self.owner, self.plugin._history_views)
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("重播 1")
        await self.history(self.song)
        await self.dispatch("最近")
        await module.run_blocking(self.plugin.library.clear_history, self.owner)
        with self.assertRaisesRegex(module.UserFacingError, "列表"):
            await self.dispatch("重播 1")

    async def test_recent_history_display_is_capped_at_ten(self):
        for index in range(12):
            await self.history(workflow_song(index))
        display = await self.dispatch("最近")
        self.assertEqual(len(display.splitlines()), 10)
        self.assertEqual(len(self.plugin._history_views[self.owner]["songs"]), 10)
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("重播 11")

    async def test_random_deduplicates_personal_sources_and_excludes_queue_and_current(self):
        first, second, third, fourth = [workflow_song(index) for index in range(1, 5)]
        for song in (first, second, third):
            await self.history(song)
            await module.run_blocking(self.plugin.library.add_favorite, self.owner, song)
        other_owner = (self.key, "999")
        await self.history(fourth, other_owner)
        await self.plugin.enqueue(self.key, first, self.owner, "Member")
        self.plugin._current[self.key] = {"row": {"song": {"video_id": second.video_id}}}
        with patch.object(module.random, "choice", side_effect=lambda pool: pool[0]) as choose:
            self.assertEqual(await self.plugin._random_song(self.key, self.owner), third)
        self.assertEqual(choose.call_args.args[0], [third])
        self.plugin._current.clear()
        reply = await self.dispatch("随机")
        self.assertIn("已加入队列", reply)
        self.assertEqual(len(self.plugin.state.snapshot("queues", self.key)), 2)

    async def test_random_filters_duration_and_pcm_budget_before_choosing(self):
        too_long = workflow_song(1, self.plugin.settings.max_duration_seconds + 1)
        over_pcm = workflow_song(2, 100)
        playable = workflow_song(3, 1)
        self.plugin.settings = replace(self.plugin.settings, max_wav_size_mb=1)
        self.plugin.service.settings = self.plugin.settings
        for song in (too_long, over_pcm, playable):
            await self.history(song)
        with patch.object(module.random, "choice", side_effect=lambda pool: pool[0]) as choose:
            self.assertEqual(await self.plugin._random_song(self.key, self.owner), playable)
        self.assertEqual(choose.call_args.args[0], [playable])
        self.plugin.settings = replace(self.plugin.settings, send_strategy="link_only")
        self.plugin.service.settings = self.plugin.settings
        with patch.object(module.random, "choice", side_effect=lambda pool: pool[-1]) as choose:
            self.assertEqual(await self.plugin._random_song(self.key, self.owner), over_pcm)
        self.assertEqual(set(choose.call_args.args[0]), {playable, over_pcm})

    async def test_random_empty_pool_and_replay_empty_history_fail_without_enqueue(self):
        for command in ("随机", "重播"):
            with self.subTest(command=command), self.assertRaises(module.UserFacingError):
                await self.dispatch(command)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key, default=[]))


class SongDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.api = OneBot()
        self.event = qq_event(self.api, message="/点歌 详情 1")
        self.plugin._start_worker = lambda key: None
        self.song = Song(
            "abcdefghijk",
            "Track (Official Audio)",
            "Artist",
            60,
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        )
        self.plugin.service.search = AsyncMock(return_value=[self.song])
        await self.plugin.search(self.owner, "Track")

    async def render(self, response):
        client = CoverClient(response)
        with patch.object(module.aiohttp, "ClientSession", return_value=client) as session:
            replies = [result async for result in self.plugin.song_command(self.event)]
        self.assertEqual(len(replies), 1)
        return replies[0], client, session

    async def test_detail_has_one_combined_message_with_bounded_prefetched_cover(self):
        response = CoverResponse(chunks=(PNG[:8], PNG[8:]))
        reply, client, session = await self.render(response)
        self.assertEqual(len(reply.chain), 2)
        self.assertIsInstance(reply.chain[0], module.Comp.Image)
        self.assertIsInstance(reply.chain[1], module.Comp.Plain)
        self.assertEqual(base64.b64decode(reply.chain[0].file.removeprefix("base64://")), PNG)
        for expected in ("Track (Official Audio)", "Artist", "1:00", "官方音频", self.song.url):
            self.assertIn(expected, reply.chain[1].text)
        self.assertEqual(
            client.requests[0], (self.song.thumbnail_url, {"allow_redirects": False, "proxy": None})
        )
        self.assertEqual(session.call_args.kwargs["timeout"].total, 4)
        self.assertFalse(session.call_args.kwargs["auto_decompress"])
        self.assertFalse(session.call_args.kwargs["trust_env"])
        self.assertEqual(session.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertTrue(client.closed)
        self.assertTrue(response.closed)
        self.assertEqual(response.chunk_size, 16384)
        self.assertFalse(self.api.calls)
        self.assertFalse(self.plugin._search_recall._records)
        self.assertEqual(self.plugin.candidate(self.owner, 1), self.song)

    async def test_invalid_cover_hosts_never_start_a_network_request(self):
        for thumbnail in (
            "http://i.ytimg.com/a.png",
            "https://evil.example/a.png",
            "https://i.ytimg.com.evil.example/a.png",
            "https://user@i.ytimg.com/a.png",
            "https://i.ytimg.com:8080/a.png",
            "",
        ):
            with self.subTest(thumbnail=thumbnail):
                reply = module.SongDetailsReply("Text only", thumbnail)
                with patch.object(module.aiohttp, "ClientSession") as session:
                    result = await self.plugin._detail_result(self.event, reply)
                session.assert_not_called()
                self.assertEqual([item.text for item in result.chain], ["Text only"])

    async def test_redirects_invalid_mime_encoding_and_signatures_fall_back_to_text(self):
        cases = [
            CoverResponse(
                status=302,
                headers={"Content-Type": "image/png", "Location": "https://evil.example"},
            ),
            CoverResponse(headers={"Content-Type": "image/svg+xml"}),
            CoverResponse(headers={"Content-Type": "image/gif"}),
            CoverResponse(headers={"Content-Type": "text/plain"}),
            CoverResponse(headers={"Content-Type": "image/png", "Content-Encoding": "gzip"}),
            CoverResponse(chunks=(b"<svg><script>alert(1)</script></svg>",)),
            CoverResponse(chunks=(PNG,), headers={"Content-Type": "image/jpeg"}),
            CoverResponse(chunks=(b"RIFFwrongbody",), headers={"Content-Type": "image/webp"}),
        ]
        for response in cases:
            with self.subTest(
                status=response.status, headers=response.headers, chunks=response.chunks
            ):
                reply, client, _ = await self.render(response)
                self.assertEqual(len(reply.chain), 1)
                self.assertIsInstance(reply.chain[0], module.Comp.Plain)
                self.assertIn("Track", reply.chain[0].text)
                self.assertTrue(client.closed)
                self.assertTrue(response.closed)
                self.assertFalse(client.requests[0][1]["allow_redirects"])

    async def test_only_jpeg_png_and_webp_signatures_are_accepted(self):
        images = [
            ("image/jpeg", b"\xff\xd8\xffrest"),
            ("image/png", PNG),
            ("image/webp", b"RIFF\x04\x00\x00\x00WEBPrest"),
        ]
        for mime, image in images:
            with self.subTest(mime=mime):
                reply, _, _ = await self.render(
                    CoverResponse(
                        chunks=(image,), headers={"Content-Type": f"{mime}; charset=binary"}
                    )
                )
                self.assertIsInstance(reply.chain[0], module.Comp.Image)

    async def test_oversized_cover_is_rejected_before_read_when_length_is_known(self):
        response = CoverResponse(length=1024 * 1024 + 1)
        reply, client, _ = await self.render(response)
        self.assertEqual(len(reply.chain), 1)
        self.assertEqual(response.read_chunks, 0)
        self.assertTrue(client.closed)

    async def test_streaming_body_cap_ignores_misleading_length_and_stops_before_extra_chunks(self):
        response = CoverResponse(
            chunks=(PNG, b"x" * (1024 * 1024 - len(PNG)), b"x", b"should-not-be-read"),
            length=1,
        )
        reply, client, _ = await self.render(response)
        self.assertEqual(len(reply.chain), 1)
        self.assertEqual(response.read_chunks, 3)
        self.assertTrue(client.closed)

    async def test_single_oversized_chunk_is_rejected_and_exact_boundary_is_accepted(self):
        oversized = CoverResponse(chunks=(PNG + b"x" * (1024 * 1024),))
        reply, _, _ = await self.render(oversized)
        self.assertEqual(len(reply.chain), 1)
        boundary = CoverResponse(chunks=(PNG, b"x" * (1024 * 1024 - len(PNG))))
        reply, _, _ = await self.render(boundary)
        self.assertEqual(len(reply.chain), 2)

    async def test_cover_errors_and_timeout_fall_back_to_text_without_a_second_message(self):
        for error in (TimeoutError(), OSError("private token=do-not-expose")):
            with self.subTest(error=type(error).__name__):
                reply, client, _ = await self.render(CoverResponse(error=error))
                self.assertEqual(len(reply.chain), 1)
                self.assertNotIn("do-not-expose", reply.chain[0].text)
                self.assertTrue(client.closed)

    async def test_waiting_for_cover_resource_is_inside_the_total_timeout(self):
        gate = self.plugin.service._stage_limit("search_concurrency", 2)
        await gate.acquire()
        await gate.acquire()
        timeout = asyncio.timeout
        try:
            with patch.object(
                module.asyncio, "timeout", side_effect=lambda seconds: timeout(0.01)
            ) as budget:
                with patch.object(module.aiohttp, "ClientSession") as session:
                    reply = await self.plugin._detail_result(
                        self.event, module.SongDetailsReply("Details", self.song.thumbnail_url)
                    )
            budget.assert_called_once_with(4)
            session.assert_not_called()
            self.assertEqual([item.text for item in reply.chain], ["Details"])
            self.assertEqual(gate._value, 0)
        finally:
            gate.release()
            gate.release()
        self.assertEqual(gate._value, 2)

    async def test_cancellation_closes_artwork_response_and_releases_shared_gate(self):
        response = CoverResponse()
        response.release = asyncio.Event()
        client = CoverClient(response)
        with patch.object(module.aiohttp, "ClientSession", return_value=client):
            task = asyncio.create_task(
                self.plugin._detail_result(
                    self.event, module.SongDetailsReply("Details", self.song.thumbnail_url)
                )
            )
            await asyncio.wait_for(response.entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)
        self.assertEqual(self.plugin.service._stage_limit("search_concurrency", 2)._value, 2)

    async def test_details_validate_owner_result_code_and_keep_search_display(self):
        code = self.plugin._candidates[self.owner]["generation"][:8]
        detail = await self.plugin.dispatch_music(self.event, f"详情 1 {code}")
        self.assertIsInstance(detail, module.SongDetailsReply)
        other = qq_event(self.api, sender="999")
        with self.assertRaises(module.UserFacingError):
            await self.plugin.dispatch_music(other, f"详情 1 {code}")
        with self.assertRaises(module.UserFacingError):
            await self.plugin.dispatch_music(self.event, "详情 1 invalid")
        self.assertEqual(self.plugin.candidate(self.owner, 1, code=code), self.song)
