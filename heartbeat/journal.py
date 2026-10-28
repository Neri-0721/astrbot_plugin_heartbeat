"""Bounded, atomic per-session decision records; never store raw model reasoning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


class DecisionJournal:
    def __init__(self, data_dir: Path, limit: int = 128):
        self.root = data_dir / "decisions"
        self.limit = limit

    def session_path(self, umo: str) -> Path:
        return self.root / hashlib.sha256(umo.encode()).hexdigest()

    async def save(self, umo: str, record: dict) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", record["run_id"]):
            raise ValueError("Invalid decision ID")
        path = self.session_path(umo) / f"{record['run_id']}.json"
        text = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
        writer = asyncio.create_task(asyncio.to_thread(self._write, path, text))
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            await writer
            raise
        return path

    def _write(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as stream:
                name = stream.name
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            name = None
        finally:
            if name:
                Path(name).unlink(missing_ok=True)
        others = sorted(
            (p for p in path.parent.glob("*.json") if p != path),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        for old in others[max(0, self.limit - 1) :]:
            old.unlink(missing_ok=True)
