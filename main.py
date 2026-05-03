"""
astrbot_plugin_heartbeat — 多会话 + per-session HEARTBEAT.md + last/all 模式

特色：
  ◈ 多会话管理：记录所有平台会话（QQ/微信等）
  ◈ "last" 模式：自动定位到最后聊天的会话（同 OpenClaw）
  ◈ "all" 模式：遍历所有会话，符合条件的唤醒
  ◈ 每会话独立 HEARTBEAT.md：QQ 和微信可以有不同的规则
  ◈ 预检零 token：条件不满足直接跳过，不调 LLM
  ◈ 通过后委托框架唤醒完整 Agent（build_main_agent）
"""

import os
import time
import asyncio
import datetime
from typing import Optional
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import StarTools, Context, Star, register
from astrbot.api import logger

_PLUGIN_NAME = "astrbot_plugin_heartbeat"
_VERSION = "2.4.0"
_CRON_NAME = "heartbeat_preflight"

# ── 默认 HEARTBEAT.md ────────────────────────────────────

_DEFAULT_HB = r"""# HEARTBEAT.md

你是被定时任务唤醒的，不是用户主动找你。

## 规则
- [12:00-13:00] 提醒吃午饭
- [18:00-19:00] 问候晚饭
- [22:00之后] 提醒休息
- 距上次对话 > 4h → 自然问候
- 距上次对话 < 30 分钟 → 不要打扰

## 约束
- 不要解释"被定时唤醒"
- 问候不超过 25 字
- 没事做 → 什么都不做，结束
"""

# QQ 专属示例
_DEFAULT_HB_QQ = r"""# HEARTBEAT.md (QQ)

你是被定时任务唤醒的，不是用户主动找你。

## 规则
- [12:00-13:00] "吃饭时间到~"
- [22:00之后] "该睡了"
- 距上次对话 > 4h → 问候

请使用 QQ 聊天风格。
"""

# 微信专属示例
_DEFAULT_HB_WECHAT = r"""# HEARTBEAT.md (微信)

你是被定时任务唤醒的，不是用户主动找你。

## 规则
- [18:00-19:00] "晚饭吃了吗"
- 距上次对话 > 8h → 问候（微信不那么频繁）

请使用微信聊天风格，可以带个表情。
"""


def _cron_expr(m: int) -> str:
    """分钟数 → 合法 cron 表达式

    APScheduler 分钟字段范围 0-59。
    整小时用小时级 cron，非整小时找最大可整除值+冷却期兜底。
    """
    if m < 60:
        return f"*/{m} * * * *"
    h = m // 60
    if m % 60 == 0:
        return f"0 */{h} * * *"
    # 非整小时：找 m 和 60 的最大公约数，保底 30
    f = 60
    while f > 1 and m % f != 0:
        f -= 1
    return f"*/{max(f, 30)} * * * *"


def _in_hours(start: str, end: str) -> bool:
    now = datetime.datetime.now()

    def p(t):
        h, *m = t.strip().split(":")
        return int(h) * 60 + (int(m[0]) if m else 0)

    nm = now.hour * 60 + now.minute
    sm, em = p(start), p(end)
    return sm <= nm < em if em > sm else (nm >= sm or nm < em)


def _extract_platform(umo: str) -> str:
    """从 umo 中提取平台名：qq_official:1:123 → qq_official"""
    return umo.split(":")[0] if ":" in umo else umo


NOTE_TPL = """Heartbeat check — preflight passed.

Platform: {platform}
Current time: {time}
Hours since last user interaction: {hours_idle:.1f}

Your HEARTBEAT.md:
{hb}"""


@register(
    _PLUGIN_NAME,
    "Key",
    "心跳助手：多会话 / per-session HEARTBEAT.md / last/all 模式",
    _VERSION,
)
class HeartbeatPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(_PLUGIN_NAME)
        self._init_data_dir()

        # 会话存储: {umo: {"sender_id":str, "last_interaction":float, "platform":str}}
        self._sessions: dict[str, dict] = {}
        self._last_umo: Optional[str] = None  # 最后聊天的 session
        self._cron_id: Optional[str] = None

        # 全局控制
        self._running = False
        self._last_run: float = 0
        self._count_today = 0
        self._today = datetime.date.today()

        # 配置
        self.interval = max(10, int(self.config.get("interval_minutes", 90)))
        self.cron = _cron_expr(self.interval)
        self.active_start = str(self.config.get("active_hours_start", "08:00"))
        self.active_end = str(self.config.get("active_hours_end", "23:30"))
        self.check_interact = bool(self.config.get("check_last_interaction", True))
        self.interact_threshold = (
            int(self.config.get("interaction_threshold_minutes", 30)) * 60
        )
        self.max_daily = int(self.config.get("max_daily_heartbeats", 48))
        self.cooldown = int(self.config.get("cooldown_seconds", 120))
        self.target_mode = str(self.config.get("target_mode", "all")).strip().lower()
        self.startup_behavior = str(self.config.get("startup_behavior", "on_interaction")).strip().lower()
        # startup_behavior: "on_interaction" (等待首次消息) | "auto" (立即启动)

        self._hb_override = str(self.config.get("heartbeat_file_path", "")).strip()

        # auto 模式：尝试从持久化文件恢复上次会话并自动注册
        if self.startup_behavior == "auto":
            restored = self._restore_last_session()
            if restored:
                logger.info(f"[heartbeat] auto mode: session restored {restored[:40]}")
                asyncio.create_task(self._delayed_schedule())

        logger.info(
            f"[heartbeat] v{_VERSION} | {self.interval}min | "
            f"mode={self.target_mode} | startup={self.startup_behavior} | "
            f"{self.active_start}-{self.active_end}"
        )

    # ── 数据目录初始化 ────────────────────────────────────

    def _init_data_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)
        # 默认全局 HEARTBEAT.md
        self._ensure_hb(self.data_dir / "HEARTBEAT.md", _DEFAULT_HB)
        # per-platform 目录（空目录，后续由 _ensure_per_platform_hb 动态创建）
        os.makedirs(self.data_dir / "heartbeat.d", exist_ok=True)

    @staticmethod
    def _ensure_hb(path, content):
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    # ── HEARTBEAT.md 解析（三层查找） ─────────────────────

    def _hb_for_umo(self, umo: str) -> str:
        """按优先级查找 HEARTBEAT.md：
        1. 用户自定义路径 (config heartbeat_file_path)
        2. per-platform: data_dir/heartbeat.d/{platform}.md
        3. 默认: data_dir/HEARTBEAT.md
        """
        if self._hb_override:
            path = self._hb_override
        else:
            platform = _extract_platform(umo)
            per_platform = self.data_dir / "heartbeat.d" / f"{platform}.md"
            if per_platform.exists():
                path = str(per_platform)
            else:
                path = str(self.data_dir / "HEARTBEAT.md")  # 本次先 fallback

        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return _DEFAULT_HB

    def _hb_path_for_umo(self, umo: str) -> str:
        if self._hb_override:
            return self._hb_override
        platform = _extract_platform(umo)
        pp = self.data_dir / "heartbeat.d" / f"{platform}.md"
        return str(pp if pp.exists() else self.data_dir / "HEARTBEAT.md")

    async def _ensure_per_platform_hb(self, platform: str):
        """如 per-platform HEARTBEAT.md 不存在则自动创建"""
        pp = self.data_dir / "heartbeat.d" / f"{platform}.md"
        if pp.exists():
            return
        content = (
            f"# HEARTBEAT.md ({platform})\n\n"
            f"你是被定时任务唤醒的，不是用户主动找你。\n\n"
            f"## 规则\n"
            f"- [12:00-13:00] 提醒吃午饭\n"
            f"- [18:00-19:00] 问候晚饭\n"
            f"- [22:00之后] 提醒休息\n"
            f"- 距上次对话 > 4h → 一句自然问候\n"
            f"- 距上次对话 < 30min → 不要打扰\n\n"
            f"## 约束\n"
            f"- 不要解释'被定时唤醒'\n"
            f"- 不超过 25 字\n"
            f"- 没事做就安静结束\n"
        )
        try:
            os.makedirs(self.data_dir / "heartbeat.d", exist_ok=True)
            with open(pp, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"[heartbeat] auto-created per-platform HEARTBEAT.md: {pp}")
        except Exception as e:
            logger.warning(f"[heartbeat] failed to create per-platform HEARTBEAT.md: {e}")

    # ── 调度 ──────────────────────────────────────────────

    async def _schedule(self):
        cm = self.context.cron_manager
        if not cm:
            return
        if self._cron_id:
            try:
                await cm.delete_job(self._cron_id)
            except Exception:
                pass
        try:
            job = await cm.add_basic_job(
                name=_CRON_NAME,
                cron_expression=self.cron,
                handler=self._preflight,
                description=f"heartbeat | {self.target_mode} | {self.interval}min",
            )
            self._cron_id = job.job_id
            logger.info(f"[heartbeat] cron registered id={job.job_id} expr={self.cron}")
        except Exception as e:
            logger.error(f"[heartbeat] register fail: {e}")

    async def _unschedule(self):
        if not self._cron_id:
            return
        cm = self.context.cron_manager
        if cm:
            try:
                await cm.delete_job(self._cron_id)
            except Exception:
                pass
        self._cron_id = None

    # ── 预检（零 token） ──────────────────────────────────

    def _get_candidate_sessions(self, now: float) -> list[tuple[str, dict]]:
        """根据 target_mode 获取候选 session 列表"""
        if not self._sessions:
            return []

        if self.target_mode == "last":
            # "last" 模式：只检查最后聊天的 session
            if self._last_umo and self._last_umo in self._sessions:
                return [(self._last_umo, self._sessions[self._last_umo])]
            return []

        # "all" 模式：遍历所有 session
        return list(self._sessions.items())

    async def _preflight(self):
        now = time.time()

        if self._running:
            return
        if not _in_hours(self.active_start, self.active_end):
            return
        if now - self._last_run < self.cooldown:
            return

        today = datetime.date.today()
        if today != self._today:
            self._count_today = 0
            self._today = today
        if self._count_today >= self.max_daily:
            return

        candidates = self._get_candidate_sessions(now)
        if not candidates:
            return

        self._running = True
        triggered = 0
        try:
            for umo, sess in candidates:
                if triggered >= 1:
                    break
                if (
                    self.check_interact
                    and (now - sess["last_interaction"]) < self.interact_threshold
                ):
                    continue
                self._count_today += 1
                triggered += 1
                platform = sess.get("platform", _extract_platform(umo))
                logger.info(
                    f"[heartbeat] ▲ #{self._count_today} | {platform} | idle={(now - sess['last_interaction']) / 60:.0f}min"
                )
                try:
                    await self._delegate_wake(umo, sess, now - sess["last_interaction"])
                except Exception as e:
                    logger.error(f"[heartbeat] wake fail {umo}: {e}")
        finally:
            self._running = False
            if triggered:
                self._last_run = time.time()

    async def _delegate_wake(self, umo: str, sess: dict, idle: float):
        cm = self.context.cron_manager
        if not cm:
            return
        platform = sess.get("platform", _extract_platform(umo))
        # 确保 per-platform HEARTBEAT.md 存在（被删除则自动重建，await 保证写入后才读取）
        if not self._hb_override:
            await self._ensure_per_platform_hb(platform)
        hb = self._hb_for_umo(umo)
        note = NOTE_TPL.format(
            platform=platform,
            time=datetime.datetime.now().strftime("%H:%M"),
            hours_idle=idle / 3600,
            hb=hb,
        )
        job = await cm.add_active_job(
            name=f"hb_{platform[:12]}",
            cron_expression=None,
            run_once=True,
            run_at=datetime.datetime.now(datetime.timezone.utc),
            payload={
                "session": umo,
                "sender_id": sess.get("sender_id", "heartbeat"),
                "note": note,
                "origin": "plugin",
            },
            description=f"heartbeat {platform}",
        )
        logger.info(f"[heartbeat] → agent job={job.job_id}")

    # ── 启停行为 ──────────────────────────────────────────

    def _last_session_path(self):
        return str(self.data_dir / "last_session.json")

    def _save_last_session(self, umo: str, sender_id: str):
        """持久化当前会话，供 auto 模式重启后恢复"""
        import json
        try:
            with open(self._last_session_path(), "w", encoding="utf-8") as f:
                json.dump({"umo": umo, "sender_id": sender_id, "saved_at": time.time()}, f)
        except Exception as e:
            logger.warning(f"[heartbeat] save session failed: {e}")

    def _restore_last_session(self) -> Optional[str]:
        """从持久化文件恢复上次会话"""
        import json
        path = self._last_session_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            umo = data.get("umo", "")
            sid = data.get("sender_id", "")
            if umo:
                self._umo = umo
                self._sender_id = sid
                self._ready = True
                # 恢复会话到内存
                platform = _extract_platform(umo)
                self._sessions[umo] = {
                    "sender_id": sid,
                    "last_interaction": time.time(),
                    "platform": platform,
                }
                self._last_umo = umo
                return umo
        except Exception as e:
            logger.warning(f"[heartbeat] restore session failed: {e}")
        return None

    async def terminate(self):
        await self._unschedule()

    # ── 事件 ──────────────────────────────────────────────

    @filter.on_llm_request()
    async def _on_interact(self, event: AstrMessageEvent, req: ProviderRequest):
        umo = event.unified_msg_origin
        sid = event.get_sender_id()
        platform = _extract_platform(umo)

        if umo not in self._sessions:
            self._sessions[umo] = {}
            logger.info(f"[heartbeat] +session {platform} | {umo[:40]}")
            # 新会话：自动创建 per-platform HEARTBEAT.md（如不存在）
            asyncio.create_task(self._ensure_per_platform_hb(platform))

        self._sessions[umo].update(
            {
                "sender_id": sid,
                "last_interaction": time.time(),
                "platform": platform,
            }
        )
        self._last_umo = umo

        # 持久化会话（供 auto 模式恢复）
        self._save_last_session(umo, sid)

        if not self._cron_id:
            asyncio.create_task(self._delayed_schedule())

    async def _delayed_schedule(self):
        await asyncio.sleep(5)
        await self._schedule()

    # ── 命令 ──────────────────────────────────────────────

    @filter.command("heartbeat_status")
    async def cmd_status(self, event: AstrMessageEvent):
        now = time.time()
        lines = [
            "❤️ 心跳 v2.4 (多会话 + per-session HB)",
            f"  模式: {self.target_mode} | cron={self._cron_id or '未注册'}",
            f"  间隔: {self.interval}min | 活跃: {self.active_start}-{self.active_end}",
            f"  今日唤醒: {self._count_today}/{self.max_daily}",
            f"  会话: {len(self._sessions)} 个",
            f"  最后聊天: {self._last_umo[:40] if self._last_umo else 'N/A'}",
        ]
        for umo, sess in sorted(self._sessions.items()):
            idle = int(now - sess.get("last_interaction", now))
            plat = sess.get("platform", "?")
            hb = self._hb_path_for_umo(umo)
            lines.append(f"    [{plat}] 空闲{idle // 3600}h{idle % 3600 // 60}m | {hb}")
        if self._cron_id:
            try:
                jobs = await self.context.cron_manager.list_jobs("basic")
                for j in jobs:
                    if j.job_id == self._cron_id and j.next_run_time:
                        lines.append(
                            f"  下次: {j.next_run_time.astimezone().strftime('%m-%d %H:%M')}"
                        )
                        break
            except Exception:
                pass
        yield event.plain_result("\n".join(lines))

    @filter.command("heartbeat_reschedule")
    async def cmd_reschedule(self, event: AstrMessageEvent):
        self.interval = max(10, int(self.config.get("interval_minutes", 90)))
        self.cron = _cron_expr(self.interval)
        await self._schedule()
        yield event.plain_result(
            f"✅ rescheduled | {self.cron} | sessions={len(self._sessions)}"
        )

    @filter.command("heartbeat_unschedule")
    async def cmd_unschedule(self, event: AstrMessageEvent):
        await self._unschedule()
        yield event.plain_result("✅ cancelled")

    @filter.command("heartbeat_test")
    async def cmd_test(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        sid = event.get_sender_id()
        if not umo:
            yield event.plain_result("❌ no session")
            return
        platform = _extract_platform(umo)
        hb = self._hb_for_umo(umo)
        note = f"[Manual test] {platform}\n\n{hb}"
        c = self.context.cron_manager
        if c:
            job = await c.add_active_job(
                name="hb_manual",
                cron_expression=None,
                run_once=True,
                run_at=datetime.datetime.now(datetime.timezone.utc),
                payload={
                    "session": umo,
                    "sender_id": sid,
                    "note": note,
                    "origin": "plugin",
                },
                description="manual test",
            )
            yield event.plain_result(f"✅ triggered job={job.job_id} [{platform}]")

    @filter.command("heartbeat_show")
    async def cmd_show(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        platform = _extract_platform(umo)
        if not self._hb_override:
            await self._ensure_per_platform_hb(platform)
        path = self._hb_path_for_umo(umo)
        try:
            with open(path, encoding="utf-8") as f:
                yield event.plain_result(
                    "HEARTBEAT.md [" + platform + "]:

" + f.read()
                )
        except Exception as e:
            yield event.plain_result("error: " + str(e))
    @filter.command("heartbeat")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "❤️ 心跳 v2.4\n\n"
            "【目标模式】\n"
            "  all  : 遍历所有会话（QQ/微信...）\n"
            "  last : 只对最后聊天的会话生效\n\n"
            "【per-session HEARTBEAT.md】\n"
            "  data/heartbeat.d/qq_official.md\n"
            "  data/heartbeat.d/lark.md\n"
            "  data/heartbeat.d/wechat.md\n"
            "  不存在则回退到 HEARTBEAT.md\n\n"
            "【命令】\n"
            "  heartbeat_status / reschedule / unschedule\n"
            "  heartbeat_test / show / help"
        )