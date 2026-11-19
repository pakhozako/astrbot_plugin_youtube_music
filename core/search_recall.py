"""Recall only search replies sent here, using the documented OneBot client."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .service import UserFacingError

Owner = tuple[str, str]


class _SendResourceTimeout(Exception):
    pass


class _SupersededSearch(Exception):
    pass


@dataclass(frozen=True)
class SearchReply:
    owner: Owner
    token: str
    text: str


@dataclass
class _SentSearch:
    reply: SearchReply
    api: Any
    self_id: int
    target: int
    group: bool
    message_id: int | None = None
    trigger: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = "timeout"
    task: asyncio.Task | None = None


class SearchRecall:
    def __init__(
        self,
        *,
        delay: int,
        semaphore: asyncio.Semaphore,
        is_current: Callable[[Owner, str], bool],
        timeout: float = 10,
        capacity: int = 500,
    ):
        self.delay = delay
        self.semaphore = semaphore
        self.is_current = is_current
        self.timeout = timeout
        self.capacity = capacity
        self._records: dict[tuple[Owner, str], _SentSearch] = {}
        self._closed = False

    @staticmethod
    def _valid_message_id(value):
        return type(value) is int and value != 0 and -(2**63) <= value < 2**63

    async def _call(self, record, action, *, sending=False, **params):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        try:
            async with asyncio.timeout_at(deadline):
                await self.semaphore.acquire()
        except TimeoutError:
            raise _SendResourceTimeout from None
        try:
            if self._closed:
                raise asyncio.CancelledError
            if sending and not self.is_current(record.reply.owner, record.reply.token):
                raise _SupersededSearch
            if loop.time() >= deadline:
                raise _SendResourceTimeout
            async with asyncio.timeout_at(deadline):
                return await record.api.call_action(action, self_id=record.self_id, **params)
        finally:
            self.semaphore.release()

    async def send(self, event: AiocqhttpMessageEvent, reply: SearchReply):
        if self._closed:
            raise UserFacingError("插件已停用，未发送搜索结果。")
        if not self.is_current(reply.owner, reply.token):
            raise UserFacingError("搜索结果已过期或被替换，未发送旧结果。")
        if not isinstance(event, AiocqhttpMessageEvent):
            raise UserFacingError("无法确认 OneBot 事件，未发送可撤回消息。")
        self_id = event.message_obj.self_id
        group_id = event.get_group_id()
        target = group_id or event.get_sender_id()
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9]{1,19}", value)
            or not 0 < int(value) < 2**63
            for value in (self_id, target)
        ):
            raise UserFacingError("无法确认机器人或目标身份，未发送搜索结果。")
        key = (reply.owner, reply.token)
        if key in self._records:
            raise UserFacingError("搜索结果已提交，请勿重复发送。")
        if len(self._records) >= self.capacity:
            raise UserFacingError("待撤回搜索消息已达上限，请稍后重试。")
        record = _SentSearch(reply, event.bot.api, int(self_id), int(target), bool(group_id))
        self._records[key] = record
        try:
            response = await self._call(
                record,
                "send_group_msg" if record.group else "send_private_msg",
                sending=True,
                **{"group_id" if record.group else "user_id": record.target},
                message=[{"type": "text", "data": {"text": reply.text}}],
            )
            if not isinstance(response, dict) or not self._valid_message_id(
                response.get("message_id")
            ):
                logger.warning(
                    "[YouTubeMusic] search_recall untracked reason=invalid_send_receipt ref=%s",
                    reply.token,
                )
                return False
            record.message_id = response["message_id"]
            if self._closed:
                return
            if not self.is_current(reply.owner, reply.token):
                record.reason = "superseded"
                record.trigger.set()
            record.task = asyncio.create_task(
                self._wait_and_recall(key, record), name=f"youtube-search-recall-{reply.token}"
            )
            return True
        except _SendResourceTimeout:
            raise UserFacingError("发送资源繁忙，搜索结果尚未提交，请稍后重新搜索。") from None
        except _SupersededSearch:
            raise UserFacingError("搜索结果已过期或被替换，未发送旧结果。") from None
        except TimeoutError:
            logger.warning(
                "[YouTubeMusic] search_recall send_unknown reason=timeout ref=%s", reply.token
            )
            raise UserFacingError("搜索结果提交超时，结果未知。请核对 QQ 消息。") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[YouTubeMusic] search_recall send_failed error=%s ref=%s",
                type(exc).__name__,
                reply.token,
            )
            raise UserFacingError("搜索结果提交失败，请检查 QQ 连接和插件日志。") from None
        finally:
            if record.task is None:
                self._records.pop(key, None)

    def selected(self, owner: Owner, token: str):
        record = self._records.get((owner, token))
        if record:
            record.reason = "selected"
            record.trigger.set()

    def supersede(self, owner: Owner):
        for (record_owner, _), record in self._records.items():
            if record_owner == owner:
                record.reason = "superseded"
                record.trigger.set()

    def _matches(self, record, message):
        if (
            not isinstance(message, dict)
            or type(message.get("message_id")) is not int
            or message["message_id"] != record.message_id
            or message.get("message_type") != ("group" if record.group else "private")
            or not isinstance(message.get("sender"), dict)
            or type(message["sender"].get("user_id")) is not int
            or message["sender"]["user_id"] != record.self_id
        ):
            return False
        target_key = "group_id" if record.group else "user_id"
        if target_key in message and (
            type(message[target_key]) is not int or message[target_key] != record.target
        ):
            return False
        return message.get("message") == [{"type": "text", "data": {"text": record.reply.text}}]

    async def _wait_and_recall(self, key, record):
        try:
            try:
                await asyncio.wait_for(record.trigger.wait(), self.delay)
            except TimeoutError:
                pass
            if self._closed:
                return
            message = await self._call(record, "get_msg", message_id=record.message_id)
            if self._closed:
                return
            if not self._matches(record, message):
                logger.warning(
                    "[YouTubeMusic] search_recall rejected reason=identity_or_content ref=%s",
                    record.reply.token,
                )
                return
            await self._call(record, "delete_msg", message_id=record.message_id)
            logger.info(
                "[YouTubeMusic] search_recall submitted reason=%s ref=%s",
                record.reason,
                record.reply.token,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[YouTubeMusic] search_recall stopped reason=%s error=%s ref=%s",
                record.reason,
                type(exc).__name__,
                record.reply.token,
            )
        finally:
            self._records.pop(key, None)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._records:
            logger.info("[YouTubeMusic] search_recall closed pending=%d", len(self._records))
        tasks = [record.task for record in self._records.values() if record.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._records.clear()
