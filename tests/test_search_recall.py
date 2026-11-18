"""Real AstrBot event types with a controlled OneBot API, not real QQ delivery."""

import asyncio
import copy
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType, PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from core.search_recall import SearchRecall, SearchReply
from core.service import UserFacingError


class OneBot:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.next_id = 101
        self.receipt = "valid"
        self.inspect_message = None
        self.fail = ""
        self.block = ""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.block == action:
            self.entered.set()
            await self.release.wait()
        if self.fail == action:
            raise RuntimeError("private token=do-not-log")
        if action in {"send_group_msg", "send_private_msg"}:
            message_id = self.next_id
            self.next_id += 1
            group = action == "send_group_msg"
            self.messages[message_id] = {
                "message_id": message_id,
                "message_type": "group" if group else "private",
                "sender": {"user_id": params["self_id"]},
                "message": copy.deepcopy(params["message"]),
                "group_id" if group else "user_id": params["group_id" if group else "user_id"],
            }
            return {"message_id": message_id} if self.receipt == "valid" else self.receipt
        if action == "get_msg":
            message = copy.deepcopy(self.messages[params["message_id"]])
            return self.inspect_message(message) if self.inspect_message else message
        if action == "delete_msg":
            self.messages.pop(params["message_id"], None)
            return None
        raise AssertionError(f"Unexpected OneBot action: {action}")


def qq_event(api, *, group="123", sender="456", platform="qq", message="/点歌 Track"):
    data = AstrBotMessage()
    data.self_id = "888"
    data.message_id = "999999"
    data.sender = MessageMember(user_id=sender, nickname="Member")
    data.group_id = group
    data.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    data.session_id = group or sender
    data.message = []
    data.message_str = message
    data.raw_message = {}
    return AiocqhttpMessageEvent(
        message,
        data,
        PlatformMetadata(name="aiocqhttp", description="Test", id=platform),
        group or sender,
        SimpleNamespace(api=api),
    )


class RecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = OneBot()
        self.current = {}
        self.manager = SearchRecall(
            delay=0.02,
            semaphore=asyncio.Semaphore(1),
            is_current=lambda owner, token: self.current.get(owner) == token,
            timeout=0.2,
        )
        self.addAsyncCleanup(self.manager.close)

    def reply(self, owner=("session", "456")):
        token = uuid.uuid4().hex
        self.current[owner] = token
        return SearchReply(owner, token, f"YouTube 搜索结果 · {token}\n1. Track")

    async def settle(self):
        tasks = [record.task for record in self.manager._records.values() if record.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 2)

    def actions(self, action):
        return [params for name, params in self.api.calls if name == action]

    async def test_timeout_recalls_only_id_returned_by_search_send(self):
        await self.manager.send(qq_event(self.api), self.reply())
        await self.settle()
        self.assertEqual(self.actions("delete_msg"), [{"self_id": 888, "message_id": 101}])
        self.assertEqual(self.actions("get_msg"), [{"self_id": 888, "message_id": 101}])
        self.assertNotIn(999999, [call["message_id"] for call in self.actions("delete_msg")])
        self.assertFalse(self.manager._records)

    async def test_selection_and_timeout_make_only_one_recall_attempt(self):
        reply = self.reply()
        await self.manager.send(qq_event(self.api), reply)
        self.manager.selected(reply.owner, reply.token)
        self.manager.selected(reply.owner, reply.token)
        await self.settle()
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.actions("delete_msg")), 1)

    async def test_private_route_and_negative_message_ids_are_supported(self):
        self.api.next_id = -101
        await self.manager.send(qq_event(self.api, group=""), self.reply())
        await self.settle()
        self.assertEqual(self.actions("send_private_msg")[0]["user_id"], 456)
        self.assertEqual(self.actions("delete_msg")[0]["message_id"], -101)
        self.assertFalse(self.actions("send_group_msg"))

    async def test_user_message_is_never_recalled_even_with_bad_receipt(self):
        self.api.inspect_message = lambda message: {
            **message,
            "sender": {"user_id": 456},
        }
        await self.manager.send(qq_event(self.api), self.reply())
        await self.settle()
        self.assertFalse(self.actions("delete_msg"))

    async def test_other_bot_business_message_is_never_recalled(self):
        self.api.inspect_message = lambda message: {
            **message,
            "message": [{"type": "text", "data": {"text": "队列状态"}}],
        }
        await self.manager.send(qq_event(self.api), self.reply())
        await self.settle()
        self.assertFalse(self.actions("delete_msg"))

    async def test_wrong_id_type_session_and_unverifiable_content_are_rejected(self):
        mutations = [
            lambda message: {**message, "message_id": 999999},
            lambda message: {**message, "sender": {"user_id": "888"}},
            lambda message: {**message, "sender": {"user_id": 889}},
            lambda message: {**message, "message_type": "private"},
            lambda message: {**message, "group_id": 987},
            lambda message: {**message, "message": "a CQ-code string"},
            lambda message: None,
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.api.inspect_message = mutate
                await self.manager.send(qq_event(self.api), self.reply())
                await self.settle()
        self.assertFalse(self.actions("delete_msg"))

    async def test_missing_optional_group_id_does_not_invent_a_protocol_field(self):
        def without_group(message):
            message.pop("group_id")
            return message

        self.api.inspect_message = without_group
        await self.manager.send(qq_event(self.api), self.reply())
        await self.settle()
        self.assertEqual(len(self.actions("delete_msg")), 1)

    async def test_invalid_receipts_are_not_replaced_with_inbound_or_nested_ids(self):
        for receipt in (
            None,
            {},
            {"data": {"message_id": 999999}},
            {"message_id": True},
            {"message_id": "101"},
            {"message_id": 0},
            {"message_id": 2**64},
        ):
            with self.subTest(receipt=receipt):
                self.api.receipt = receipt
                await self.manager.send(qq_event(self.api), self.reply())
                self.assertFalse(self.manager._records)
        self.assertFalse(self.actions("get_msg"))
        self.assertFalse(self.actions("delete_msg"))

    async def test_api_errors_stop_without_retry_or_secret_logging(self):
        with patch("core.search_recall.logger") as log:
            for action in ("get_msg", "delete_msg"):
                self.api.fail = action
                await self.manager.send(qq_event(self.api), self.reply())
                await self.settle()
            self.assertNotIn("do-not-log", str(log.mock_calls))
        self.assertEqual(len(self.actions("get_msg")), 2)
        self.assertEqual(len(self.actions("delete_msg")), 1)

    async def test_send_failure_does_not_track_or_resend(self):
        self.api.fail = "send_group_msg"
        with self.assertRaises(UserFacingError):
            await self.manager.send(qq_event(self.api), self.reply())
        self.assertFalse(self.manager._records)
        self.assertEqual(len(self.actions("send_group_msg")), 1)
        self.assertFalse(self.actions("delete_msg"))

    async def test_send_timeout_is_unknown_and_never_retried(self):
        self.api.block = "send_group_msg"
        self.manager.timeout = 0.02
        with self.assertRaisesRegex(UserFacingError, "结果未知"):
            await self.manager.send(qq_event(self.api), self.reply())
        self.assertEqual(len(self.actions("send_group_msg")), 1)
        self.assertFalse(self.manager._records)
        self.assertFalse(self.actions("delete_msg"))

    async def test_selection_during_pending_send_is_not_lost(self):
        self.api.block = "send_group_msg"
        self.manager.delay = 60
        reply = self.reply()
        task = asyncio.create_task(self.manager.send(qq_event(self.api), reply))
        await self.api.entered.wait()
        self.manager.selected(reply.owner, reply.token)
        self.api.release.set()
        await task
        await self.settle()
        self.assertEqual(len(self.actions("delete_msg")), 1)

    async def test_superseded_late_receipt_is_recalled_not_attached_to_new_generation(self):
        self.api.block = "send_group_msg"
        self.manager.delay = 60
        old = self.reply()
        task = asyncio.create_task(self.manager.send(qq_event(self.api), old))
        await self.api.entered.wait()
        self.reply(old.owner)
        self.api.release.set()
        await task
        await self.settle()
        self.assertEqual(len(self.actions("delete_msg")), 1)

    async def test_owner_and_generation_isolation(self):
        self.manager.delay = 60
        first = self.reply(("qq-group-a", "456"))
        second = self.reply(("qq-group-b", "456"))
        await self.manager.send(qq_event(self.api), first)
        await self.manager.send(qq_event(self.api, group="321", platform="qq2"), second)
        self.manager.selected(first.owner, first.token)
        await self.manager._records[(first.owner, first.token)].task
        self.assertEqual([item["message_id"] for item in self.actions("delete_msg")], [101])
        self.manager.selected(second.owner, first.token)
        self.assertFalse(self.manager._records[(second.owner, second.token)].trigger.is_set())
        self.manager.supersede(second.owner)
        await self.settle()
        self.assertEqual([item["message_id"] for item in self.actions("delete_msg")], [101, 102])

    async def test_capacity_is_reserved_before_network_io(self):
        self.manager.capacity = 1
        self.api.block = "send_group_msg"
        task = asyncio.create_task(self.manager.send(qq_event(self.api), self.reply()))
        await self.api.entered.wait()
        with self.assertRaisesRegex(UserFacingError, "上限"):
            await self.manager.send(qq_event(self.api), self.reply(("other", "456")))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.manager._records)
        self.assertEqual(len(self.actions("send_group_msg")), 1)

    async def test_close_stops_timer_and_old_instance_cannot_send(self):
        self.manager.delay = 60
        await self.manager.send(qq_event(self.api), self.reply())
        await self.manager.close()
        self.assertFalse(self.manager._records)
        self.assertFalse(self.actions("get_msg"))
        with self.assertRaises(UserFacingError):
            await self.manager.send(qq_event(self.api), self.reply())

    async def test_close_during_lookup_does_not_delete(self):
        self.api.block = "get_msg"
        await self.manager.send(qq_event(self.api), self.reply())
        await self.api.entered.wait()
        await self.manager.close()
        self.assertFalse(self.actions("delete_msg"))
        self.assertFalse(self.manager._records)

    async def test_non_onebot_event_cannot_send_trackable_messages(self):
        with self.assertRaises(UserFacingError):
            await self.manager.send(SimpleNamespace(), self.reply())
        self.assertFalse(self.api.calls)

    async def test_invalid_bot_and_target_identity_is_rejected_before_any_api_call(self):
        for value in ("", "0", "-1", "\u0661", str(2**63), True, None):
            for field in ("self_id", "group_id"):
                with self.subTest(value=value, field=field):
                    event = qq_event(self.api)
                    setattr(event.message_obj, field, value)
                    if field == "group_id" and not value:
                        event.message_obj.sender.user_id = value
                    with self.assertRaises(UserFacingError):
                        await self.manager.send(event, self.reply())
        self.assertFalse(self.api.calls)
        self.assertFalse(self.manager._records)

    async def test_same_reply_cannot_be_sent_twice_while_tracked(self):
        self.manager.delay = 60
        reply = self.reply()
        await self.manager.send(qq_event(self.api), reply)
        with self.assertRaisesRegex(UserFacingError, "重复"):
            await self.manager.send(qq_event(self.api), reply)
        self.assertEqual(len(self.actions("send_group_msg")), 1)

    async def test_close_during_send_receipt_cannot_create_a_late_timer(self):
        self.api.block = "send_group_msg"
        task = asyncio.create_task(self.manager.send(qq_event(self.api), self.reply()))
        await self.api.entered.wait()
        await self.manager.close()
        self.api.release.set()
        await task
        self.assertFalse(self.manager._records)
        self.assertFalse(self.actions("get_msg"))

    async def test_send_lookup_and_delete_share_one_concurrency_gate(self):
        active = peak = 0
        call = self.api.call_action

        async def tracked_call(action, **params):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.003)
                return await call(action, **params)
            finally:
                active -= 1

        self.api.call_action = tracked_call
        await asyncio.gather(
            self.manager.send(qq_event(self.api), self.reply(("first", "456"))),
            self.manager.send(qq_event(self.api, group="321"), self.reply(("second", "456"))),
        )
        await self.settle()
        self.assertEqual(peak, 1)
        self.assertEqual(len(self.actions("delete_msg")), 2)


class PluginRecallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_runtime

        await test_runtime.RuntimeTests.asyncSetUp(self)
        self.api = OneBot()
        self.event = qq_event(self.api)
        self.plugin.service.search = AsyncMock(return_value=[self.song])
        self.plugin._search_recall.delay = 60
        self.plugin._start_worker = lambda key: None

    async def command(self, argument):
        self.event.message_str = f"/点歌 {argument}"
        return [result async for result in self.plugin.song_command(self.event)]

    async def settle(self):
        tasks = [row.task for row in self.plugin._search_recall._records.values() if row.task]
        await asyncio.wait_for(asyncio.gather(*tasks), 2)

    async def test_only_search_result_is_directly_sent_and_tracked(self):
        self.assertEqual(await self.command("Track"), [])
        self.assertEqual(len(self.plugin._search_recall._records), 1)
        for argument in ("队列", "帮助", "收藏列表", "最近"):
            self.assertEqual(len(await self.command(argument)), 1)
        self.assertEqual([action for action, _ in self.api.calls], ["send_group_msg"])

    async def test_valid_selection_recalls_but_invalid_selection_does_not(self):
        await self.command("Track")
        await self.command("999")
        self.assertFalse(any(action == "delete_msg" for action, _ in self.api.calls))
        await self.command("1")
        await self.settle()
        self.assertEqual(sum(action == "delete_msg" for action, _ in self.api.calls), 1)
        self.assertEqual(len(self.plugin.state.snapshot()["queues"][self.key]), 1)

    async def test_successful_favorite_recalls_search_only(self):
        await self.command("Track")
        await self.command("收藏 1")
        await self.settle()
        self.assertEqual(len(self.plugin.library.favorites(self.owner)), 1)
        self.assertEqual(sum(action == "delete_msg" for action, _ in self.api.calls), 1)

    async def test_failed_enqueue_does_not_trigger_early_recall(self):
        await self.command("Track")
        with patch.object(self.plugin, "enqueue", new=AsyncMock(side_effect=UserFacingError("满"))):
            await self.command("1")
        self.assertFalse(next(iter(self.plugin._search_recall._records.values())).trigger.is_set())

    async def test_disabled_setting_uses_normal_reply_without_protocol_calls(self):
        from dataclasses import replace

        self.plugin.settings = replace(self.plugin.settings, search_result_auto_recall=False)
        self.assertEqual(len(await self.command("Track")), 1)
        self.assertFalse(self.api.calls)
        self.assertFalse(self.plugin._search_recall._records)

    async def test_ai_search_is_not_registered_as_a_recallable_llm_reply(self):
        await self.plugin.ai_music_action(self.event, "search", "Track")
        self.assertFalse(self.api.calls)
        self.assertFalse(self.plugin._search_recall._records)

    async def test_ai_confirmed_index_reuses_chat_search_receipt(self):
        await self.command("Track")
        await self.plugin.ai_music_action(self.event, "enqueue", index=1)
        await self.settle()
        self.assertEqual(sum(action == "delete_msg" for action, _ in self.api.calls), 1)

    async def test_old_selection_during_enqueue_does_not_recall_new_search(self):
        await self.command("Track")
        old_record = next(iter(self.plugin._search_recall._records.values()))
        entered, release = asyncio.Event(), asyncio.Event()

        async def enqueue(*args):
            entered.set()
            await release.wait()
            return 1

        with patch.object(self.plugin, "enqueue", new=AsyncMock(side_effect=enqueue)):
            choosing = asyncio.create_task(self.plugin.dispatch_music(self.event, "1"))
            await entered.wait()
            await self.command("New search")
            new_record = next(
                record
                for record in self.plugin._search_recall._records.values()
                if record is not old_record
            )
            release.set()
            await choosing
        await old_record.task
        self.assertFalse(new_record.trigger.is_set())
        self.assertEqual(
            [params["message_id"] for action, params in self.api.calls if action == "delete_msg"],
            [101],
        )

    async def test_song_send_and_search_recall_share_the_configured_limit(self):
        active = peak = 0
        call = self.api.call_action

        async def track(operation, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.01)
                return await operation(*args, **kwargs)
            finally:
                active -= 1

        self.api.call_action = lambda action, **params: track(call, action, **params)

        async def send(*args):
            return await track(AsyncMock(return_value=True))

        self.context.send_message.side_effect = send
        await self.command("Track")
        reply = next(iter(self.plugin._search_recall._records.values())).reply
        self.plugin._search_recall.selected(reply.owner, reply.token)
        await asyncio.gather(
            self.plugin._send_song(
                {"umo": "qq:GroupMessage:123"}, self.song, self.root / "track.wav"
            ),
            self.settle(),
        )
        self.assertEqual(peak, 1)
        self.assertEqual(sum(action == "delete_msg" for action, _ in self.api.calls), 1)
