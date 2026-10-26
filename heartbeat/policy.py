"""Deterministic wake/contact policy; action selection stays with the Agent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .state import HeartbeatState, SessionState

LIMITS = {
    "interval_minutes": (10, 1440),
    "interaction_threshold_minutes": (5, 1440),
    "max_daily_heartbeats": (1, 288),
    "cooldown_seconds": (10, 3600),
    "contact_cooldown_minutes": (1, 10080),
    "max_daily_messages": (1, 48),
    "unanswered_backoff_hours": (1, 168),
    "agent_timeout_seconds": (10, 1800),
}


def clock_minutes(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid HH:MM time: {value}")
    hour, minute = map(int, parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid HH:MM time: {value}")
    return hour * 60 + minute


@dataclass(frozen=True)
class Settings:
    enabled_sessions: tuple[str, ...] = ()
    interval_minutes: int = 90
    startup_behavior: str = "auto"
    target_mode: str = "all"
    active_hours_start: str = "08:00"
    active_hours_end: str = "23:30"
    background_outside_active_hours: bool = False
    check_last_interaction: bool = True
    interaction_threshold_minutes: int = 30
    max_daily_heartbeats: int = 48
    cooldown_seconds: int = 120
    contact_cooldown_minutes: int = 240
    max_daily_messages: int = 3
    unanswered_backoff_hours: int = 24
    agent_timeout_seconds: int = 300
    allow_group_sessions: bool = False
    timezone: str = ""

    @classmethod
    def from_config(cls, config: dict, fallback_timezone: str = "") -> Settings:
        defaults = cls()
        values = {}
        for name in cls.__dataclass_fields__:
            value = config.get(name, getattr(defaults, name))
            default = getattr(defaults, name)
            if name == "enabled_sessions":
                if not isinstance(value, (list, tuple)):
                    raise ValueError("enabled_sessions must be a list of complete session IDs")
                sessions = []
                for umo in value:
                    parts = umo.strip().split(":", 2) if isinstance(umo, str) else []
                    if (
                        len(parts) != 3
                        or not all(parts)
                        or parts[1] not in {"FriendMessage", "GroupMessage"}
                        or any(c in umo for c in "*?\r\n")
                    ):
                        raise ValueError(
                            "enabled_sessions requires exact platform:type:session IDs"
                        )
                    sessions.append(umo.strip())
                value = tuple(dict.fromkeys(sessions))
            elif isinstance(default, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be a boolean")
            elif isinstance(default, int):
                if isinstance(value, bool):
                    raise ValueError(f"{name} must be an integer")
                value = int(value)
                if value < 1:
                    raise ValueError(f"{name} must be positive")
            else:
                value = str(value).strip()
            values[name] = value
        for name, (minimum, maximum) in LIMITS.items():
            if not minimum <= values[name] <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if values["startup_behavior"] not in {"auto", "on_interaction"}:
            raise ValueError("startup_behavior must be auto or on_interaction")
        if values["target_mode"] not in {"all", "last"}:
            raise ValueError("target_mode must be all or last")
        clock_minutes(values["active_hours_start"])
        clock_minutes(values["active_hours_end"])
        values["timezone"] = values["timezone"] or fallback_timezone
        if values["timezone"]:
            ZoneInfo(values["timezone"])
        return cls(**values)

    def local_time(self, now: float) -> datetime:
        tz = ZoneInfo(self.timezone) if self.timezone else None
        current = datetime.fromtimestamp(now, tz=tz)
        return current if tz else current.astimezone()

    def day(self, now: float) -> str:
        # Do not convert a configured zone back to the host's local timezone.
        tz = ZoneInfo(self.timezone) if self.timezone else None
        return datetime.fromtimestamp(now, tz=tz).date().isoformat()

    def in_hours(self, now: float) -> bool:
        tz = ZoneInfo(self.timezone) if self.timezone else None
        current = datetime.fromtimestamp(now, tz=tz)
        minute = current.hour * 60 + current.minute
        start = clock_minutes(self.active_hours_start)
        end = clock_minutes(self.active_hours_end)
        if start == end:
            return True
        return start <= minute < end if start < end else minute >= start or minute < end


def candidate_sessions(state: HeartbeatState, settings: Settings) -> list[str]:
    enabled = {
        umo
        for umo, session in state.sessions.items()
        if umo in settings.enabled_sessions and session.enabled
    }
    if settings.target_mode == "last":
        return [state.last_umo] if state.last_umo in enabled else []
    return sorted(
        enabled,
        key=lambda umo: (state.sessions[umo].last_wake_at, umo),
    )


def wake_block(
    state: HeartbeatState, umo: str, settings: Settings, now: float, *, manual=False
) -> str:
    if umo not in settings.enabled_sessions:
        return "session_disabled"
    session = state.sessions[umo]
    if state.paused or not session.enabled:
        return "paused"
    if ":GroupMessage:" in umo and not settings.allow_group_sessions:
        return "group_sessions_disabled"
    if not settings.background_outside_active_hours and not settings.in_hours(now):
        return "outside_background_hours"
    if state.wake_day == settings.day(now) and state.wake_count >= settings.max_daily_heartbeats:
        return "wake_budget"
    if state.last_dispatch_at and now - state.last_dispatch_at < settings.cooldown_seconds:
        return "wake_cooldown"
    if not manual and session.next_due_at and now < session.next_due_at:
        return "not_due"
    return ""


def message_digest(messages: list[dict]) -> str:
    data = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def contact_block(
    session: SessionState,
    settings: Settings,
    now: float,
    *,
    started_user_at: float,
    topic: str = "",
    digest: str = "",
) -> str:
    if not session.enabled:
        return "session_paused"
    if session.last_user_at > started_user_at:
        return "new_user_message"
    if now < session.quiet_until:
        return "user_quiet_period"
    if not settings.in_hours(now):
        return "outside_contact_hours"
    if (
        settings.check_last_interaction
        and now - session.last_user_at < settings.interaction_threshold_minutes * 60
    ):
        return "recent_user_interaction"
    day = settings.day(now)
    if sum(c["day"] == day for c in session.contacts) >= settings.max_daily_messages:
        return "message_budget"
    if session.last_contact_at:
        wait = settings.contact_cooldown_minutes * 60
        if session.unanswered:
            wait = max(
                wait,
                settings.unanswered_backoff_hours * 3600 * 2 ** min(session.unanswered - 1, 3),
            )
        if now - session.last_contact_at < wait:
            return "unanswered_backoff" if session.unanswered else "contact_cooldown"
    for contact in session.contacts:
        if contact["day"] == day and (
            (topic and contact["topic"] == topic) or (digest and contact["digest"] == digest)
        ):
            return "already_contacted_today"
    return ""
