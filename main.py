"""Periodic opportunities for the selected character to act autonomously."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .heartbeat.agent import FullAgent
from .heartbeat.policy import Settings, candidate_sessions, contact_block, wake_block
from .heartbeat.rules import RuleBook
from .heartbeat.state import SessionState, StateStore

_PLUGIN_NAME = "astrbot_plugin_heartbeat"
_VERSION = "3.1.0"


@register(
    _PLUGIN_NAME, "Neri0721", "周期唤醒完整 Agent，自主行动、静默任务与有分寸的联系", _VERSION
)
class HeartbeatPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = Path(StarTools.get_data_dir(_PLUGIN_NAME))
        self.store = StateStore(self.data_dir)
        self.state = self.store.load()
        self.rules = RuleBook(self.data_dir, self.config)
        self.settings = self._read_settings()
        self.agent = FullAgent(context, self.state, self._refresh_settings, self._persist)
        self._run_lock = asyncio.Lock()
        self._schedule_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._cron_id = None
        self._active_task = None
        self._closed = False
        self._activated = self.settings.startup_behavior == "auto"
        self._refresh_settings(reschedule_new=False)

    def _read_settings(self):
        return Settings.from_config(self.config, self.context.get_config().get("timezone", ""))

    def _refresh_settings(self, *, reschedule_new=True):
        self.settings = self._read_settings()
        for umo, session in self.state.sessions.items():
            enabled = umo in self.settings.enabled_sessions
            if enabled and not session.enabled and reschedule_new:
                session.next_due_at = time.time() + self.settings.interval_minutes * 60
            if session.enabled and not enabled:
                self.agent.interrupt(umo)
            session.enabled = enabled
        if self.state.last_umo not in self.settings.enabled_sessions:
            eligible = [umo for umo, s in self.state.sessions.items() if s.enabled]
            self.state.last_umo = max(
                eligible, key=lambda umo: self.state.sessions[umo].last_user_at, default=""
            )
        return self.settings

    async def initialize(self):
        await asyncio.to_thread(self.rules.initialize)
        await self._persist()
        await self._ensure_schedule()
        logger.info(
            f"[heartbeat] v{_VERSION} | interval={self.settings.interval_minutes}min | mode={self.settings.target_mode}"
        )

    async def _persist(self):
        await self.store.save(self.state)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """Retry registration when the host has finished starting."""
        await self._ensure_schedule()

    async def _ensure_schedule(self):
        async with self._schedule_lock:
            if self._closed or not self._activated or self.state.paused or self._cron_id:
                return
            manager = self.context.cron_manager
            if manager is None:
                return
            # The minute tick is token-free. Persisted per-session deadlines provide
            # the actual interval, including 45, 90 and 1440 minutes, across restarts.
            job = await manager.add_basic_job(
                name="heartbeat_preflight",
                cron_expression="* * * * *",
                handler=self._preflight,
                persistent=False,
                description=f"Heartbeat v3 | {self.settings.interval_minutes}min | {self.settings.target_mode}",
            )
            self._cron_id = job.job_id

    async def _remove_schedule(self):
        async with self._schedule_lock:
            if self._cron_id and self.context.cron_manager:
                await self.context.cron_manager.delete_job(self._cron_id)
            self._cron_id = None

    @staticmethod
    def _synthetic(event):
        return (
            bool(event.get_extra("heartbeat") or event.get_extra("cron_job"))
            or event.get_platform_name() == "cron"
        )

    async def _observe(self, event, *, enroll=False):
        if self._closed or self._synthetic(event):
            return
        umo = event.unified_msg_origin
        self._refresh_settings()
        if umo not in self.settings.enabled_sessions:
            return
        if event.get_group_id() and not self.settings.allow_group_sessions:
            return
        sender = event.get_sender_id()
        if not sender or sender == event.get_self_id():
            return
        if umo not in self.state.sessions:
            if not enroll or (event.get_group_id() and not self.settings.allow_group_sessions):
                return
            self.state.sessions[umo] = SessionState(
                enabled=True, next_due_at=time.time() + self.settings.interval_minutes * 60
            )
        now = time.time()
        self.state.sessions[umo].observe_user(sender, now, event.get_platform_name())
        self.state.last_umo = umo
        self.agent.interrupt(umo)
        self._activated = True
        await self._persist()
        await self._ensure_schedule()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        """Observe real messages only in explicitly enabled sessions."""
        await self._observe(event, enroll=True)

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Fill session metadata after opt-in; ordinary chat never grants permission."""
        await self._observe(event, enroll=True)

    async def _preflight(self):
        if self._closed or self._run_lock.locked():
            return
        self._refresh_settings()
        for umo in candidate_sessions(self.state, self.settings):
            session = self.state.sessions[umo]
            if not session.sender_id or not session.platform_type:
                continue
            if not wake_block(self.state, umo, self.settings, time.time()):
                outcome = await self._run_one(umo)
                if outcome != "foreground_busy":
                    break

    def _note(self, umo, handbook, source):
        now = time.time()
        session = self.state.sessions[umo]
        settings = self.settings
        record = {
            "current_time": settings.local_time(now).isoformat(),
            "hours_since_user_message": round((now - session.last_user_at) / 3600, 2)
            if session.last_user_at
            else None,
            "last_background_outcome": session.last_outcome,
            "previous_agent_summary": session.last_summary,
            "recent_successful_contacts": session.contacts[-8:],
            "consecutive_unanswered_contacts": session.unanswered,
            "task_notes_agent_reported": session.notes,
            "contact_restriction_now": contact_block(
                session, settings, now, started_user_at=session.last_user_at
            )
            or "none; decide whether contact is worthwhile",
        }
        return (
            "Heartbeat background opportunity. Decide what to do using your persona and context.\n"
            "Task notes and the previous summary are Agent reports, not independently verified facts.\n\n"
            + json.dumps(record, ensure_ascii=False, indent=2)
            + f"\n\nHandbook source: {source}\n\n{handbook}"
        )

    async def _run_one(self, umo, *, manual=False):
        if self._closed:
            return "closed"
        if self._run_lock.locked():
            return "background_busy"
        async with self._run_lock:
            self._refresh_settings()
            blocked = wake_block(self.state, umo, self.settings, time.time(), manual=manual)
            if blocked:
                return blocked
            if not self.state.sessions[umo].sender_id or not self.state.sessions[umo].platform_type:
                return "waiting_for_interaction"
            started = False
            self._active_task = asyncio.current_task()
            session = self.state.sessions[umo]

            async def on_start():
                nonlocal started
                now = time.time()
                # Reserve the wake before the first model step. Provider/hook failures
                # before this point consume no wake allowance.
                self.state.begin(umo, now, self.settings.interval_minutes, self.settings.day(now))
                await self._persist()
                started = True

            try:
                handbook, source = await asyncio.to_thread(self.rules.read, umo)
                result = await self.agent.run(umo, self._note(umo, handbook, source), on_start)
                session.last_outcome = result.outcome
                if result.started:
                    session.last_summary = result.summary
                elif result.outcome != "foreground_busy":
                    session.next_due_at = time.time() + self.settings.interval_minutes * 60
                return result.outcome
            except asyncio.CancelledError:
                session.last_outcome = "interrupted" if started else "cancelled_before_start"
                raise
            except Exception as exc:
                session.last_outcome = "failed" if started else "preparation_failed"
                session.last_summary = f"{type(exc).__name__}: {exc}"[:1000]
                session.next_due_at = time.time() + self.settings.interval_minutes * 60
                logger.error(f"[heartbeat] {session.last_outcome}: {exc}", exc_info=True)
                return session.last_outcome
            finally:
                self._active_task = None
                await asyncio.shield(self._persist())

    async def terminate(self):
        self._closed = True
        await self._remove_schedule()
        for umo in list(self.agent.runners):
            self.agent.interrupt(umo)
        task = self._active_task
        if task and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await self._persist()

    async def _current(self, event):
        if (
            self._synthetic(event)
            or not event.get_sender_id()
            or (event.get_sender_id() == event.get_self_id())
        ):
            return None
        self._refresh_settings()
        if event.get_group_id() and (
            not self.settings.allow_group_sessions or event.role != "admin"
        ):
            return None
        await self._observe(event, enroll=True)
        session = self.state.sessions.setdefault(event.unified_msg_origin, SessionState())
        session.observe_user(event.get_sender_id(), time.time(), event.get_platform_name())
        return session

    async def _set_enabled(self, event, enabled):
        if await self._current(event) is None:
            return "当前会话不可控制；群聊需在配置中允许，并由 AstrBot 管理员操作。后台事件不能修改开启名单。"
        umo = event.unified_msg_origin
        async with self._config_lock:
            previous = list(self._read_settings().enabled_sessions)
            updated = [item for item in previous if item != umo]
            if enabled:
                updated.append(umo)
            self.config["enabled_sessions"] = updated
            try:
                # Use the same config object/file as WebUI. No second permission store.
                self.config.save_config()
            except Exception as exc:
                if enabled:
                    self.config["enabled_sessions"] = previous
                self._refresh_settings()
                await self._persist()
                logger.error(f"[heartbeat] cannot save session configuration: {exc}")
                return (
                    "开启配置保存失败，未新增开启权限；请检查配置文件。"
                    if enabled
                    else "本次运行已关闭当前会话，但配置保存失败；重启前请在 WebUI 删除此会话并保存。"
                )
            self._refresh_settings()
            if enabled:
                self.state.sessions[umo].next_due_at = (
                    time.time() + self.settings.interval_minutes * 60
                )
                self.state.last_umo = umo
                self._activated = True
            await self._persist()
        await self._ensure_schedule()
        return (
            "已开启当前会话并保存配置；下一轮按配置间隔运行。全局暂停需管理员另行恢复。"
            if enabled
            else "已关闭当前会话并保存配置；后续不再唤醒，已开始的工具操作可能需要返回后才能停止。"
        )

    @filter.command("heartbeat_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前会话的后台活动、联系限制和规则来源。"""
        self._refresh_settings()
        umo = event.unified_msg_origin
        enabled = umo in self.settings.enabled_sessions
        session = self.state.sessions.get(event.unified_msg_origin)
        if session is None:
            yield event.plain_result(
                f"会话 ID：{umo}\n"
                + (
                    "已在开启名单中，等待真实消息取得会话信息。"
                    if enabled
                    else "当前会话未开启；普通聊天不会自动开启。使用 /heartbeat_enable 或在配置 enabled_sessions 中添加此 ID。"
                )
                + "群聊还需在配置中允许。"
            )
            return
        now = time.time()
        try:
            _, source = await asyncio.to_thread(self.rules.read, event.unified_msg_origin)
        except Exception as exc:
            source = str(exc)
        count = self.state.wake_count if self.state.wake_day == self.settings.day(now) else 0
        blocked = contact_block(session, self.settings, now, started_user_at=session.last_user_at)
        background_block = wake_block(self.state, umo, self.settings, now)
        if enabled and (not session.sender_id or not session.platform_type):
            background_block = "waiting_for_interaction"
        host_block = (await self.agent.access(umo))[0] if enabled else "session_disabled"
        yield event.plain_result(
            f"Heartbeat v{_VERSION}\n"
            f"会话 ID：{umo}\n"
            f"当前会话：{'已开启' if enabled else '未开启'}；全局：{'暂停' if self.state.paused else '运行'}；模式 {self.settings.target_mode}\n"
            f"后台条件：{background_block or '已到期'}\n"
            f"宿主条件：{host_block or '允许'}\n"
            f"间隔：{self.settings.interval_minutes} 分钟；下次检查约 {max(0, int(session.next_due_at - now)) // 60} 分钟后\n"
            f"今日 Agent 轮次：{count}/{self.settings.max_daily_heartbeats}；配置开启会话 {len(self.settings.enabled_sessions)}\n"
            f"联系限制：{blocked or '可由 Agent 判断'}；连续未回应 {session.unanswered} 次\n"
            f"上轮：{session.last_outcome or '尚未运行'}\n规则：{source}\n"
            f"会话规则路径：{self.rules.session_path(event.unified_msg_origin)}"
        )

    @filter.command("heartbeat_reschedule")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_reschedule(self, event: AstrMessageEvent):
        """读取全部配置、恢复全局调度并重新计算下一轮时间（管理员）。"""
        if self._synthetic(event):
            yield event.plain_result("后台事件不能修改全局调度。")
            return
        self._refresh_settings()
        self.state.paused = False
        self._activated = True
        for session in self.state.sessions.values():
            session.next_due_at = (
                session.last_wake_at or time.time()
            ) + self.settings.interval_minutes * 60
        await self._persist()
        await self._remove_schedule()
        await self._ensure_schedule()
        yield event.plain_result(f"已更新全部配置；间隔 {self.settings.interval_minutes} 分钟。")

    @filter.command("heartbeat_unschedule")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_unschedule(self, event: AstrMessageEvent):
        """暂停全局后台调度；普通聊天不会重新开启（管理员）。"""
        if self._synthetic(event):
            yield event.plain_result("后台事件不能修改全局调度。")
            return
        self.state.paused = True
        for umo in list(self.agent.runners):
            self.agent.interrupt(umo)
        await self._persist()
        await self._remove_schedule()
        yield event.plain_result("已暂停全局心跳；使用 /heartbeat_reschedule 恢复。")

    @filter.command("heartbeat_disable", alias={"heartbeat_pause"})
    async def cmd_pause(self, event: AstrMessageEvent):
        """关闭当前会话并从配置开启名单移除；兼容 /heartbeat_pause。"""
        yield event.plain_result(await self._set_enabled(event, False))

    @filter.command("heartbeat_enable", alias={"heartbeat_resume"})
    async def cmd_resume(self, event: AstrMessageEvent):
        """明确开启当前会话并保存配置；兼容 /heartbeat_resume。"""
        yield event.plain_result(await self._set_enabled(event, True))

    async def _set_quiet(self, event, minutes):
        if self._synthetic(event):
            return "请仅在用户明确要求时设置免打扰；后台任务不能替用户修改此设置。"
        if isinstance(minutes, bool) or not 0 <= minutes <= 10080:
            return "分钟数需在 0—10080 之间；0 表示解除。"
        session = await self._current(event)
        if session is None:
            return "当前会话不可控制；群聊需先在配置中允许，并由 AstrBot 管理员操作。"
        session.quiet_until = time.time() + minutes * 60 if minutes else 0
        await self._persist()
        return (
            f"已设置 {minutes} 分钟内不主动联系，后台仍可静默做事。"
            if minutes
            else "已解除主动联系免打扰；其他联系限制仍生效。"
        )

    @filter.command("heartbeat_quiet")
    async def cmd_quiet(self, event: AstrMessageEvent, minutes: int = 120):
        """暂停当前会话的主动联系，默认 120 分钟；0 解除。"""
        yield event.plain_result(await self._set_quiet(event, minutes))

    @filter.llm_tool(name="heartbeat_set_quiet")
    async def set_quiet(self, event: AstrMessageEvent, minutes: int) -> str:
        """Only when the user explicitly requests it, pause proactive contact while keeping silent background work.

        Args:
            minutes(number): Quiet duration in minutes, 0 to lift; maximum 10080.
        """
        return await self._set_quiet(event, minutes)

    @filter.command("heartbeat_test")
    async def cmd_test(self, event: AstrMessageEvent):
        """在当前会话立即尝试完整 Agent；仍遵守暂停、预算与联系限制。"""
        if await self._current(event) is None:
            yield event.plain_result(
                "当前会话不可测试；群聊需先在配置中允许，并由 AstrBot 管理员操作。"
            )
            return
        if event.unified_msg_origin not in self.settings.enabled_sessions:
            yield event.plain_result(
                "当前会话未开启；请先使用 /heartbeat_enable。手动测试不会自动开启。"
            )
            return
        yield event.plain_result("正在尝试一次后台活动；近期聊天可能使它只做静默任务。")
        outcome = await self._run_one(event.unified_msg_origin, manual=True)
        yield event.plain_result(f"后台活动结果：{outcome}")

    @filter.command("heartbeat_show")
    async def cmd_show(self, event: AstrMessageEvent):
        """显示当前会话实际生效的手册及来源，不创建平台副本。"""
        try:
            text, source = await asyncio.to_thread(self.rules.read, event.unified_msg_origin)
            yield event.plain_result(f"规则来源：{source}\n\n{text}")
        except Exception as exc:
            yield event.plain_result(f"读取规则失败：{exc}")

    @filter.command("heartbeat")
    async def cmd_heartbeat(self, event: AstrMessageEvent):
        """查看后台活动和主动联系的控制命令。"""
        yield event.plain_result(
            f"Heartbeat v{_VERSION}\n"
            "/heartbeat_status — 当前会话状态\n"
            "/heartbeat_show — 实际生效的规则\n"
            "/heartbeat_test — 尝试一次完整 Agent\n"
            "/heartbeat_enable / heartbeat_disable — 开启/关闭当前会话并保存配置\n"
            "/heartbeat_resume / heartbeat_pause — 上述命令的兼容别名\n"
            "/heartbeat_quiet [分钟] — 只暂停主动联系，0 解除\n"
            "/heartbeat_reschedule / heartbeat_unschedule — 全局恢复/暂停（管理员）"
        )
