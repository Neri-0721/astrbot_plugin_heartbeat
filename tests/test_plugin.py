import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_policy import NOW, UMO


def load_plugin():
    def decorator(*args, **kwargs):
        return lambda function: function

    api = ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=Mock(), error=Mock())
    event_api = ModuleType("astrbot.api.event")
    event_api.AstrMessageEvent = object
    event_api.filter = SimpleNamespace(
        on_astrbot_loaded=decorator,
        event_message_type=decorator,
        on_llm_request=decorator,
        command=decorator,
        permission_type=decorator,
        llm_tool=decorator,
        EventMessageType=SimpleNamespace(ALL="all"),
        PermissionType=SimpleNamespace(ADMIN="admin"),
    )
    provider_api = ModuleType("astrbot.api.provider")
    provider_api.ProviderRequest = object
    star_api = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star_api.Star = Star
    star_api.Context = object
    star_api.StarTools = SimpleNamespace()
    star_api.register = decorator
    fake_modules = {
        "astrbot": ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.provider": provider_api,
        "astrbot.api.star": star_api,
    }
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "heartbeat_test_plugin", root / "main.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, fake_modules | {spec.name: module}):
        spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin()


def event(umo=UMO, *, synthetic=False, group=False, admin=False):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_extra=lambda key: {"heartbeat": {"id": "background"}}.get(key) if synthetic else None,
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: "1",
        get_self_id=lambda: "bot",
        get_group_id=lambda: "group" if group else "",
        plain_result=lambda text: text,
        role="admin" if admin else "member",
    )


class SavedConfig(dict):
    def __init__(self, path, values):
        super().__init__(values)
        self.path = path
        self.fail_save = False
        self.save_config()

    def save_config(self):
        if self.fail_save:
            raise OSError("read-only config")
        self.path.write_text(json.dumps(self), encoding="utf-8")


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        PLUGIN.StarTools.get_data_dir = lambda name: Path(self.tmp.name)
        self.manager = SimpleNamespace(
            add_basic_job=AsyncMock(return_value=SimpleNamespace(job_id="job")),
            delete_job=AsyncMock(),
        )
        self.context = SimpleNamespace(
            get_config=lambda **kwargs: {"timezone": "UTC"}, cron_manager=self.manager
        )
        self.config = SavedConfig(
            Path(self.tmp.name) / "config.json",
            {
                "startup_behavior": "auto",
                "enabled_sessions": [UMO],
            },
        )
        self.plugin = PLUGIN.HeartbeatPlugin(self.context, self.config)
        self.clock = patch.object(PLUGIN.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        await self.plugin.initialize()
        await self.plugin._observe(event(), enroll=True)

    async def test_initialization_registers_one_token_free_minute_tick(self):
        await asyncio.gather(*(self.plugin._ensure_schedule() for _ in range(5)))
        self.assertEqual(self.manager.add_basic_job.await_count, 1)
        args = self.manager.add_basic_job.call_args.kwargs
        self.assertEqual(args["cron_expression"], "* * * * *")
        self.assertFalse(args["persistent"])

    async def test_synthetic_request_never_updates_user_activity_or_last_target(self):
        self.plugin.state.sessions[UMO].last_user_at = 123
        await self.plugin.on_request(event(synthetic=True), None)
        self.assertEqual(self.plugin.state.sessions[UMO].last_user_at, 123)

    async def test_raw_message_updates_known_session_without_model_request(self):
        self.plugin.state.sessions[UMO].last_user_at = 123
        await self.plugin.on_message(event())
        self.assertEqual(self.plugin.state.sessions[UMO].last_user_at, NOW)

    async def test_pause_is_durable_and_normal_chat_does_not_restart_it(self):
        async for _ in self.plugin.cmd_unschedule(event()):
            pass
        await self.plugin.on_message(event())
        self.assertTrue(self.plugin.state.paused)
        self.assertTrue(self.plugin.store.load().paused)
        self.assertEqual(self.manager.add_basic_job.await_count, 1)

    async def test_reschedule_reads_all_settings_not_only_interval(self):
        self.config.update(
            interval_minutes=45, target_mode="last", max_daily_messages=1, active_hours_end="22:00"
        )
        async for _ in self.plugin.cmd_reschedule(event()):
            pass
        self.assertEqual(self.plugin.settings.interval_minutes, 45)
        self.assertEqual(self.plugin.settings.target_mode, "last")
        self.assertEqual(self.plugin.settings.max_daily_messages, 1)
        self.assertEqual(self.plugin.settings.active_hours_end, "22:00")

    async def test_lock_covers_complete_agent_turn(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def run(umo, note, start):
            await start()
            entered.set()
            await release.wait()
            return SimpleNamespace(started=True, outcome="completed", summary="Done")

        self.plugin.agent.run = run
        first = asyncio.create_task(self.plugin._run_one(UMO, manual=True))
        await entered.wait()
        self.assertTrue(self.plugin._run_lock.locked())
        self.assertEqual(await self.plugin._run_one(UMO, manual=True), "background_busy")
        release.set()
        self.assertEqual(await first, "completed")
        self.assertEqual(self.plugin.state.wake_count, 1)
        self.assertEqual(self.plugin.store.load().wake_count, 1)

    async def test_preparation_failure_does_not_spend_wake_budget(self):
        self.plugin.agent.run = AsyncMock(
            return_value=SimpleNamespace(started=False, outcome="no_provider", summary="")
        )
        self.assertEqual(await self.plugin._run_one(UMO, manual=True), "no_provider")
        self.assertEqual(self.plugin.state.wake_count, 0)

    async def test_busy_first_session_does_not_starve_other_sessions(self):
        second = "qq:FriendMessage:2"
        session_type = type(self.plugin.state.sessions[UMO])
        self.plugin.state.sessions[UMO].next_due_at = 0
        self.config["enabled_sessions"].append(second)
        self.plugin.state.sessions[second] = session_type(
            enabled=True, sender_id="2", platform_type="qq"
        )
        self.plugin._run_one = AsyncMock(side_effect=["foreground_busy", "completed"])
        await self.plugin._preflight()
        self.assertEqual(self.plugin._run_one.await_count, 2)

    async def test_new_real_message_requests_background_stop(self):
        self.plugin.agent.interrupt = Mock()
        await self.plugin.on_message(event())
        self.plugin.agent.interrupt.assert_called_once_with(UMO)

    async def test_quiet_changes_contact_state_without_disabling_background(self):
        await self.plugin._set_quiet(event(), 120)
        session = self.plugin.state.sessions[UMO]
        self.assertTrue(session.enabled)
        self.assertEqual(session.quiet_until, NOW + 7200)
        self.assertIn("后台任务不能", await self.plugin._set_quiet(event(synthetic=True), 0))
        self.assertEqual(session.quiet_until, NOW + 7200)

    async def test_ordinary_chat_in_an_unlisted_session_never_enrolls_or_runs(self):
        other = "qq:FriendMessage:unused"
        await self.plugin.on_message(event(other))
        await self.plugin.on_request(event(other), None)
        self.assertNotIn(other, self.plugin.state.sessions)
        self.assertEqual(self.plugin.state.last_umo, UMO)
        self.assertEqual(await self.plugin._run_one(other, manual=True), "session_disabled")

    async def test_empty_configuration_disables_legacy_state_after_restart(self):
        session = self.plugin.state.sessions[UMO]
        session.notes["existing-task"] = {"text": "keep"}
        session.unanswered = 2
        self.plugin.state.wake_count = 12
        await self.plugin._persist()
        self.config["enabled_sessions"] = []
        restored = PLUGIN.HeartbeatPlugin(self.context, self.config)
        await restored.initialize()
        restored.agent.run = AsyncMock()
        await restored._preflight()
        restored.agent.run.assert_not_awaited()
        self.assertFalse(restored.state.sessions[UMO].enabled)
        self.assertEqual(restored.state.sessions[UMO].notes["existing-task"]["text"], "keep")
        self.assertEqual(restored.state.sessions[UMO].unanswered, 2)
        self.assertEqual(restored.state.wake_count, 12)

    async def test_enable_and_disable_commands_manage_the_same_config_file(self):
        other = "qq:FriendMessage:second"
        response = await self.plugin._set_enabled(event(other), True)
        self.assertIn("已开启", response)
        saved = json.loads(self.config.path.read_text())
        self.assertEqual(saved["enabled_sessions"], [UMO, other])
        session = self.plugin.state.sessions[other]
        session.notes["task"] = {"text": "keep"}
        self.plugin.agent.interrupt = Mock()
        response = await self.plugin._set_enabled(event(other), False)
        self.assertIn("已关闭", response)
        self.assertEqual(json.loads(self.config.path.read_text())["enabled_sessions"], [UMO])
        self.assertFalse(session.enabled)
        self.assertEqual(session.notes["task"]["text"], "keep")
        self.assertTrue(self.plugin.state.sessions[UMO].enabled)
        self.plugin.agent.interrupt.assert_called_with(other)
        restored_config = SavedConfig(self.config.path, json.loads(self.config.path.read_text()))
        restored = PLUGIN.HeartbeatPlugin(self.context, restored_config)
        self.assertTrue(restored.state.sessions[UMO].enabled)
        self.assertFalse(restored.state.sessions[other].enabled)

    async def test_runtime_config_removal_stops_only_that_session(self):
        other = "qq:FriendMessage:second"
        await self.plugin._set_enabled(event(other), True)
        self.config["enabled_sessions"] = [other]
        self.plugin.agent.interrupt = Mock()
        self.plugin._run_one = AsyncMock(return_value="completed")
        self.plugin.state.sessions[other].next_due_at = 0
        await self.plugin._preflight()
        self.plugin.agent.interrupt.assert_called_with(UMO)
        self.plugin._run_one.assert_awaited_once_with(other)

    async def test_failed_enable_is_not_granted_or_reported_as_success(self):
        other = "qq:FriendMessage:second"
        self.config.fail_save = True
        self.assertIn("保存失败", await self.plugin._set_enabled(event(other), True))
        self.assertNotIn(other, self.config["enabled_sessions"])
        self.assertFalse(self.plugin.state.sessions[other].enabled)
        self.assertEqual(await self.plugin._run_one(other, manual=True), "session_disabled")

    async def test_failed_disable_still_stops_runtime_and_reports_persistence_failure(self):
        self.config.fail_save = True
        self.assertIn("配置保存失败", await self.plugin._set_enabled(event(), False))
        self.assertFalse(self.plugin.state.sessions[UMO].enabled)
        self.assertNotIn(UMO, self.config["enabled_sessions"])
        self.assertEqual(await self.plugin._run_one(UMO, manual=True), "session_disabled")

    async def test_status_test_and_quiet_never_enable_a_session(self):
        other = "qq:FriendMessage:second"
        status = [r async for r in self.plugin.cmd_status(event(other))]
        self.assertIn(other, status[0])
        self.assertNotIn(other, self.plugin.state.sessions)
        responses = [r async for r in self.plugin.cmd_test(event(other))]
        self.assertIn("未开启", responses[0])
        await self.plugin._set_quiet(event(other), 120)
        self.assertNotIn(other, self.config["enabled_sessions"])
        self.assertFalse(self.plugin.state.sessions[other].enabled)

    async def test_background_cannot_enable_a_session_or_change_global_schedule(self):
        await self.plugin._set_enabled(event(synthetic=True), True)
        before = list(self.config["enabled_sessions"])
        await self.plugin._set_enabled(event(synthetic=True), False)
        self.assertEqual(self.config["enabled_sessions"], before)
        async for _ in self.plugin.cmd_unschedule(event(synthetic=True)):
            pass
        self.assertFalse(self.plugin.state.paused)

    async def test_group_opt_in_requires_both_configuration_and_admin(self):
        group = "qq:GroupMessage:1"
        self.assertIn(
            "不可控制", await self.plugin._set_enabled(event(group, group=True, admin=True), True)
        )
        self.config["allow_group_sessions"] = True
        self.assertIn("不可控制", await self.plugin._set_enabled(event(group, group=True), True))
        self.assertIn(
            "已开启", await self.plugin._set_enabled(event(group, group=True, admin=True), True)
        )

    async def test_explicit_enable_does_not_lift_global_pause(self):
        self.plugin.state.paused = True
        await self.plugin._set_enabled(event(), True)
        self.assertTrue(self.plugin.state.paused)
        self.assertEqual(await self.plugin._run_one(UMO, manual=True), "paused")

    async def test_configured_unknown_session_waits_for_real_metadata(self):
        other = "webchat:FriendMessage:window:2"
        self.config["enabled_sessions"].append(other)
        self.plugin._refresh_settings()
        self.assertNotIn(other, self.plugin.state.sessions)
        await self.plugin.on_message(event(other))
        self.assertTrue(self.plugin.state.sessions[other].enabled)
        self.assertEqual(self.plugin.state.sessions[other].sender_id, "1")

    async def test_other_sessions_activity_does_not_redirect_last_mode(self):
        self.config["target_mode"] = "last"
        await self.plugin.on_request(event("qq:FriendMessage:unused"), None)
        self.plugin._run_one = AsyncMock(return_value="completed")
        self.plugin.state.sessions[UMO].next_due_at = 0
        await self.plugin._preflight()
        self.plugin._run_one.assert_awaited_once_with(UMO)

    async def test_old_single_session_file_cannot_grant_permission(self):
        self.plugin.store.path.unlink()
        self.plugin.store.legacy_path.write_text(
            json.dumps({"umo": UMO, "sender_id": "1", "saved_at": 123})
        )
        self.config["enabled_sessions"] = []
        restored = PLUGIN.HeartbeatPlugin(self.context, self.config)
        self.assertEqual(restored.state.sessions[UMO].last_user_at, 123)
        self.assertFalse(restored.state.sessions[UMO].enabled)
        self.assertEqual(await restored._run_one(UMO, manual=True), "session_disabled")

    async def test_missing_legacy_metadata_does_not_starve_ready_sessions(self):
        legacy = "aa:FriendMessage:old"
        self.config["enabled_sessions"].append(legacy)
        self.plugin.state.sessions[legacy] = type(self.plugin.state.sessions[UMO])(enabled=True)
        self.plugin.state.sessions[UMO].next_due_at = 0
        self.plugin._run_one = AsyncMock(return_value="completed")
        await self.plugin._preflight()
        self.plugin._run_one.assert_awaited_once_with(UMO)

    async def test_turn_records_and_shutdown_are_independent_between_sessions(self):
        other = "qq:FriendMessage:second"
        await self.plugin._set_enabled(event(other), True)

        async def run(umo, note, start):
            await start()
            return SimpleNamespace(started=True, outcome="completed", summary="Result for " + umo)

        self.plugin.agent.run = run
        await self.plugin._run_one(UMO, manual=True)
        with patch.object(PLUGIN.time, "time", return_value=NOW + 121):
            await self.plugin._run_one(other, manual=True)
        with patch.object(PLUGIN.time, "time", return_value=NOW + 242):
            await self.plugin._set_enabled(event(), False)
            await self.plugin._run_one(other, manual=True)
        self.assertEqual(self.plugin.state.sessions[UMO].last_summary, "Result for " + UMO)
        self.assertEqual(self.plugin.state.sessions[other].last_summary, "Result for " + other)
        self.assertFalse(self.plugin.state.sessions[UMO].enabled)
        self.assertTrue(self.plugin.state.sessions[other].enabled)
        self.assertEqual(self.plugin.state.wake_count, 3)
