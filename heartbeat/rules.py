"""Explicit rule inheritance without creating stale per-platform copies."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

DEFAULT_RULES = """# HEARTBEAT.md

这是一次后台自主活动机会，不是用户的新消息。保持当前人格，结合对话与记忆决定做什么。

## 可以做的事
- 检查已有待办、继续约定的跟进，或整理值得下次聊天使用的信息。
- 根据最近话题和实际信息，决定是否值得分享、关心或轻量问候。
- 没有合适行动时可以安静结束；不需要每次都联系用户。

## 联系用户
- 先查看本轮联系限制；不能联系时仍可静默完成已有任务。
- 长时间没聊天只是一次问候的参考，不能据此断言用户生病、难过或遇到危险。
- 上次主动联系没得到回应时应退让；不要重复催促或升级措辞。
- 同一提醒完成后，用 heartbeat_remember 记下结果，避免下一轮重复。
- 发消息使用 send_message_to_user，并提供具体 reason 和稳定 topic。

## 连续性
- 用 heartbeat_remember 保存待办或简短进度，完成后将 status 设为 done。
- 结束时简述实际行动和结果；这份摘要用于下次后台活动，不会自动发给用户。
"""


class RuleBook:
    def __init__(self, data_dir: Path, config: dict):
        self.data_dir = data_dir
        self.config = config

    def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "HEARTBEAT.md"
        if not path.exists():
            path.write_text(DEFAULT_RULES, encoding="utf-8")

    def session_path(self, umo: str) -> Path:
        key = hashlib.sha256(umo.encode()).hexdigest()
        return self.data_dir / "heartbeat.sessions" / f"{key}.md"

    def read(self, umo: str) -> tuple[str, str]:
        override = str(self.config.get("heartbeat_file_path", "")).strip()
        if override:
            return self._read(Path(override)), override
        path = self.session_path(umo)
        if path.exists():
            return self._read(path), str(path)
        platform = umo.split(":", 1)[0]
        if re.fullmatch(r"[\w.-]+", platform):
            path = self.data_dir / "heartbeat.d" / f"{platform}.md"
            if path.exists():
                text = self._read(path)
                # Old auto-generated snapshots are inherited, not user overrides.
                # Preserve edited snapshots and all original files on disk.
                if not self._is_inherited_copy(text):
                    return text, str(path)
        webui = str(self.config.get("heartbeat_content", "")).strip()
        if webui:
            return webui, "WebUI heartbeat_content (global default)"
        path = self.data_dir / "HEARTBEAT.md"
        return self._read(path), str(path)

    def _is_inherited_copy(self, text: str) -> bool:
        prefix = "> 自动生成，编辑全局 HEARTBEAT.md 或直接修改此文件。"
        if not text.startswith(prefix):
            return False
        body = text[len(prefix) :].strip()
        global_path = self.data_dir / "HEARTBEAT.md"
        if global_path.exists() and body == self._read(global_path).strip():
            return True
        legacy_default = """# HEARTBEAT.md

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
- 没事做 → 什么都不做，结束"""
        return body == legacy_default

    @staticmethod
    def _read(path: Path) -> str:
        if path.stat().st_size > 65536:
            raise ValueError(f"Heartbeat rule file exceeds 64 KiB: {path}")
        return path.read_text(encoding="utf-8-sig")
