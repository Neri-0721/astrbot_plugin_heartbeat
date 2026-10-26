"""Small, versioned state store. All mutable state belongs to the event loop."""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class SessionState:
    sender_id: str = ""
    platform_type: str = ""
    enabled: bool = True
    quiet_until: float = 0
    last_user_at: float = 0
    last_wake_at: float = 0
    next_due_at: float = 0
    last_contact_at: float = 0
    unanswered: int = 0
    last_outcome: str = ""
    last_summary: str = ""
    contacts: list[dict] = field(default_factory=list)
    notes: dict[str, dict] = field(default_factory=dict)

    def observe_user(self, sender_id: str, now: float, platform_type: str = ""):
        self.sender_id = sender_id
        self.platform_type = platform_type or self.platform_type
        self.last_user_at = now
        self.unanswered = 0

    def record_contact(self, now: float, day: str, topic: str, digest: str, reason: str):
        self.last_contact_at = now
        self.unanswered += 1
        self.contacts.append(
            {"at": now, "day": day, "topic": topic, "digest": digest, "reason": reason}
        )
        self.contacts = self.contacts[-128:]


@dataclass
class HeartbeatState:
    version: int = 1
    paused: bool = False
    last_umo: str = ""
    last_dispatch_at: float = 0
    wake_day: str = ""
    wake_count: int = 0
    sessions: dict[str, SessionState] = field(default_factory=dict)

    def roll_day(self, day: str):
        if day != self.wake_day:
            self.wake_day, self.wake_count = day, 0

    def begin(self, umo: str, now: float, interval: int, day: str):
        self.roll_day(day)
        self.wake_count += 1
        self.last_dispatch_at = now
        session = self.sessions[umo]
        session.last_wake_at = now
        session.next_due_at = now + interval * 60
        session.last_outcome = "running"


class StateStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "state.json"
        self.legacy_path = data_dir / "last_session.json"
        self._save_lock = asyncio.Lock()

    def load(self) -> HeartbeatState:
        # Invalid state must not silently reset budgets or destroy the original file.
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("Heartbeat state must be an object")
            if data.get("version") != 1:
                raise ValueError("Unsupported heartbeat state version")
            allowed = {f.name for f in fields(HeartbeatState)} - {"sessions"}
            state = HeartbeatState(**{k: v for k, v in data.items() if k in allowed})
            session_fields = {f.name for f in fields(SessionState)}
            for umo, value in data.get("sessions", {}).items():
                state.sessions[umo] = SessionState(
                    **{k: v for k, v in value.items() if k in session_fields}
                )
            for session in state.sessions.values():
                if session.last_outcome == "running":
                    session.last_outcome = "interrupted_by_restart"
            self._validate(state)
            return state
        state = HeartbeatState()
        if self.legacy_path.exists():
            data = json.loads(self.legacy_path.read_text(encoding="utf-8-sig"))
            umo = data.get("umo", "")
            if umo:
                state.last_umo = umo
                state.sessions[umo] = SessionState(
                    sender_id=str(data.get("sender_id", "")),
                    last_user_at=float(data.get("saved_at", 0)),
                )
        self._validate(state)
        return state

    @staticmethod
    def _validate(state):
        def number(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0

        if (
            type(state.paused) is not bool
            or type(state.wake_count) is not int
            or state.wake_count < 0
        ):
            raise ValueError("Invalid heartbeat pause/budget state")
        if (
            not number(state.last_dispatch_at)
            or not isinstance(state.wake_day, str)
            or not isinstance(state.last_umo, str)
        ):
            raise ValueError("Invalid heartbeat scheduling state")
        for umo, session in state.sessions.items():
            if (
                not isinstance(umo, str)
                or type(session.enabled) is not bool
                or type(session.unanswered) is not int
                or session.unanswered < 0
            ):
                raise ValueError("Invalid heartbeat session state")
            for name in (
                "quiet_until",
                "last_user_at",
                "last_wake_at",
                "next_due_at",
                "last_contact_at",
            ):
                if not number(getattr(session, name)):
                    raise ValueError(f"Invalid session timestamp: {name}")
            if not isinstance(session.contacts, list) or not isinstance(session.notes, dict):
                raise ValueError("Invalid heartbeat contact/task records")
            for contact in session.contacts:
                if (
                    not isinstance(contact, dict)
                    or not number(contact.get("at"))
                    or not all(
                        isinstance(contact.get(k), str)
                        for k in ("day", "topic", "digest", "reason")
                    )
                ):
                    raise ValueError("Invalid successful contact record")

    async def save(self, state: HeartbeatState):
        async with self._save_lock:
            text = json.dumps(asdict(state), ensure_ascii=False, indent=2)
            writer = asyncio.create_task(asyncio.to_thread(self._atomic_write, text))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                # Do not release the lock while an older writer can still replace
                # a newer snapshot on disk after this caller was cancelled.
                await writer
                raise

    def _atomic_write(self, text: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                name = stream.name
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)
