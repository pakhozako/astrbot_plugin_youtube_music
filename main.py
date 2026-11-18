"""QQ music requests share one bounded queue, permission policy and lifecycle."""

from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.ai_tools import YouTubeMusicTool
from .core.audio_cache import AudioCache
from .core.dependency_manager import DependencyManager
from .core.library import UserLibrary
from .core.maintenance import run_blocking
from .core.queue import MAX_BATCH_SIZE, JobLimiter, pending_queue_summary
from .core.search_recall import SearchRecall, SearchReply
from .core.service import UserFacingError, YouTubeMusicService
from .core.settings import Settings
from .core.state import QueueState, session_key
from .core.store_validation import valid_int, valid_song
from .core.utils import (
    Song,
    cleanup_stale_owned_job_dirs,
    extract_command_argument,
    format_duration,
    mark_owned_job_dir,
    parse_youtube_url,
    safe_text,
)
from .core.webui import WebUIMixin

PLUGIN_NAME = "astrbot_plugin_youtube_music"
HELP_TEXT = """YouTube 点歌
/点歌 <关键词>：搜索
/点歌 <编号或视频链接>：加入当前会话队列
/点歌 队列 / 状态：查看队列
/点歌 收藏 <编号> / 收藏列表 / 播放收藏 <编号>
/点歌 取消收藏 <编号> / 最近 / 清除历史
/点歌 取消：取消自己的待播歌曲
/点歌 移除 <队列编号>：移除自己的待播歌曲
队列管理员：/点歌 继续 / 暂停 / 下一首 / 清空队列
仅支持单个 YouTube 视频。"""


class YouTubeMusicPlugin(WebUIMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.settings = Settings.from_config(config, data_dir=self.data_dir)
        self.job_root = self.data_dir / "cache" / "jobs"
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
        self._running = False
        self._terminating = False
        self._search_recall = SearchRecall(
            delay=self.settings.search_result_recall_delay_seconds,
            timeout=min(10, self.settings.send_timeout_seconds),
            semaphore=self.service._stage_limit("send_concurrency", self.settings.send_concurrency),
            is_current=lambda owner, token: self._candidates.get(owner, {}).get("token") == token,
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
        owned = set(self._workers.values()) | self._requests | self.dependency_gate.task_snapshot()
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
        for owner in list(self._candidates):
            if now - self._candidates[owner]["at"] >= ttl:
                self._candidates.pop(owner)
        while len(self._candidates) > 500:
            self._candidates.popitem(last=False)

    async def search(self, owner: tuple[str, str], query: str) -> list[Song]:
        self._require_running()
        self._prune_candidates()
        previous = self._candidates.get(owner)
        if (
            owner in self._search_busy
            or previous
            and time.monotonic() - previous["at"] < self.settings.request_cooldown_seconds
        ):
            raise UserFacingError("搜索过于频繁，请稍后重试。")
        self._search_busy.add(owner)
        token = uuid.uuid4().hex
        self._search_recall.supersede(owner)
        self._candidates[owner] = {"at": time.monotonic(), "songs": [], "token": token}
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
                    else await self.service.search(query)
                )
            self._candidates[owner] = {"at": time.monotonic(), "songs": songs, "token": token}
            self._candidates.move_to_end(owner)
            self._prune_candidates()
            return songs
        finally:
            self._search_busy.discard(owner)

    def candidate(self, owner, index):
        self._prune_candidates()
        songs = self._candidates.get(owner, {}).get("songs", [])
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(songs):
            raise UserFacingError("歌曲编号无效或已过期，请重新搜索。")
        return songs[index - 1]

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

            def add(payload):
                rows = payload["queues"].get(key, [])
                existing = {row["song"]["video_id"] for row in rows}
                skipped = [
                    row["song"]["video_id"]
                    for row in prepared
                    if row["song"]["video_id"] in existing
                ]
                if skipped and not skip_existing:
                    raise UserFacingError("这首歌已在队列中。")
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

    def _create_job(self, job):
        self.job_root.mkdir(parents=True, exist_ok=True)
        job.mkdir()
        mark_owned_job_dir(job)

    async def _send(self, umo, chain):
        result = await asyncio.wait_for(
            self.context.send_message(umo, chain), timeout=self.settings.send_timeout_seconds
        )
        if result is False:
            raise UserFacingError("AstrBot 未匹配到目标平台，未提交消息。")

    async def _send_song(self, session, song, wav):
        async with self.service._stage_limit("send_concurrency", self.settings.send_concurrency):
            if self.settings.send_strategy == "link_only":
                await self._send(
                    session["umo"], MessageChain().message(f"{safe_text(song.title)}\n{song.url}")
                )
                return False
            path = str(wav.resolve())
            try:
                await self._send(
                    session["umo"], MessageChain(chain=[Comp.Record(file=path, url=path)])
                )
                return True
            except TimeoutError:
                # A timeout is ambiguous: do not send a second message automatically.
                raise UserFacingError("语音提交超时，结果未知。请核对消息后再点播。") from None
            except Exception:
                if self.settings.send_strategy != "voice_then_link":
                    raise
                await self._send(
                    session["umo"],
                    MessageChain().message(f"语音提交失败：{safe_text(song.title)}\n{song.url}"),
                )
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
                        if self.settings.send_strategy != "link_only":
                            job = self.job_root / f"job-{uuid.uuid4().hex}"
                            await run_blocking(self._create_job, job)
                            wav, song, _ = await self.service.download_and_convert(
                                song, job, on_stage=lambda phase: self._phase(key, phase)
                            )
                        self._phase(key, "提交消息")
                        audio = await self._send_song(session, song, wav)
                        submitted = True
                        self._current[key]["submitted"] = True

                        def finish(data, entry_id=row["id"]):
                            data["queues"][key] = [
                                item
                                for item in data["queues"].get(key, [])
                                if item["id"] != entry_id
                            ]

                        await run_blocking(self.state.update, finish)
                        if audio:
                            await run_blocking(
                                self.library.add_history_for_owner_id, row["owner"], song
                            )
                        self._errors.pop(key, None)
                        self._error_entry_ids.pop(key, None)
                if job:
                    await run_blocking(shutil.rmtree, job)
                    job = None
                self._phase(key, "已提交语音" if audio else "已提交链接")
                await asyncio.sleep((song.duration or 0) + 2 if audio else 2)
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
            if job:
                await run_blocking(shutil.rmtree, job, ignore_errors=True)
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
            lines.append(f"{current['phase']}：{safe_text(current['row']['song']['title'])}")
        lines.extend(
            f"{index}. {safe_text(row['song']['title'])} · {format_duration(row['song']['duration'])}"
            for index, row in enumerate(rows, 1)
        )
        if not rows:
            lines.append("暂无待播歌曲。")
        if key in self._errors:
            lines.append(self._errors[key])
        return "\n".join(lines)

    async def dispatch_music(self, event, argument):
        async with self.operation():
            key, owner, admin = await self._event_identity(event)
            if argument in {"", "帮助"}:
                return HELP_TEXT
            if argument in {"队列", "状态"}:
                return self.queue_text(key)
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
                return self.song_list(songs) or "暂无收藏。"
            if argument == "最近":
                rows = await run_blocking(self.library.history, owner)
                return (
                    self.song_list([Song(**row["song"]) for row in rows[:10]])
                    or "暂无语音提交记录。"
                )
            if argument == "清除历史":
                await run_blocking(self.library.clear_history, owner)
                return "已清除历史。"
            if argument.startswith("收藏 "):
                song = self.candidate(owner, self._index(argument[3:]))
                token = self._candidates[owner]["token"]
                added, _ = await run_blocking(self.library.add_favorite, owner, song)
                self._search_recall.selected(owner, token)
                return "已收藏。" if added else "已在收藏中。"
            if argument.startswith(("播放收藏 ", "取消收藏 ")):
                songs = await run_blocking(self.library.favorites, owner)
                index = self._index(argument[5:])
                if not 1 <= index <= len(songs):
                    raise UserFacingError("收藏编号无效。")
                song = songs[index - 1]
                if argument.startswith("取消"):
                    await run_blocking(self.library.remove_favorite, owner, song.video_id)
                    return "已移除收藏。"
            elif argument.isdecimal():
                song = self.candidate(owner, self._index(argument))
                token = self._candidates[owner]["token"]
            elif parse_youtube_url(argument):
                song = (await self.search(owner, argument))[0]
            else:
                songs = await self.search(owner, argument)
                if not songs:
                    return "未找到符合时长限制的歌曲。"
                token = self._candidates[owner]["token"]
                return SearchReply(
                    owner, token, f"YouTube 搜索结果 · {token}\n{self.song_list(songs)}"
                )
            position = await self.enqueue(key, song, owner, event.get_sender_name())
            if argument.isdecimal():
                self._search_recall.selected(owner, token)
            return f"已加入队列 #{position}：{safe_text(song.title)}"

    @staticmethod
    def _index(value):
        if not re.fullmatch(r"\d{1,3}", value.strip()):
            raise UserFacingError("请提供有效编号。")
        return int(value)

    @staticmethod
    def song_list(songs):
        return "\n".join(
            f"{index}. {safe_text(song.title)} · {safe_text(song.artist)} · {format_duration(song.duration)}"
            for index, song in enumerate(songs, 1)
        )

    @filter.command("点歌")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def song_command(self, event: AstrMessageEvent):
        try:
            result = await self.dispatch_music(event, extract_command_argument(event.message_str))
            if isinstance(result, SearchReply):
                if self.settings.search_result_auto_recall:
                    async with self.operation():
                        await self._search_recall.send(event, result)
                    return
                result = result.text
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[YouTubeMusic] 点歌请求失败：%s", type(exc).__name__)
            result = (
                safe_text(str(exc), 200)
                if isinstance(exc, UserFacingError)
                else "请求未完成，请查看插件日志。"
            )
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
                return f"已加入队列 #{position}：{safe_text(song.title)}"
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
