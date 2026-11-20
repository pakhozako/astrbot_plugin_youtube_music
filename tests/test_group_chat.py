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


class PersonalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.plugin._start_worker = lambda key: None

    async def add(self, index, event=None):
        event = event or self.event
        key, owner, _ = await self.plugin._event_identity(event)
        song = workflow_song(index, duration=index)
        await self.plugin.enqueue(key, song, owner, event.get_sender_name())
        return self.plugin.state.snapshot("queues", key)[-1]

    async def show(self, event=None):
        return await self.plugin.dispatch_music(event or self.event, "我的队列")

    async def test_personal_queue_keeps_shared_indices_and_only_personal_details(self):
        other = ChatEvent(sender="999")
        await self.add(1, other)
        await self.add(2)
        await self.add(3, other)
        await self.add(4)
        before = self.plugin.state.snapshot()
        reply = await self.show()
        self.assertIn("QQ 456", reply)
        self.assertIn("本人待播 2 首", reply)
        self.assertIn("总时长 0:06", reply)
        self.assertIn("\n2. Track 2", reply)
        self.assertIn("\n4. Track 4", reply)
        self.assertNotIn("Track 1", reply)
        self.assertNotIn("Track 3", reply)
        self.assertNotIn("User 999", reply)
        self.assertNotIn("\n1.", reply)
        self.assertEqual(self.plugin.state.snapshot(), before)
        self.context.send_message.assert_not_awaited()

    async def test_full_queue_and_status_are_unchanged(self):
        await self.add(1, ChatEvent(sender="999"))
        await self.add(2)
        full = self.plugin.queue_text(self.key)
        await self.show()
        self.assertEqual(await self.plugin.dispatch_music(self.event, "队列"), full)
        self.assertEqual(await self.plugin.dispatch_music(self.event, "状态"), full)
        self.assertIn("Track 1", full)
        self.assertIn("Track 2", full)

    async def test_own_current_is_displayed_once_and_excluded_from_pending_summary(self):
        current = await self.add(1)
        await self.add(2, ChatEvent(sender="999"))
        await self.add(3)
        self.plugin._current[self.key] = {
            "row": current,
            "phase": "转换音频",
            "submitted": False,
        }
        reply = await self.show()
        self.assertIn("当前 #1 · 转换音频：Track 1", reply)
        self.assertEqual(reply.count("Track 1"), 1)
        self.assertIn("本人待播 1 首", reply)
        self.assertIn("总时长 0:03", reply)
        self.assertIn("\n3. Track 3", reply)
        self.assertNotIn("Track 2", reply)
        self.assertNotIn("\n1. Track", reply)

    async def test_submitted_current_waiting_is_not_given_another_songs_queue_number(self):
        current = await self.add(1)
        await self.add(2)
        await module.run_blocking(
            self.plugin.state.update, lambda data: data["queues"][self.key].pop(0)
        )
        self.plugin._current[self.key] = {
            "row": current,
            "phase": "等待语音结束",
            "submitted": True,
        }
        reply = await self.show()
        self.assertIn("当前 · 等待语音结束：Track 1", reply)
        self.assertNotIn("当前 #1", reply)
        self.assertIn("\n1. Track 2", reply)
        self.assertIn("本人待播 1 首", reply)

    async def test_other_current_and_detailed_error_are_not_in_personal_output(self):
        other = await self.add(1, ChatEvent(sender="999"))
        await self.add(2)
        self.plugin._current[self.key] = {
            "row": other,
            "phase": "Private other stage",
            "submitted": False,
        }
        self.plugin._errors[self.key] = "Private failure for Track 1"
        reply = await self.show()
        self.assertIn("已暂停", reply)
        self.assertIn("群队列处理异常", reply)
        self.assertIn("\n2. Track 2", reply)
        self.assertNotIn("Track 1", reply)
        self.assertNotIn("Private", reply)
        self.assertNotIn("当前", reply)

    async def test_shared_pause_and_empty_personal_queue_do_not_expose_other_tracks(self):
        await self.add(1, ChatEvent(sender="999"))
        await module.run_blocking(
            self.plugin.state.update, lambda data: data["paused"].append(self.key)
        )
        reply = await self.show()
        self.assertIn("已暂停", reply)
        self.assertIn("本人待播 0 首", reply)
        self.assertIn("你没有待播歌曲", reply)
        self.assertNotIn("Track 1", reply)
        self.assertLessEqual(len(reply.splitlines()), 4)

    async def test_same_user_other_groups_platforms_and_web_owner_remain_isolated(self):
        await self.add(1)
        await self.add(2, ChatEvent(group="321"))
        await self.add(3, ChatEvent(platform="qq2"))
        await self.plugin.enqueue(self.key, workflow_song(4), ("webui", "456"), "Web user")
        reply = await self.show()
        self.assertIn("Track 1", reply)
        for title in ("Track 2", "Track 3", "Track 4", "Web user"):
            self.assertNotIn(title, reply)
        other_group = await self.show(ChatEvent(group="321"))
        self.assertIn("Track 2", other_group)
        self.assertNotIn("Track 1", other_group)
        other_platform = await self.show(ChatEvent(platform="qq2"))
        self.assertIn("Track 3", other_platform)
        self.assertNotIn("Track 1", other_platform)

    async def test_personal_queue_is_available_to_non_admin_members_and_private_chats(self):
        other = ChatEvent(sender="999")
        await self.add(1, other)
        self.assertIn("Track 1", await self.show(other))
        private = ChatEvent(sender="789", group="")
        await self.add(2, private)
        reply = await self.show(private)
        self.assertIn("\n1. Track 2", reply)
        self.assertNotIn("Track 1", reply)


class FavoriteSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.plugin._start_worker = lambda key: None

    async def favorite(self, index, owner=None):
        song = workflow_song(index)
        await module.run_blocking(self.plugin.library.add_favorite, owner or self.owner, song)
        return song

    async def dispatch(self, argument, event=None):
        return await self.plugin.dispatch_music(event or self.event, argument)

    async def test_without_display_both_numbered_commands_keep_direct_legacy_behavior(self):
        await self.favorite(1)
        second = await self.favorite(2)
        self.assertNotIn(self.owner, self.plugin._favorite_views)
        self.assertFalse(self.plugin._favorite_views_limited)
        self.assertIn("Track 2", await self.dispatch("播放收藏 1"))
        self.assertEqual(
            self.plugin.state.snapshot("queues", self.key)[0]["song"]["video_id"], second.video_id
        )
        self.assertEqual(await self.dispatch("取消收藏 1"), "已移除收藏。")
        self.assertEqual(self.plugin.library.favorites(self.owner), [workflow_song(1)])
        self.assertNotIn(self.owner, self.plugin._favorite_views)

    async def test_new_favorite_cannot_rebind_a_displayed_play_index(self):
        first = await self.favorite(1)
        self.assertIn("1. Track 1", await self.dispatch("收藏列表"))
        await self.favorite(2)
        self.assertIn("Track 1", await self.dispatch("播放收藏 1"))
        self.assertEqual(
            self.plugin.state.snapshot("queues", self.key)[0]["song"]["video_id"], first.video_id
        )

    async def test_new_favorite_cannot_rebind_a_displayed_remove_index(self):
        await self.favorite(1)
        second = await self.favorite(2)
        self.assertIn("1. Track 2", await self.dispatch("收藏列表"))
        third = await self.favorite(3)
        self.assertEqual(await self.dispatch("取消收藏 1"), "已移除收藏。")
        self.assertNotIn(second, self.plugin.library.favorites(self.owner))
        self.assertEqual(self.plugin.library.favorites(self.owner), [third, workflow_song(1)])

    async def test_deleting_one_displayed_song_does_not_shift_the_other_indices(self):
        first = await self.favorite(1)
        await self.favorite(2)
        await self.dispatch("收藏列表")
        await self.dispatch("取消收藏 1")
        self.assertIn("Track 1", await self.dispatch("播放收藏 2"))
        self.assertEqual(
            self.plugin.state.snapshot("queues", self.key)[0]["song"]["video_id"], first.video_id
        )
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("取消收藏 1")
        self.assertEqual(self.plugin.library.favorites(self.owner), [first])

    async def test_deleted_display_target_is_rejected_instead_of_using_a_new_current_index(self):
        shown = await self.favorite(1)
        await self.dispatch("收藏列表")
        replacement = await self.favorite(2)
        await module.run_blocking(self.plugin.library.remove_favorite, self.owner, shown.video_id)
        for argument in ("播放收藏 1", "取消收藏 1"):
            with self.subTest(argument=argument), self.assertRaises(module.UserFacingError):
                await self.dispatch(argument)
        self.assertEqual(self.plugin.library.favorites(self.owner), [replacement])
        self.assertFalse(self.plugin.state.snapshot("queues", self.key, default=[]))

    async def test_redisplaying_favorites_intentionally_updates_the_index_snapshot(self):
        await self.favorite(1)
        await self.dispatch("收藏列表")
        second = await self.favorite(2)
        self.assertIn("1. Track 2", await self.dispatch("收藏列表"))
        self.assertIn("Track 2", await self.dispatch("播放收藏 1"))
        self.assertEqual(self.plugin._favorite_views[self.owner][0], second.video_id)

    async def test_search_ttl_pruning_does_not_silently_expire_favorite_snapshot(self):
        shown = await self.favorite(1)
        await self.dispatch("收藏列表")
        await self.favorite(2)
        now = module.time.monotonic()
        with patch.object(module.time, "monotonic", return_value=now + 7201):
            self.plugin._prune_candidates()
        self.assertEqual(self.plugin._favorite_views[self.owner], (shown.video_id,))
        self.assertIn("Track 1", await self.dispatch("播放收藏 1"))

    async def test_empty_display_is_not_rebound_when_a_favorite_is_later_added(self):
        self.assertEqual(await self.dispatch("收藏列表"), "暂无收藏。")
        await self.favorite(1)
        with self.assertRaises(module.UserFacingError):
            await self.dispatch("播放收藏 1")
        self.assertIn("Track 1", await self.dispatch("收藏列表"))
        self.assertIn("Track 1", await self.dispatch("播放收藏 1"))

    async def test_favorite_views_are_owner_and_group_scoped(self):
        shown = await self.favorite(1)
        await self.dispatch("收藏列表")
        other = ChatEvent(sender="999")
        _, other_owner, _ = await self.plugin._event_identity(other)
        other_song = await self.favorite(2, other_owner)
        other_group = ChatEvent(group="321")
        other_key, other_group_owner, _ = await self.plugin._event_identity(other_group)
        group_song = await self.favorite(3, other_group_owner)
        await self.dispatch("收藏列表", other)
        await self.dispatch("收藏列表", other_group)
        await self.favorite(4)
        self.assertEqual(await self.plugin._favorite_song(self.owner, 1), shown)
        self.assertEqual(await self.plugin._favorite_song(other_owner, 1), other_song)
        self.assertEqual(await self.plugin._favorite_song(other_group_owner, 1), group_song)
        await self.dispatch("播放收藏 1", other_group)
        self.assertEqual(
            self.plugin.state.snapshot("queues", other_key)[0]["song"]["video_id"],
            group_song.video_id,
        )
        self.assertFalse(self.plugin.state.snapshot("queues", self.key, default=[]))

    async def test_favorite_view_registry_is_bounded_and_keeps_the_newest_display(self):
        for index in range(500):
            self.plugin._favorite_views[("session", str(index))] = (self.song.video_id,)
        await self.favorite(1)
        await self.dispatch("收藏列表")
        self.assertEqual(len(self.plugin._favorite_views), 500)
        self.assertNotIn(("session", "0"), self.plugin._favorite_views)
        self.assertIn(self.owner, self.plugin._favorite_views)
        await self.plugin.terminate()
        self.assertFalse(self.plugin._favorite_views)

    async def test_evicted_view_rejects_play_and_remove_without_mutating_state(self):
        shown = await self.favorite(1)
        await self.dispatch("收藏列表")
        for index in range(499):
            self.plugin._favorite_views[("session", str(index))] = (shown.video_id,)
        await self.dispatch("收藏列表", ChatEvent(sender="999"))
        self.assertTrue(self.plugin._favorite_views_limited)
        self.assertNotIn(self.owner, self.plugin._favorite_views)
        await self.favorite(2)
        before_state = self.plugin.state.snapshot()
        before_library = self.plugin.library.store.snapshot()
        for argument in ("播放收藏 1", "取消收藏 1"):
            with (
                self.subTest(argument=argument),
                self.assertRaisesRegex(module.UserFacingError, "请先查看 /点歌 收藏列表"),
            ):
                await self.dispatch(argument)
            self.assertEqual(self.plugin.state.snapshot(), before_state)
            self.assertEqual(self.plugin.library.store.snapshot(), before_library)

    async def test_overflow_requires_display_even_for_a_previously_unseen_owner(self):
        for index in range(500):
            self.plugin._favorite_views[("session", str(index))] = (self.song.video_id,)
        await self.dispatch("收藏列表")
        other = ChatEvent(sender="999")
        _, owner, _ = await self.plugin._event_identity(other)
        await self.favorite(1, owner)
        await self.favorite(2, owner)
        self.assertTrue(self.plugin._favorite_views_limited)
        self.assertNotIn(owner, self.plugin._favorite_views)
        before_state = self.plugin.state.snapshot()
        before_library = self.plugin.library.store.snapshot()
        for argument in ("播放收藏 1", "取消收藏 1"):
            with (
                self.subTest(argument=argument),
                self.assertRaisesRegex(module.UserFacingError, "请先查看 /点歌 收藏列表"),
            ):
                await self.dispatch(argument, other)
            self.assertEqual(self.plugin.state.snapshot(), before_state)
            self.assertEqual(self.plugin.library.store.snapshot(), before_library)
        self.assertNotIn(owner, self.plugin._favorite_views)

    async def test_redisplay_after_eviction_restores_selection_and_keeps_resident_mapping(self):
        shown = await self.favorite(1)
        await self.dispatch("收藏列表")
        resident = ChatEvent(sender="999")
        _, resident_owner, _ = await self.plugin._event_identity(resident)
        resident_shown = await self.favorite(3, resident_owner)
        await self.dispatch("收藏列表", resident)
        for index in range(498):
            self.plugin._favorite_views[("session", str(index))] = (shown.video_id,)
        await self.dispatch("收藏列表", ChatEvent(sender="777"))
        self.assertNotIn(self.owner, self.plugin._favorite_views)
        replacement = await self.favorite(2)
        resident_new = await self.favorite(4, resident_owner)
        self.assertIn("Track 3", await self.dispatch("播放收藏 1", resident))
        self.assertIn("1. Track 2", await self.dispatch("收藏列表"))
        self.assertTrue(self.plugin._favorite_views_limited)
        self.assertEqual(len(self.plugin._favorite_views), 500)
        self.assertEqual(self.plugin._favorite_views[resident_owner], (resident_shown.video_id,))
        self.assertEqual(
            self.plugin._favorite_views[self.owner], (replacement.video_id, shown.video_id)
        )
        self.assertIn("Track 2", await self.dispatch("播放收藏 1"))
        self.assertEqual(
            [row["song"]["video_id"] for row in self.plugin.state.snapshot("queues", self.key)],
            [resident_shown.video_id, replacement.video_id],
        )
        self.assertEqual(await self.dispatch("取消收藏 1"), "已移除收藏。")
        self.assertEqual(self.plugin.library.favorites(self.owner), [shown])
        self.assertEqual(
            self.plugin.library.favorites(resident_owner), [resident_new, resident_shown]
        )

    async def test_concurrent_removal_returns_an_error_without_claiming_another_song_was_removed(
        self,
    ):
        await self.favorite(1)
        await self.dispatch("收藏列表")
        with patch.object(self.plugin.library, "remove_favorite", return_value=False):
            with self.assertRaisesRegex(module.UserFacingError, "已不在收藏"):
                await self.dispatch("取消收藏 1")
        self.assertEqual(self.plugin.library.favorites(self.owner), [workflow_song(1)])


class SearchDeliveryCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.api = OneBot()
        self.plugin._start_worker = lambda key: None
        self.plugin.service.search = AsyncMock(return_value=[self.song])
        self.plugin._search_recall.delay = 60
        self.plugin._search_recall.timeout = 1

    async def command(self, argument, *, sender="456", group="123", platform="qq"):
        event = qq_event(
            self.api, sender=sender, group=group, platform=platform, message=f"/点歌 {argument}"
        )
        return [reply async for reply in self.plugin.song_command(event)]

    async def wait_delivery(self, owner=None):
        owner = owner or self.owner
        for _ in range(200):
            if owner in self.plugin._search_deliveries and self.plugin._search_recall._records:
                return self.plugin._search_deliveries[owner]
            await asyncio.sleep(0.005)
        self.fail("Search result did not enter tracked delivery")

    async def settle_recall(self):
        tasks = [
            record.task for record in self.plugin._search_recall._records.values() if record.task
        ]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

    def text(self, replies):
        self.assertEqual(len(replies), 1)
        return "".join(component.text for component in replies[0].chain)

    def actions(self, action):
        return [params for name, params in self.api.calls if name == action]

    async def test_cancel_waiting_result_prevents_submission_and_preserves_queue(self):
        await self.plugin.enqueue(self.key, workflow_song(2), self.owner, "Member")
        before = self.plugin.state.snapshot("queues", self.key)
        gate = self.plugin._search_recall.semaphore
        await gate.acquire()
        search = asyncio.create_task(self.command("Track"))
        try:
            delivery = await self.wait_delivery()
            self.assertFalse(self.plugin._search_tasks)
            self.assertIn("已取消你的搜索", self.text(await self.command("取消搜索")))
            self.assertTrue(delivery["cancelled"])
            self.assertFalse(self.plugin._candidates[self.owner]["selectable"])
        finally:
            gate.release()
        self.assertEqual(await search, [])
        self.assertFalse(self.api.calls)
        self.assertFalse(self.plugin._search_deliveries)
        self.assertFalse(self.plugin._search_recall._records)
        self.assertEqual(gate._value, 1)
        self.assertEqual(self.plugin.state.snapshot("queues", self.key), before)

    async def test_cancelled_gate_timeout_is_silent_and_does_not_leak_capacity(self):
        self.plugin._search_recall.timeout = 0.2
        gate = self.plugin._search_recall.semaphore
        await gate.acquire()
        search = asyncio.create_task(self.command("Track"))
        try:
            await self.wait_delivery()
            self.assertIn("已取消你的搜索", self.text(await self.command("取消搜索")))
            self.assertEqual(await asyncio.wait_for(search, 1), [])
            self.assertFalse(self.plugin._search_deliveries)
            self.assertFalse(self.plugin._search_recall._records)
            self.assertEqual(gate._value, 0)
            self.assertFalse(self.api.calls)
        finally:
            gate.release()
        self.assertEqual(gate._value, 1)

    async def test_other_member_group_and_platform_cannot_cancel_pending_delivery(self):
        gate = self.plugin._search_recall.semaphore
        await gate.acquire()
        search = asyncio.create_task(self.command("Track"))
        try:
            delivery = await self.wait_delivery()
            for identity in (
                {"sender": "999"},
                {"group": "321"},
                {"platform": "qq2"},
            ):
                self.assertIn(
                    "没有进行中的搜索", self.text(await self.command("取消搜索", **identity))
                )
                self.assertFalse(delivery["cancelled"])
                self.assertTrue(self.plugin._candidates[self.owner]["selectable"])
        finally:
            gate.release()
        self.assertEqual(await search, [])
        self.assertEqual(len(self.actions("send_group_msg")), 1)

    async def test_cancel_after_api_submission_preserves_late_receipt_and_recalls_only_that_result(
        self,
    ):
        self.api.block = "send_group_msg"
        search = asyncio.create_task(self.command("Track"))
        await asyncio.wait_for(self.api.entered.wait(), 2)
        self.assertIn("若结果已提交", self.text(await self.command("取消搜索")))
        self.assertFalse(search.cancelled())
        self.assertFalse(search.done())
        self.api.release.set()
        self.assertEqual(await search, [])
        await self.settle_recall()
        self.assertEqual(len(self.actions("send_group_msg")), 1)
        self.assertEqual(self.actions("get_msg"), [{"self_id": 888, "message_id": 101}])
        self.assertEqual(self.actions("delete_msg"), [{"self_id": 888, "message_id": 101}])
        self.assertFalse(self.plugin._search_deliveries)

    async def test_cancelled_inflight_api_timeout_does_not_add_a_second_message_or_retry(self):
        self.api.block = "send_group_msg"
        self.plugin._search_recall.timeout = 0.2
        search = asyncio.create_task(self.command("Track"))
        await asyncio.wait_for(self.api.entered.wait(), 2)
        self.assertIn("已取消你的搜索", self.text(await self.command("取消搜索")))
        self.assertEqual(await asyncio.wait_for(search, 1), [])
        self.assertEqual(len(self.actions("send_group_msg")), 1)
        self.assertFalse(self.actions("get_msg"))
        self.assertFalse(self.actions("delete_msg"))
        self.assertFalse(self.plugin._search_deliveries)
        self.assertFalse(self.plugin._search_recall._records)

    async def test_cancelled_unknown_receipt_does_not_send_a_fallback_message(self):
        self.api.block = "send_group_msg"
        self.api.receipt = None
        search = asyncio.create_task(self.command("Track"))
        await asyncio.wait_for(self.api.entered.wait(), 2)
        await self.command("取消搜索")
        self.api.release.set()
        self.assertEqual(await search, [])
        self.assertEqual(len(self.actions("send_group_msg")), 1)
        self.assertFalse(self.plugin._candidates[self.owner]["selectable"])
        self.assertFalse(self.plugin._search_recall._records)

    async def test_new_search_after_cancellation_is_not_invalidated_by_the_old_receipt(self):
        self.api.block = "send_group_msg"
        old = asyncio.create_task(self.command("Old words"))
        await asyncio.wait_for(self.api.entered.wait(), 2)
        old_delivery = self.plugin._search_deliveries[self.owner]
        await self.command("取消搜索")
        new = asyncio.create_task(self.command("New words"))
        for _ in range(200):
            delivery = self.plugin._search_deliveries.get(self.owner)
            if delivery is not None and delivery is not old_delivery:
                break
            await asyncio.sleep(0.005)
        else:
            self.fail("Replacement search did not enter delivery")
        self.assertFalse(delivery["cancelled"])
        new_token = delivery["reply"].token
        self.api.release.set()
        self.assertEqual(await old, [])
        self.assertEqual(await new, [])
        old_tasks = [
            record.task
            for record in self.plugin._search_recall._records.values()
            if record.reply.token != new_token and record.task
        ]
        if old_tasks:
            await asyncio.wait_for(asyncio.gather(*old_tasks), 2)
        self.assertTrue(self.plugin._candidates[self.owner]["selectable"])
        self.assertEqual(self.plugin._candidates[self.owner]["token"], new_token)
        self.assertEqual(len(self.actions("send_group_msg")), 2)
        self.assertEqual([params["message_id"] for params in self.actions("delete_msg")], [101])
        new_record = self.plugin._search_recall._records[(self.owner, new_token)]
        self.assertFalse(new_record.trigger.is_set())
        self.assertFalse(self.plugin._search_deliveries)

    async def test_termination_cleans_pending_delivery_without_submitting_it(self):
        gate = self.plugin._search_recall.semaphore
        await gate.acquire()
        search = asyncio.create_task(self.command("Track"))
        try:
            await self.wait_delivery()
            await self.plugin.terminate()
            with self.assertRaises(asyncio.CancelledError):
                await search
            self.assertFalse(self.plugin._search_deliveries)
            self.assertFalse(self.plugin._search_recall._records)
            self.assertFalse(self.api.calls)
        finally:
            gate.release()
        self.assertEqual(gate._value, 1)
