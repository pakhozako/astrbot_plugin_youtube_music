"""QQ music requests share one bounded queue, permission policy and lifecycle."""

from __future__ import annotations

import asyncio
import math
import random
import re
import shutil
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp
import astrbot.api.message_components as Comp
from aiocqhttp.exceptions import NetworkError
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.ai_tools import YouTubeMusicTool
from .core.audio_cache import AudioCache
from .core.dependency_manager import DependencyManager
from .core.library import UserLibrary
from .core.maintenance import run_blocking
from .core.queue import MAX_BATCH_SIZE, JobLimiter, pending_queue_summary
from .core.search_quality import version_label
from .core.search_recall import SearchRecall, SearchReply
from .core.service import UserFacingError, YouTubeMusicService
from .core.settings import Settings
from .core.state import QueueState, session_key
from .core.store_validation import valid_int, valid_song, valid_thumbnail_url
from .core.utils import (
    Song,
    cleanup_stale_owned_job_dirs,
    extract_command_argument,
    format_duration,
    is_owned_job_dir,
    mark_owned_job_dir,
    parse_youtube_url,
    safe_text,
)
from .core.webui import WebUIMixin

PLUGIN_NAME = "astrbot_plugin_youtube_music"
HELP_TEXT = """YouTube 点歌
/点歌 <关键词>：搜索
/点歌 歌手 <名称>：搜索歌手作品
/点歌 <编号> [结果码] / <视频链接>：加入队列
/点歌 下一页 / 上一页 / 翻页 <页码>
/点歌 重搜 [关键词] / 取消搜索
/点歌 详情 <编号> [结果码]：歌曲信息与封面
/点歌 队列 / 状态：查看队列
/点歌 我的队列：查看本人的待播与当前歌曲
/点歌 收藏 <编号> [结果码] / 收藏列表 / 播放收藏 <编号>
/点歌 取消收藏 <编号> / 最近 / 清除历史
/点歌 重播 [历史编号] / 随机：个人收藏与历史
/点歌 取消：取消自己的待播歌曲
/点歌 移除 <队列编号>：移除自己的待播歌曲
队列管理员：/点歌 继续 / 暂停 / 下一首 / 清空队列
结果编号和结果码仅限本人当前会话；仅点播单个 YouTube 视频。"""


@dataclass(frozen=True)
class SongDetailsReply:
    text: str
    thumbnail_url: str = ""


class YouTubeMusicPlugin(WebUIMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.settings = Settings.from_config(config, data_dir=self.data_dir)
        self.job_root = self.data_dir / "cache" / "jobs"
        self._data_dir_real = self.data_dir.resolve()
        self._job_root_real = self.job_root.resolve()
        self.state = self.library = self.audio_cache = None
        self.dependency_manager = DependencyManager(self.data_dir, self.settings)
        self.dependency_gate = self.dependency_manager.dependency_gate
        self.service = YouTubeMusicService(self.settings, self.audio_cache)
        self.service.dependency_gate = self.dependency_gate
        self._limiter = JobLimiter(self.settings.max_concurrent_jobs, self.settings.max_queued_jobs)
        self._initialize_lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._workers: dict[str, asyncio.Task] = {}
        self._current: dict[str, dict] = {}
        self._errors: dict[str, str] = {}
        self._error_entry_ids: dict[str, str] = {}
        self._requests: set[asyncio.Task] = set()
        self._candidates: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._search_busy: set[tuple[str, str]] = set()
        self._search_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._search_deliveries: dict[tuple[str, str], dict] = {}
        self._history_views: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._favorite_views: OrderedDict[tuple[str, str], tuple[str, ...]] = OrderedDict()
        self._favorite_views_limited = False
        self._running = False
        self._terminating = False
        self._search_recall = SearchRecall(
            delay=self.settings.search_result_recall_delay_seconds,
            timeout=min(10, self.settings.send_timeout_seconds),
            semaphore=self.service._stage_limit("send_concurrency", self.settings.send_concurrency),
            is_current=self._is_current_result,
        )
        self._init_webui()
        tool = YouTubeMusicTool()
        tool.plugin = self
        self.context.add_llm_tools(tool)

    def _load_stores(self):
        for path in (self.job_root, self.data_dir / "cache" / "audio"):
            path.resolve().relative_to(self.data_dir.resolve())
        return (
            QueueState(self.data_dir / "queue_state.json"),
            UserLibrary(
                self.data_dir / "library.json",
                max_history=self.settings.max_history_items,
                max_favorites=self.settings.max_favorites_per_user,
            ),
            AudioCache(
                self.data_dir / "cache" / "audio",
                retention_hours=self.settings.audio_cache_retention_hours,
                max_size_mb=self.settings.audio_cache_max_mb,
            ),
        )

    async def initialize(self):
        if self._terminating:
            raise UserFacingError("插件已停用，不能初始化旧实例。")
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            async with self._initialize_lock:
                if self._terminating:
                    raise UserFacingError("插件已停用，不能初始化旧实例。")
                if self._running:
                    return
                self.state, self.library, self.audio_cache = await run_blocking(self._load_stores)
                self.service.audio_cache = self.audio_cache
                await run_blocking(self.job_root.mkdir, parents=True, exist_ok=True)
                await run_blocking(
                    cleanup_stale_owned_job_dirs, self.job_root, time.time() - 24 * 3600
                )
                await run_blocking(self.audio_cache.cleanup)

                def pause_saved(data):
                    for key, rows in data["queues"].items():
                        if rows and key not in data["paused"]:
                            data["paused"].append(key)

                await run_blocking(self.state.update, pause_saved)
                self._running = True
                logger.info("[YouTubeMusic] 已启动；持久队列等待明确的继续操作，不自动重播。")
        finally:
            self._requests.discard(task)

    async def terminate(self):
        self._terminating = True
        self._running = False
        self._close_events()
        await self._search_recall.close()
        await self.dependency_gate.close()
        current = asyncio.current_task()
        owned = (
            set(self._workers.values())
            | self._requests
            | set(self._search_tasks.values())
            | self.dependency_gate.task_snapshot()
        )
        if self._dependency_update_task is not None:
            owned.add(self._dependency_update_task)
        tasks = [task for task in owned if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._current.clear()
        self._candidates.clear()
        self._search_busy.clear()
        self._search_tasks.clear()
        self._search_deliveries.clear()
        self._history_views.clear()
        self._favorite_views.clear()
        if self.audio_cache is not None:
            self.audio_cache.protected_video_ids = set()

    def _require_running(self):
        if not self._running or self._terminating:
            raise UserFacingError("插件尚未就绪或正在停用，未执行操作。")

    @asynccontextmanager
    async def operation(self):
        self._require_running()
        task = asyncio.current_task()
        self._requests.add(task)
        try:
            yield
        finally:
            self._requests.discard(task)

    def _group_allowed(self, group_id: str):
        return (
            not group_id
            or not self.settings.allowed_group_ids
            or group_id in self.settings.allowed_group_ids
        )

    async def _event_identity(self, event):
        if event.get_platform_name() != "aiocqhttp":
            raise UserFacingError("此插件仅支持 QQ OneBot 会话。")
        group = str(event.get_group_id() or "")
        sender = str(event.get_sender_id() or "")
        if (
            not sender.isdecimal()
            or len(sender) > 24
            or group
            and (not group.isdecimal() or len(group) > 24)
        ):
            raise UserFacingError("无法确认 QQ 会话身份。")
        if not self._group_allowed(group):
            raise UserFacingError("当前群未获准使用点歌。")
        umo = str(event.unified_msg_origin)
        key = session_key(event.get_platform_id(), group, umo)
        label = f"QQ 群 {group}" if group else f"QQ 私聊 {sender}"
        async with self._queue_lock:

            def remember(payload):
                if key not in payload["sessions"]:
                    payload["sessions"][key] = {"umo": umo, "label": label, "group_id": group}

            await run_blocking(self.state.update, remember)
        owner = (key, sender)
        admin = not group or event.is_admin() or sender in self.settings.queue_admin_user_ids
        return key, owner, bool(admin)

    def _prune_candidates(self):
        now = time.monotonic()
        ttl = self.settings.result_ttl_minutes * 60
        for registry in (self._candidates, self._history_views):
            for owner in list(registry):
                if now - registry[owner]["at"] >= ttl:
                    registry.pop(owner)
            while len(registry) > 500:
                registry.popitem(last=False)

    def _is_current_result(self, owner, token):
        self._prune_candidates()
        record = self._candidates.get(owner)
        return bool(record and record.get("selectable", True) and record.get("token") == token)

    def _candidate_record(self, owner, *, code=None):
        self._prune_candidates()
        record = self._candidates.get(owner)
        if code is not None and (
            not isinstance(code, str)
            or not re.fullmatch(r"[a-fA-F0-9]{8}", code)
            or not record
            or record.get("generation", record.get("token", ""))[:8] != code.lower()
        ):
            raise UserFacingError("结果码已过期或不属于你，请重新搜索。")
        if not record or not record.get("selectable", True) or not record.get("songs"):
            raise UserFacingError("歌曲编号无效或已过期，请重新搜索。")
        return record

    async def search(
        self, owner: tuple[str, str], query: str, *, paged: bool = False
    ) -> list[Song]:
        self._require_running()
        if not isinstance(query, str) or not query.strip():
            raise UserFacingError("请输入搜索关键词。")
        query = query.strip()
        self._prune_candidates()
        previous = self._candidates.get(owner)
        if (
            owner in self._search_busy
            or previous
            and time.monotonic() - previous["at"] < self.settings.request_cooldown_seconds
        ):
            raise UserFacingError("搜索过于频繁，请稍后重试。")
        self._search_busy.add(owner)
        task = asyncio.current_task()
        self._search_tasks[owner] = task
        generation = uuid.uuid4().hex
        self._search_recall.supersede(owner)
        self._candidates[owner] = {
            "at": time.monotonic(),
            "songs": [],
            "token": generation,
            "generation": generation,
            "query": query,
            "page": 1,
            "selectable": False,
        }
        self._candidates.move_to_end(owner)
        self._prune_candidates()
        try:
            async with self._limiter.slot(wait_timeout=self.settings.queue_wait_timeout_seconds):
                url = parse_youtube_url(query)
                if query.lower().startswith(("http://", "https://")) and not url:
                    raise UserFacingError("仅支持单个 YouTube 视频链接。")
                songs = (
                    [await self.service.get_metadata(url)]
                    if url
                    else await self.service.search(query, candidate_pool=True)
                    if paged
                    else await self.service.search(query)
                )
            self._require_running()
            if paged:
                songs = songs[: self.settings.search_results * 3]
            self._candidates[owner] = {
                "at": time.monotonic(),
                "songs": songs,
                "token": generation,
                "generation": generation,
                "query": query,
                "page": 1,
                "selectable": True,
            }
            self._candidates.move_to_end(owner)
            self._prune_candidates()
            return songs
        finally:
            self._search_busy.discard(owner)
            if self._search_tasks.get(owner) is task:
                self._search_tasks.pop(owner, None)

    def candidate(self, owner, index, *, code=None):
        songs = self._candidate_record(owner, code=code)["songs"]
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(songs):
            raise UserFacingError("歌曲编号无效或已过期，请重新搜索。")
        return songs[index - 1]

    async def _cancel_search(self, owner):
        task = self._search_tasks.get(owner)
        active = task is not None and not task.done() and task is not asyncio.current_task()
        delivery = self._search_deliveries.get(owner)
        pending_reply = delivery is not None and not delivery["cancelled"]
        if not active and not pending_reply:
            return "你没有进行中的搜索。"
        if pending_reply:
            # Preserve late API receipts instead of interrupting a possibly submitted call.
            delivery["cancelled"] = True
            self._invalidate_search_reply(delivery["reply"])
            self._search_recall.supersede(owner)
        if active:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if pending_reply:
            return "已取消你的搜索，待播队列未改变；若结果已提交且收到可核对回执，将尝试撤回。"
        return "已取消你的搜索，待播队列未改变。"

    def _invalidate_search_reply(self, reply):
        record = self._candidates.get(reply.owner)
        if record and record.get("token") == reply.token:
            record["selectable"] = False

    def _search_reply(self, event, owner, *, page=None):
        record = self._candidate_record(owner)
        page_size = self.settings.search_results
        pages = (len(record["songs"]) + page_size - 1) // page_size
        page = record.get("page", 1) if page is None else page
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= pages:
            raise UserFacingError(f"页码无效，当前共有 {pages} 页。")
        self._search_recall.supersede(owner)
        token = uuid.uuid4().hex
        record["token"] = token
        record["page"] = page
        start = (page - 1) * page_size
        code = record["generation"][:8]
        requester = safe_text(event.get_sender_name(), 80) or safe_text(owner[1], 24)
        lines = [
            f"YouTube 搜索 · {requester}（{safe_text(owner[1], 24)}）",
            f"结果码 {code} · 第 {page}/{pages} 页 · 共 {len(record['songs'])} 首",
            self.song_list(
                record["songs"][start : start + page_size], start_index=start + 1, versions=True
            ),
            f"/点歌 编号 {code} · /点歌 详情 编号 {code}",
        ]
        if pages > 1:
            lines.append("/点歌 上一页 / 下一页 / 翻页 页码")
        return SearchReply(owner, token, "\n".join(lines))

    @staticmethod
    def _selection_arguments(value):
        parts = value.split()
        if not 1 <= len(parts) <= 2:
            raise UserFacingError("请提供歌曲编号及可选的 8 位结果码。")
        index = YouTubeMusicPlugin._index(parts[0])
        code = parts[1] if len(parts) == 2 else None
        if code is not None and not re.fullmatch(r"[a-fA-F0-9]{8}", code):
            raise UserFacingError("结果码应为搜索列表中的 8 位编号。")
        return index, code

    def _song_details(self, owner, index, code):
        song = self.candidate(owner, index, code=code)
        version = version_label(song)
        lines = [
            f"歌曲：{safe_text(song.title, 200)}",
            f"歌手：{safe_text(song.artist, 120)}",
            f"时长：{format_duration(song.duration)}",
            f"版本：{version or '未标注'}",
            song.url,
        ]
        thumbnail = song.thumbnail_url if valid_thumbnail_url(song.thumbnail_url) else ""
        return SongDetailsReply("\n".join(lines), thumbnail)

    async def _history_song(self, owner, index):
        if index is None:
            rows = await run_blocking(self.library.history, owner)
            if not rows:
                raise UserFacingError("暂无语音历史。")
            return Song(**rows[0]["song"])
        self._prune_candidates()
        view = self._history_views.get(owner)
        if not view or not 1 <= index <= len(view["songs"]):
            raise UserFacingError("历史编号无效或已过期，请先发送 /点歌 最近。")
        song = view["songs"][index - 1]
        songs = await run_blocking(self.library.songs_for_source, owner, "history", [song.video_id])
        return songs[0]

    async def _favorite_song(self, owner, index):
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise UserFacingError("收藏编号无效。")
        view = self._favorite_views.get(owner)
        if view is None:
            if self._favorite_views_limited:
                raise UserFacingError("请先查看 /点歌 收藏列表，再按展示编号操作。")
            songs = await run_blocking(self.library.favorites, owner)
            if index > len(songs):
                raise UserFacingError("收藏编号无效。")
            return songs[index - 1]
        if index > len(view):
            raise UserFacingError("收藏编号无效，请重新查看 /点歌 收藏列表。")
        self._favorite_views.move_to_end(owner)
        songs = await run_blocking(
            self.library.songs_for_source, owner, "favorites", [view[index - 1]]
        )
        return songs[0]

    async def _random_song(self, key, owner):
        favorites = await run_blocking(self.library.favorites, owner)
        history = await run_blocking(self.library.history, owner)
        queued = {row["song"]["video_id"] for row in self.state.snapshot("queues", key, default=[])}
        current = self._current.get(key)
        if current:
            queued.add(current["row"]["song"]["video_id"])
        pool = {}
        for song in [*favorites, *(Song(**row["song"]) for row in history)]:
            if (
                song.video_id not in queued
                and valid_int(song.duration, 1, self.settings.max_duration_seconds)
                and valid_song(asdict(song))
            ):
                try:
                    self.service.preflight(song, voice=self.settings.send_strategy != "link_only")
                except UserFacingError:
                    continue
                pool.setdefault(song.video_id, song)
        if not pool:
            raise UserFacingError(
                "你的收藏和历史中没有可随机点播的歌曲，已排队、当前曲目和超限歌曲会排除。"
            )
        return random.choice(list(pool.values()))

    async def _detail_result(self, event, reply):
        thumbnail = reply.thumbnail_url
        if not thumbnail or not valid_thumbnail_url(thumbnail):
            return event.plain_result(reply.text)
        try:
            async with asyncio.timeout(4):
                async with self.service._stage_limit("search_concurrency", 2):
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=4),
                        auto_decompress=False,
                        trust_env=False,
                        headers={
                            "Accept": "image/jpeg,image/png,image/webp",
                            "Accept-Encoding": "identity",
                        },
                    ) as client:
                        async with client.get(
                            thumbnail,
                            allow_redirects=False,
                            proxy=self.settings.youtube_proxy or None,
                        ) as response:
                            mime = (
                                response.headers.get("Content-Type", "")
                                .split(";", 1)[0]
                                .lower()
                                .strip()
                            )
                            encoding = response.headers.get("Content-Encoding", "").lower().strip()
                            if (
                                response.status != 200
                                or mime not in {"image/jpeg", "image/png", "image/webp"}
                                or encoding not in {"", "identity"}
                                or response.content_length is not None
                                and response.content_length > 1024 * 1024
                            ):
                                return event.plain_result(reply.text)
                            content = bytearray()
                            async for chunk in response.content.iter_chunked(16384):
                                if len(content) + len(chunk) > 1024 * 1024:
                                    return event.plain_result(reply.text)
                                content.extend(chunk)
                            image = bytes(content)
            signatures = {
                "image/jpeg": image.startswith(b"\xff\xd8\xff"),
                "image/png": image.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/webp": len(image) >= 12 and image[:4] == b"RIFF" and image[8:12] == b"WEBP",
            }
            if not signatures[mime]:
                return event.plain_result(reply.text)
            return event.chain_result([Comp.Image.fromBytes(image), Comp.Plain(reply.text)])
        except asyncio.CancelledError:
            raise
        except Exception:
            return event.plain_result(reply.text)

    def _known_session(self, key):
        session = self.state.snapshot("sessions", key) if isinstance(key, str) else None
        if not session or not self._group_allowed(session["group_id"]):
            raise UserFacingError("会话不存在或已被禁止。请先在 QQ 会话中使用点歌。")
        return session

    async def enqueue(self, key, song: Song, owner, requested_by):
        result = await self.enqueue_many(key, [song], owner, requested_by)
        return result["positions"][0]["position"]

    async def enqueue_many(self, key, songs, owner, requested_by, *, skip_existing=False):
        self._require_running()
        if (
            not isinstance(songs, (list, tuple))
            or not 1 <= len(songs) <= MAX_BATCH_SIZE
            or type(skip_existing) is not bool
        ):
            raise UserFacingError(f"每批请选择 1 至 {MAX_BATCH_SIZE} 首歌曲。")
        if (
            not isinstance(owner, tuple)
            or len(owner) != 2
            or not all(isinstance(part, str) for part in owner)
        ):
            raise UserFacingError("无法确认点歌用户。")
        prepared = []
        identities = set()
        owner_id = self.library.owner_id(owner)
        added_at = time.time()
        for song in songs:
            if not isinstance(song, Song):
                raise UserFacingError("歌曲信息无效，请重新选择。")
            if not valid_int(song.duration, 1, self.settings.max_duration_seconds):
                raise UserFacingError("歌曲超过时长限制或时长未知。")
            self.service.preflight(song, voice=self.settings.send_strategy != "link_only")
            data = asdict(song)
            if not valid_song(data):
                raise UserFacingError("歌曲信息无效，请重新选择。")
            if song.video_id in identities:
                raise UserFacingError("同一批次不能重复选择歌曲。")
            identities.add(song.video_id)
            prepared.append(
                {
                    "id": uuid.uuid4().hex,
                    "song": data,
                    "owner": owner_id,
                    "requested_by": safe_text(requested_by, 80),
                    "added_at": added_at,
                }
            )
        async with self._queue_lock:
            self._require_running()
            self._known_session(key)
            current_id = self._current.get(key, {}).get("row", {}).get("song", {}).get("video_id")

            def add(payload):
                rows = payload["queues"].get(key, [])
                existing = {row["song"]["video_id"] for row in rows}
                if current_id:
                    existing.add(current_id)
                skipped = [
                    row["song"]["video_id"]
                    for row in prepared
                    if row["song"]["video_id"] in existing
                ]
                if skipped and not skip_existing:
                    raise UserFacingError(
                        "这首歌正在播放或处理，请等待后再点播。"
                        if current_id in skipped
                        else "这首歌已在队列中。"
                    )
                additions = [row for row in prepared if row["song"]["video_id"] not in existing]
                if additions and len(rows) + len(additions) > self.settings.max_session_queue:
                    raise UserFacingError(
                        "当前会话队列已满。"
                        if len(prepared) == 1
                        else "队列剩余容量不足，整批未加入。"
                    )
                positions = [
                    {"video_id": row["song"]["video_id"], "position": len(rows) + index}
                    for index, row in enumerate(additions, 1)
                ]
                if additions:
                    rows.extend(additions)
                    payload["queues"][key] = rows
                return {
                    "added": len(additions),
                    "positions": positions,
                    "skipped": skipped,
                    "queue_size": len(rows),
                }

            result = await run_blocking(self.state.update, add)
            if result["added"]:
                self._start_worker(key)
        if result["added"]:
            self._broadcast()
        return result

    def enqueue_feedback(self, key, song, position):
        payload = self.state.snapshot()
        lines = [
            f"已加入队列 #{position}：{safe_text(song.title)} · "
            f"{safe_text(song.artist, 60)} · {format_duration(song.duration)}"
        ]
        if key in self._errors or key in payload["paused"]:
            lines.append("队列已暂停，等待管理员核对后继续。")
            return "\n".join(lines)
        rows = payload["queues"].get(key, [])
        index = next(
            (index for index, row in enumerate(rows) if row["song"]["video_id"] == song.video_id),
            max(0, position - 1),
        )
        ahead = rows[:index]
        current = self._current.get(key)
        if current and current["submitted"]:
            ahead = [row for row in ahead if row["id"] != current["row"]["id"]]
        summary = pending_queue_summary(ahead)
        count = summary["count"]
        wait = (
            2 * count
            if self.settings.send_strategy == "link_only"
            else summary["duration_seconds"] + 2 * count
        )
        if current and current["submitted"] and current["row"]["song"]["video_id"] != song.video_id:
            count += 1
            wait += max(0, current.get("wait_until", time.monotonic()) - time.monotonic())
        if count:
            lines.append(f"前方 {count} 首 · 播放等待约 {format_duration(math.ceil(wait))}")
        return "\n".join(lines)

    def _start_worker(self, key):
        if self._terminating or not self._running or key in self._workers or key in self._errors:
            return
        payload = self.state.snapshot()
        if key in payload["paused"] or not payload["queues"].get(key):
            return
        self._known_session(key)
        self._workers[key] = asyncio.create_task(
            self._queue_worker(key), name=f"youtube-queue-{key}"
        )

    def _phase(self, key, phase):
        if key in self._current:
            self._current[key]["phase"] = phase
        self._broadcast()

    def _job_path_available(self, job):
        try:
            return (
                job.parent == self.job_root
                and job.name.startswith("job-")
                and self.data_dir.resolve() == self._data_dir_real
                and self.job_root.resolve() == self._job_root_real
                and self._job_root_real.is_relative_to(self._data_dir_real)
                and job.resolve().parent == self._job_root_real
                and not any(path.is_symlink() or path.is_junction() for path in (job, *job.parents))
            )
        except (OSError, RuntimeError):
            return False

    def _create_job(self, job):
        if not self._job_path_available(job):
            raise UserFacingError("临时音频目录无效或已改变，请检查插件数据目录。")
        self.job_root.mkdir(parents=True, exist_ok=True)
        if not self._job_path_available(job):
            raise UserFacingError("临时音频目录已改变，未启动下载。")
        job.mkdir()
        mark_owned_job_dir(job)

    def _cleanup_job(self, job):
        try:
            if not self._job_path_available(job) or not is_owned_job_dir(job):
                logger.warning("[YouTubeMusic] 已拒绝清理不属于任务根目录的临时路径。")
                return
            shutil.rmtree(job)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("[YouTubeMusic] 临时音频目录清理失败：%s", type(exc).__name__)

    async def _send(self, umo, chain):
        result = await asyncio.wait_for(
            self.context.send_message(umo, chain), timeout=self.settings.send_timeout_seconds
        )
        if result is False:
            raise UserFacingError("AstrBot 未匹配到目标平台，未提交消息。")

    async def _send_song(self, session, song, wav):
        async with self.service._stage_limit("send_concurrency", self.settings.send_concurrency):
            if self.settings.send_strategy == "link_only":
                try:
                    await self._send(
                        session["umo"],
                        MessageChain().message(f"{safe_text(song.title)}\n{song.url}"),
                    )
                except (TimeoutError, NetworkError, ConnectionError):
                    raise UserFacingError("链接提交异常，结果未知。请核对消息后再点播。") from None
                return False
            path = str(wav.resolve())
            try:
                await self._send(
                    session["umo"], MessageChain(chain=[Comp.Record(file=path, url=path)])
                )
                return True
            except (TimeoutError, NetworkError, ConnectionError):
                # A lost receipt is ambiguous: do not submit a second message.
                raise UserFacingError("语音提交异常，结果未知。请核对消息后再点播。") from None
            except Exception:
                if self.settings.send_strategy != "voice_then_link":
                    raise
                try:
                    await self._send(
                        session["umo"],
                        MessageChain().message(
                            f"语音提交失败：{safe_text(song.title)}\n{song.url}"
                        ),
                    )
                except (TimeoutError, NetworkError, ConnectionError):
                    raise UserFacingError("链接提交异常，结果未知。请核对消息后再点播。") from None
                return False

    async def _queue_worker(self, key):
        job = None
        row = None
        submitted = False
        try:
            while not self._terminating:
                async with self._queue_lock:
                    payload = self.state.snapshot()
                    rows = payload["queues"].get(key, [])
                    if key in payload["paused"] or not rows:
                        break
                    row = rows[0]
                    song = Song(**row["song"])
                    session = self._known_session(key)
                    self._current[key] = {"row": row, "phase": "等待处理", "submitted": False}
                    self.audio_cache.protected_video_ids = {
                        item["row"]["song"]["video_id"] for item in self._current.values()
                    }
                submitted = False
                async with self._limiter.slot(
                    wait_timeout=self.settings.queue_wait_timeout_seconds,
                    on_state=lambda phase: self._phase(key, phase),
                ):
                    async with self.dependency_gate.use(
                        timeout=self.settings.queue_wait_timeout_seconds
                    ):
                        wav = None
                        media = {}
                        if self.settings.send_strategy != "link_only":
                            job = self.job_root / f"job-{uuid.uuid4().hex}"
                            await run_blocking(self._create_job, job)
                            wav, song, media = await self.service.download_and_convert(
                                song, job, on_stage=lambda phase: self._phase(key, phase)
                            )
                        self._current[key]["row"] = {**row, "song": asdict(song)}
                        self._phase(key, "提交消息")
                        audio = await self._send_song(session, song, wav)
                        submitted = True
                        self._current[key]["submitted"] = True
                        duration = media.get("duration_seconds", song.duration)
                        if (
                            isinstance(duration, bool)
                            or not isinstance(duration, (int, float))
                            or not math.isfinite(duration)
                            or duration <= 0
                        ):
                            duration = song.duration or 0
                        self._current[key]["wait_until"] = time.monotonic() + (
                            duration + 2 if audio else 2
                        )

                        def finish(data, entry_id=row["id"]):
                            data["queues"][key] = [
                                item
                                for item in data["queues"].get(key, [])
                                if item["id"] != entry_id
                            ]

                        def finish_submission(
                            finalize_queue=finish, record=row, resolved=song, record_audio=audio
                        ):
                            self.state.update(finalize_queue)
                            if record_audio:
                                self.library.add_history_for_owner_id(record["owner"], resolved)

                        # Parent cancellation must not hide a failed persistence write.
                        finalization = asyncio.create_task(run_blocking(finish_submission))
                        cancelled = False
                        while True:
                            try:
                                await asyncio.shield(finalization)
                                break
                            except asyncio.CancelledError:
                                cancelled = True
                                if finalization.done():
                                    finalization.result()
                                    break
                        if cancelled:
                            raise asyncio.CancelledError
                        self._errors.pop(key, None)
                        self._error_entry_ids.pop(key, None)
                if job:
                    await run_blocking(self._cleanup_job, job)
                    job = None
                self._phase(key, "已提交语音" if audio else "已提交链接")
                await asyncio.sleep(max(0, self._current[key]["wait_until"] - time.monotonic()))
                self._current.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = (
                "消息已提交，状态保存失败。请核对队列后继续。"
                if submitted
                else safe_text(str(exc), 200)
                if isinstance(exc, UserFacingError)
                else "处理失败，请查看插件日志。"
            )
            self._errors[key] = message or "处理失败，请检查依赖并重试。"
            if row is not None:
                self._error_entry_ids[key] = row["id"]
            logger.error("[YouTubeMusic] 队列任务失败：%s", type(exc).__name__)
            try:
                await run_blocking(
                    self.state.update,
                    lambda data: data["paused"].append(key) if key not in data["paused"] else None,
                )
            except Exception as save_error:
                logger.error("[YouTubeMusic] 暂停状态保存失败：%s", type(save_error).__name__)
        finally:
            try:
                if job:
                    await run_blocking(self._cleanup_job, job)
            finally:
                self._current.pop(key, None)
                self._workers.pop(key, None)
                self.audio_cache.protected_video_ids = {
                    item["row"]["song"]["video_id"] for item in self._current.values()
                }
                self._broadcast()
                if key not in self._errors and not self._terminating:
                    self._start_worker(key)

    async def queue_action(
        self, key, action, *, owner=None, admin=False, entry_id="", direction=""
    ):
        self._require_running()
        self._known_session(key)
        if action not in {
            "resume",
            "pause",
            "skip",
            "clear",
            "remove",
            "move",
            "cancel_mine",
            "forget",
        }:
            raise UserFacingError("不支持的队列操作。")
        if action not in {"remove", "cancel_mine"} and not admin:
            raise UserFacingError("该操作需要队列管理员权限。")
        owner_id = self.library.owner_id(owner) if owner else ""
        async with self._queue_lock:
            payload = self.state.snapshot()
            rows = payload["queues"].get(key, [])
            active = self._current.get(key)
            target = next((row for row in rows if row["id"] == entry_id), None)
            if action in {"remove", "move"}:
                if not target:
                    raise UserFacingError("歌曲已离开队列，请刷新后重试。")
                if active and not active["submitted"] and active["row"]["id"] == entry_id:
                    raise UserFacingError("歌曲正在处理，不能移除或重排。")
                if not admin and target["owner"] != owner_id:
                    raise UserFacingError("只能移除自己加入的待播歌曲。")
                if action == "move" and direction not in {"up", "down"}:
                    raise UserFacingError("无效的顺序操作。")
            if action == "forget" and (rows or active):
                raise UserFacingError("请先清空队列，再移除会话。")
            should_cancel = (
                action in {"pause", "skip", "clear"}
                or action == "cancel_mine"
                and active
                and not active["submitted"]
                and active["row"]["owner"] == owner_id
            )
            was_paused = key in payload["paused"]
            skip_id = (
                active["row"]["id"]
                if active and not active["submitted"]
                else rows[0]["id"]
                if rows and not active
                else ""
            )
            if should_cancel:
                await run_blocking(
                    self.state.update,
                    lambda data: data["paused"].append(key) if key not in data["paused"] else None,
                )
                worker = self._workers.get(key)
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
            failed_entry_id = self._error_entry_ids.get(key)

            def mutate(data):
                queue = data["queues"].get(key, [])
                if action == "clear":
                    data["queues"][key] = []
                elif action in {"skip", "remove"}:
                    selected = skip_id if action == "skip" else entry_id
                    data["queues"][key] = [row for row in queue if row["id"] != selected]
                elif action == "cancel_mine":
                    data["queues"][key] = [row for row in queue if row["owner"] != owner_id]
                elif action == "move":
                    index = next(i for i, row in enumerate(queue) if row["id"] == entry_id)
                    other = index + (-1 if direction == "up" else 1)
                    if 0 <= other < len(queue):
                        if (
                            active
                            and not active["submitted"]
                            and queue[other]["id"] == active["row"]["id"]
                        ):
                            raise UserFacingError("不能移动到正在处理的歌曲之前。")
                        queue[index], queue[other] = queue[other], queue[index]
                elif action == "forget":
                    data["sessions"].pop(key)
                    data["queues"].pop(key, None)
                resolves_error = action in {"resume", "clear", "forget"} or (
                    action in {"skip", "remove", "cancel_mine"}
                    and bool(failed_entry_id)
                    and any(row["id"] == failed_entry_id for row in queue)
                    and not any(row["id"] == failed_entry_id for row in data["queues"].get(key, []))
                )
                # Reordering must not turn a skip of another song into an implicit retry.
                unresolved_error = key in self._errors and not resolves_error
                resume = (
                    action in {"resume", "skip", "forget"}
                    or action == "cancel_mine"
                    and not was_paused
                ) and not unresolved_error
                if resume:
                    data["paused"] = [item for item in data["paused"] if item != key]
                return resolves_error

            resolves_error = await run_blocking(self.state.update, mutate)
            if resolves_error:
                self._errors.pop(key, None)
                self._error_entry_ids.pop(key, None)
            if action not in {"pause", "clear", "forget"}:
                self._start_worker(key)
        self._broadcast()

    def queue_text(self, key):
        payload = self.state.snapshot()
        rows = payload["queues"].get(key, [])
        status = (
            "已暂停" if key in payload["paused"] else "运行中" if key in self._workers else "待启动"
        )
        summary = pending_queue_summary(rows)
        lines = [
            f"点歌队列 · {status}",
            f"待播 {summary['count']} 首 · 总时长 {format_duration(summary['duration_seconds'])}",
        ]
        if summary["unknown_duration_count"]:
            lines[-1] += f"（{summary['unknown_duration_count']} 首时长未知）"
        current = self._current.get(key)
        if current:
            lines.append(
                f"{current['phase']}：{safe_text(current['row']['song']['title'])} · "
                f"点歌：{safe_text(current['row'].get('requested_by') or '未知点歌者', 40)}"
            )
        lines.extend(
            f"{index}. {safe_text(row['song']['title'])} · {safe_text(row['song']['artist'], 60)} · "
            f"{format_duration(row['song']['duration'])} · 点歌：{safe_text(row.get('requested_by') or '未知点歌者', 40)}"
            for index, row in enumerate(rows, 1)
        )
        if not rows:
            lines.append("暂无待播歌曲。")
        if key in self._errors:
            lines.append(self._errors[key])
        return "\n".join(lines)

    def personal_queue_text(self, key, owner):
        payload = self.state.snapshot()
        rows = payload["queues"].get(key, [])
        owner_id = self.library.owner_id(owner)
        current = self._current.get(key)
        own_current = current if current and current["row"]["owner"] == owner_id else None
        current_id = own_current["row"]["id"] if own_current else None
        mine = [
            (index, row)
            for index, row in enumerate(rows, 1)
            if row["owner"] == owner_id and row["id"] != current_id
        ]
        status = (
            "已暂停"
            if key in payload["paused"] or key in self._errors
            else "运行中"
            if key in self._workers
            else "待启动"
        )
        summary = pending_queue_summary([row for _, row in mine])
        lines = [
            f"我的点歌队列 · QQ {safe_text(owner[1], 24)} · {status}",
            f"本人待播 {summary['count']} 首 · 总时长 {format_duration(summary['duration_seconds'])}",
        ]
        if summary["unknown_duration_count"]:
            lines[-1] += f"（{summary['unknown_duration_count']} 首时长未知）"
        if own_current:
            row = own_current["row"]
            song = row["song"]
            position = next(
                (index for index, entry in enumerate(rows, 1) if entry["id"] == current_id), None
            )
            label = f"当前 #{position}" if position is not None else "当前"
            lines.append(
                f"{label} · {safe_text(own_current['phase'], 60)}：{safe_text(song['title'])} · "
                f"{safe_text(song['artist'], 60)} · {format_duration(song['duration'])}"
            )
        lines.extend(
            f"{index}. {safe_text(row['song']['title'])} · {safe_text(row['song']['artist'], 60)} · "
            f"{format_duration(row['song']['duration'])}"
            for index, row in mine
        )
        if not mine:
            lines.append("你没有待播歌曲。")
        if key in self._errors:
            lines.append("群队列处理异常，等待管理员核对后继续。")
        return "\n".join(lines)

    async def dispatch_music(self, event, argument):
        async with self.operation():
            key, owner, admin = await self._event_identity(event)
            if argument in {"", "帮助"}:
                return HELP_TEXT
            if argument in {"队列", "状态"}:
                return self.queue_text(key)
            if argument == "我的队列":
                return self.personal_queue_text(key, owner)
            if argument == "取消搜索":
                return await self._cancel_search(owner)
            if argument in {"下一页", "上一页"} or argument.startswith("翻页 "):
                record = self._candidate_record(owner)
                page = (
                    self._index(argument[3:])
                    if argument.startswith("翻页 ")
                    else record.get("page", 1) + (1 if argument == "下一页" else -1)
                )
                return self._search_reply(event, owner, page=page)
            if argument in {"重搜", "重新搜索"} or argument.startswith(("重搜 ", "重新搜索 ")):
                alias = "重新搜索" if argument.startswith("重新搜索") else "重搜"
                query = argument[len(alias) :].strip()
                if not query:
                    self._prune_candidates()
                    query = self._candidates.get(owner, {}).get("query", "")
                if not query:
                    raise UserFacingError("没有可重搜的关键词，请发送 /点歌 重搜 关键词。")
                songs = await self.search(owner, query, paged=True)
                return self._search_reply(event, owner) if songs else "未找到符合时长限制的歌曲。"
            if argument.startswith("详情 "):
                index, code = self._selection_arguments(argument[3:])
                return self._song_details(owner, index, code)
            controls = {
                "继续": "resume",
                "暂停": "pause",
                "下一首": "skip",
                "清空队列": "clear",
                "取消": "cancel_mine",
            }
            if argument in controls:
                await self.queue_action(key, controls[argument], owner=owner, admin=admin)
                return self.queue_text(key)
            if argument.startswith("移除 "):
                index = self._index(argument[3:])
                rows = self.state.snapshot()["queues"].get(key, [])
                if not 1 <= index <= len(rows):
                    raise UserFacingError("队列编号无效。")
                await self.queue_action(
                    key, "remove", owner=owner, admin=admin, entry_id=rows[index - 1]["id"]
                )
                return "已移除待播歌曲。"
            if argument == "收藏列表":
                songs = await run_blocking(self.library.favorites, owner)
                self._favorite_views[owner] = tuple(song.video_id for song in songs)
                self._favorite_views.move_to_end(owner)
                while len(self._favorite_views) > 500:
                    self._favorite_views.popitem(last=False)
                    # After any eviction, absence cannot distinguish an unseen list from a stale one.
                    self._favorite_views_limited = True
                return self.song_list(songs) or "暂无收藏。"
            if argument == "最近":
                rows = await run_blocking(self.library.history, owner)
                songs = [Song(**row["song"]) for row in rows[:10]]
                self._history_views[owner] = {"at": time.monotonic(), "songs": songs}
                self._history_views.move_to_end(owner)
                self._prune_candidates()
                return self.song_list(songs) or "暂无语音提交记录。"
            if argument == "清除历史":
                await run_blocking(self.library.clear_history, owner)
                self._history_views.pop(owner, None)
                return "已清除历史。"
            if argument.startswith("收藏 "):
                index, code = self._selection_arguments(argument[3:])
                song = self.candidate(owner, index, code=code)
                token = self._candidates[owner]["token"]
                added, _ = await run_blocking(self.library.add_favorite, owner, song)
                self._search_recall.selected(owner, token)
                return "已收藏。" if added else "已在收藏中。"
            selected_token = None
            if argument.startswith(("播放收藏 ", "取消收藏 ")):
                index = self._index(argument[5:])
                song = await self._favorite_song(owner, index)
                if argument.startswith("取消"):
                    removed = await run_blocking(self.library.remove_favorite, owner, song.video_id)
                    if not removed:
                        raise UserFacingError("歌曲已不在收藏中，请重新查看 /点歌 收藏列表。")
                    return "已移除收藏。"
            elif argument == "重播" or argument.startswith("重播 "):
                index = self._index(argument[3:]) if argument.startswith("重播 ") else None
                song = await self._history_song(owner, index)
            elif argument == "随机":
                song = await self._random_song(key, owner)
            elif argument.isdecimal() or re.fullmatch(r"\d{1,3}\s+[a-fA-F0-9]{8}", argument):
                index, code = self._selection_arguments(argument)
                song = self.candidate(owner, index, code=code)
                selected_token = self._candidates[owner]["token"]
            elif parse_youtube_url(argument):
                song = (await self.search(owner, argument))[0]
            else:
                query = argument[3:].strip() if argument.startswith("歌手 ") else argument
                songs = await self.search(owner, query, paged=True)
                if not songs:
                    return "未找到符合时长限制的歌曲。"
                return self._search_reply(event, owner)
            position = await self.enqueue(key, song, owner, event.get_sender_name())
            if selected_token:
                self._search_recall.selected(owner, selected_token)
            return self.enqueue_feedback(key, song, position)

    @staticmethod
    def _index(value):
        if not re.fullmatch(r"\d{1,3}", value.strip()):
            raise UserFacingError("请提供有效编号。")
        return int(value)

    @staticmethod
    def song_list(songs, *, start_index=1, versions=False):
        return "\n".join(
            f"{index}. {safe_text(song.title)} · {safe_text(song.artist)} · "
            f"{format_duration(song.duration)}"
            + (f" · {version_label(song)}" if versions and version_label(song) else "")
            for index, song in enumerate(songs, start_index)
        )

    @filter.command("点歌")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def song_command(self, event: AstrMessageEvent):
        search_reply = None
        delivery = None
        try:
            result = await self.dispatch_music(event, extract_command_argument(event.message_str))
            if isinstance(result, SearchReply):
                search_reply = result
                if self.settings.search_result_auto_recall:
                    delivery = {"reply": result, "cancelled": False}
                    self._search_deliveries[result.owner] = delivery
                    async with self.operation():
                        sent = await self._search_recall.send(event, result)
                    if delivery["cancelled"]:
                        return
                    if sent is False:
                        self._invalidate_search_reply(result)
                    return
                result = result.text
            elif isinstance(result, SongDetailsReply):
                async with self.operation():
                    detail = await self._detail_result(event, result)
                yield detail
                return
        except asyncio.CancelledError:
            if search_reply:
                self._invalidate_search_reply(search_reply)
            raise
        except Exception as exc:
            if search_reply:
                self._invalidate_search_reply(search_reply)
            if delivery and delivery["cancelled"]:
                return
            logger.error("[YouTubeMusic] 点歌请求失败：%s", type(exc).__name__)
            result = (
                safe_text(str(exc), 200)
                if isinstance(exc, UserFacingError)
                else "请求未完成，请查看插件日志。"
            )
        finally:
            if delivery and self._search_deliveries.get(search_reply.owner) is delivery:
                self._search_deliveries.pop(search_reply.owner, None)
        yield event.plain_result(result)

    async def ai_music_action(self, event, action, query="", index=0):
        async with self.operation():
            key, owner, admin = await self._event_identity(event)
            if action == "search":
                songs = await self.search(owner, query)
                return self.song_list(songs) or "未找到符合限制的歌曲。"
            if action == "enqueue":
                if not index and not parse_youtube_url(query):
                    raise UserFacingError("请先搜索并由用户确认候选编号，不自动猜测歌曲。")
                song = (
                    self.candidate(owner, index) if index else (await self.search(owner, query))[0]
                )
                token = self._candidates[owner]["token"]
                position = await self.enqueue(key, song, owner, event.get_sender_name())
                if index:
                    self._search_recall.selected(owner, token)
                return self.enqueue_feedback(key, song, position)
            if action == "queue_status":
                return self.queue_text(key)
            controls = {
                "resume": "resume",
                "pause": "pause",
                "next": "skip",
                "cancel": "cancel_mine",
            }
            if action not in controls:
                raise UserFacingError("不支持的音乐操作。")
            await self.queue_action(key, controls[action], owner=owner, admin=admin)
            return self.queue_text(key)
