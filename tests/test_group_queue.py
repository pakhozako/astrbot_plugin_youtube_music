"""Group-scoped admission, compact feedback and validated playback waiting."""

import asyncio
import time
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch


class GroupQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_runtime as runtime

        self.runtime = runtime
        await runtime.RuntimeTests.asyncSetUp(self)

    async def add(self, **kwargs):
        return await self.runtime.RuntimeTests.add(self, **kwargs)

    async def wait_for(self, predicate):
        await self.runtime.RuntimeTests.wait_for(self, predicate)

    def settings(self, **values):
        self.plugin.settings = replace(self.plugin.settings, **values)
        self.plugin.service.settings = self.plugin.settings

    async def current(self, *, submitted=True, remove=True, duration=60):
        await self.add(song=replace(self.song, duration=duration))
        row = self.plugin.state.snapshot("queues", self.key)[0]
        if remove:
            await self.runtime.module.run_blocking(
                self.plugin.state.update, lambda data: data["queues"][self.key].pop(0)
            )
        self.plugin._current[self.key] = {
            "row": row,
            "phase": "submitted" if submitted else "download",
            "submitted": submitted,
            "wait_until": time.monotonic() + 10,
        }
        return row

    async def test_wav_budget_rejects_admission_without_queue_or_network_changes(self):
        self.settings(max_wav_size_mb=1)
        before = self.plugin.state.path.read_bytes()
        with (
            patch.object(self.plugin, "_start_worker") as start,
            patch.object(self.plugin.service, "get_metadata", new=AsyncMock()) as metadata,
            self.assertRaisesRegex(self.runtime.module.UserFacingError, "WAV"),
        ):
            await self.plugin.enqueue(self.key, replace(self.song, duration=60), self.owner, "User")
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        start.assert_not_called()
        metadata.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_link_only_admission_does_not_apply_pcm_size_budget(self):
        self.settings(max_wav_size_mb=1, send_strategy="link_only")
        self.assertEqual(await self.add(song=replace(self.song, duration=60)), 1)
        self.assertEqual(len(self.plugin.state.snapshot("queues", self.key)), 1)
        self.context.send_message.assert_not_awaited()

    async def test_invalid_batch_member_rejects_the_complete_batch(self):
        self.settings(max_wav_size_mb=1)
        before = self.plugin.state.path.read_bytes()
        with (
            patch.object(self.plugin, "_start_worker") as start,
            self.assertRaisesRegex(self.runtime.module.UserFacingError, "WAV"),
        ):
            await self.plugin.enqueue_many(
                self.key,
                [self.song, replace(self.runtime.workflow_song(2), duration=60)],
                self.owner,
                "User",
            )
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        start.assert_not_called()

    async def test_current_submitted_song_is_not_admitted_again(self):
        await self.current()
        before = self.plugin.state.path.read_bytes()
        with self.assertRaisesRegex(self.runtime.module.UserFacingError, "\u6b63\u5728"):
            await self.add()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)
        with patch.object(self.plugin, "_start_worker") as start:
            result = await self.plugin.enqueue_many(
                self.key, [self.song], self.owner, "User", skip_existing=True
            )
        self.assertEqual(
            result,
            {
                "added": 0,
                "positions": [],
                "skipped": [self.song.video_id],
                "queue_size": 0,
            },
        )
        start.assert_not_called()
        self.assertEqual(self.plugin.state.path.read_bytes(), before)

    async def test_duplicate_detection_is_scoped_to_the_target_group(self):
        await self.current()
        key, owner, _ = await self.plugin._event_identity(
            self.runtime.ChatEvent(sender="789", group="234")
        )
        with patch.object(self.plugin, "_start_worker"):
            position = await self.plugin.enqueue(key, self.song, owner, "Other group")
        self.assertEqual(position, 1)
        self.assertFalse(self.plugin.state.snapshot("queues", self.key))
        self.assertEqual(
            self.plugin.state.snapshot("queues", key)[0]["owner"],
            self.plugin.library.owner_id(owner),
        )
        await self.plugin.queue_action(key, "cancel_mine", owner=owner)
        self.assertTrue(self.plugin._current[self.key]["submitted"])
        self.assertNotIn(self.key, self.plugin.state.snapshot("paused"))
        self.context.send_message.assert_not_awaited()

    async def test_concurrent_members_cannot_add_the_same_video_twice(self):
        with patch.object(self.plugin, "_start_worker"):
            results = await asyncio.gather(
                self.plugin.enqueue(self.key, self.song, self.owner, "First"),
                self.plugin.enqueue(self.key, self.song, (self.key, "789"), "Second"),
                return_exceptions=True,
            )
        self.assertEqual(results.count(1), 1)
        self.assertEqual(
            sum(isinstance(result, self.runtime.module.UserFacingError) for result in results),
            1,
        )
        self.assertEqual(len(self.plugin.state.snapshot("queues", self.key)), 1)
        self.context.send_message.assert_not_awaited()

    async def test_feedback_counts_a_submitted_head_once_before_state_finalization(self):
        await self.current(remove=False, duration=60)
        second = self.runtime.workflow_song(2)
        position = await self.add(song=second)
        with patch.object(self.runtime.module.time, "monotonic", return_value=100):
            self.plugin._current[self.key]["wait_until"] = 110
            text = self.plugin.enqueue_feedback(self.key, second, position)
        self.assertIn("\u524d\u65b9 1 \u9996", text)
        self.assertIn("0:10", text)
        self.assertNotIn("1:12", text)
        self.assertEqual(len(text.splitlines()), 2)
        self.context.send_message.assert_not_awaited()

    async def test_paused_feedback_and_queue_status_show_useful_state_and_requester(self):
        await self.add()
        second = self.runtime.workflow_song(2)
        position = await self.add(song=second, owner=(self.key, "789"))
        await self.plugin.queue_action(self.key, "pause", admin=True)
        text = self.plugin.enqueue_feedback(self.key, second, position)
        self.assertIn("\u5df2\u6682\u505c", text)
        self.assertNotIn("\u7b49\u5f85\u7ea6", text)
        self.assertEqual(len(text.splitlines()), 2)
        queue = self.plugin.queue_text(self.key)
        self.assertIn("\u70b9\u6b4c\uff1aUser", queue)
        self.assertIn("Artist", queue)
        self.context.send_message.assert_not_awaited()

    async def test_playback_wait_uses_validated_audio_duration_not_metadata(self):
        await self.add()
        self.plugin.service.download_and_convert = AsyncMock(
            return_value=(self.root / "track.wav", self.song, {"duration_seconds": 12.5})
        )
        self.plugin._start_worker(self.key)
        await self.wait_for(lambda: bool(self.plugin.library.history(self.owner)))
        current = self.plugin._current[self.key]
        remaining = current["wait_until"] - time.monotonic()
        self.assertTrue(current["submitted"])
        self.assertGreater(remaining, 12)
        self.assertLessEqual(remaining, 14.5)
        self.assertEqual(self.context.send_message.await_count, 1)
        await self.plugin.queue_action(self.key, "cancel_mine", owner=self.owner)
        self.assertIn(self.key, self.plugin._workers)
        self.assertEqual(self.context.send_message.await_count, 1)
        await self.plugin.queue_action(self.key, "pause", admin=True)


if __name__ == "__main__":
    unittest.main()
