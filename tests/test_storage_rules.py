import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from heartbeat.rules import DEFAULT_RULES, RuleBook
from heartbeat.state import HeartbeatState, SessionState, StateStore


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_restores_all_sessions_quotas_pause_and_actual_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            state = HeartbeatState(paused=True, wake_day="2026-10-26", wake_count=12)
            state.sessions = {
                "a": SessionState(last_user_at=123, next_due_at=456),
                "b": SessionState(last_user_at=789),
            }
            state.sessions["a"].record_contact(100, "2026-10-26", "lunch", "hash", "reason")
            await store.save(state)
            restored = store.load()
            self.assertEqual(restored.wake_count, 12)
            self.assertTrue(restored.paused)
            self.assertEqual(restored.sessions["a"].last_user_at, 123)
            self.assertEqual(restored.sessions["a"].next_due_at, 456)
            self.assertEqual(restored.sessions["a"].unanswered, 1)
            self.assertEqual(len(restored.sessions), 2)

    async def test_migrates_legacy_timestamp_without_rewriting_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            text = json.dumps({"umo": "qq:FriendMessage:1", "sender_id": "1", "saved_at": 123})
            store.legacy_path.write_text(text)
            state = store.load()
            await store.save(state)
            self.assertEqual(state.sessions[state.last_umo].last_user_at, 123)
            self.assertEqual(store.legacy_path.read_text(), text)

    async def test_concurrent_flushes_leave_complete_latest_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            state = HeartbeatState()
            await asyncio.gather(*(store.save(state) for _ in range(8)))
            self.assertEqual(store.load().version, 1)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["state.json"])

    async def test_corrupt_state_is_not_replaced_or_budget_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            store.path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                store.load()
            self.assertEqual(store.path.read_text(), "broken")

    async def test_invalid_budget_type_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            text = json.dumps({"version": 1, "wake_count": "zero"})
            store.path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                store.load()
            self.assertEqual(store.path.read_text(), text)

    async def test_cancelled_flush_cannot_overwrite_newer_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            state = HeartbeatState(wake_count=1)
            entered, release = threading.Event(), threading.Event()
            write = store._atomic_write

            def delayed(text):
                if json.loads(text)["wake_count"] == 1:
                    entered.set()
                    release.wait(timeout=5)
                write(text)

            store._atomic_write = delayed
            first = asyncio.create_task(store.save(state))
            await asyncio.to_thread(entered.wait, 5)
            first.cancel()
            state.wake_count = 2
            second = asyncio.create_task(store.save(state))
            try:
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(second), timeout=0.02)
            finally:
                release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            self.assertEqual(store.load().wake_count, 2)

    async def test_running_turn_is_marked_interrupted_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp))
            state = HeartbeatState(sessions={"a": SessionState(last_outcome="running")})
            await store.save(state)
            self.assertEqual(store.load().sessions["a"].last_outcome, "interrupted_by_restart")


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {}
        self.book = RuleBook(self.root, self.config)
        self.book.initialize()
        self.umo = "qq:FriendMessage:1"

    def test_fresh_install_reads_shared_default_without_creating_copies(self):
        self.assertEqual(self.book.read(self.umo)[0], DEFAULT_RULES)
        self.assertFalse((self.root / "heartbeat.d").exists())

    def test_priority_custom_session_platform_webui_global(self):
        self.config["heartbeat_content"] = "webui"
        self.assertEqual(self.book.read(self.umo)[0], "webui")
        platform = self.root / "heartbeat.d" / "qq.md"
        platform.parent.mkdir()
        platform.write_text("platform", encoding="utf-8")
        self.assertEqual(self.book.read(self.umo)[0], "platform")
        session = self.book.session_path(self.umo)
        session.parent.mkdir()
        session.write_text("session", encoding="utf-8")
        self.assertEqual(self.book.read(self.umo)[0], "session")
        custom = self.root / "custom.md"
        custom.write_text("custom", encoding="utf-8")
        self.config["heartbeat_file_path"] = str(custom)
        self.assertEqual(self.book.read(self.umo)[0], "custom")

    def test_session_overrides_are_actually_per_conversation(self):
        session = self.book.session_path(self.umo)
        session.parent.mkdir()
        session.write_text("user one", encoding="utf-8")
        self.assertEqual(self.book.read(self.umo)[0], "user one")
        self.assertEqual(self.book.read("qq:FriendMessage:2")[0], DEFAULT_RULES)

    def test_webui_updates_inherited_copy_without_overwriting_files(self):
        platform = self.root / "heartbeat.d" / "qq.md"
        platform.parent.mkdir()
        original = "> 自动生成，编辑全局 HEARTBEAT.md 或直接修改此文件。\n\n" + DEFAULT_RULES
        platform.write_text(original, encoding="utf-8")
        self.config["heartbeat_content"] = "updated default"
        self.assertEqual(self.book.read(self.umo)[0], "updated default")
        self.assertEqual(platform.read_text(encoding="utf-8"), original)
        self.assertEqual((self.root / "HEARTBEAT.md").read_text(encoding="utf-8"), DEFAULT_RULES)

    def test_missing_explicit_file_fails_instead_of_silently_changing_rules(self):
        self.config["heartbeat_file_path"] = str(self.root / "missing.md")
        with self.assertRaises(FileNotFoundError):
            self.book.read(self.umo)

    def test_existing_custom_handbook_is_preserved_on_initialization(self):
        path = self.root / "HEARTBEAT.md"
        path.write_text("my original rules", encoding="utf-8")
        self.book.initialize()
        self.assertEqual(path.read_text(encoding="utf-8"), "my original rules")
