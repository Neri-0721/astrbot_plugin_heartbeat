import asyncio
import importlib.util
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


def event(umo=UMO, *, synthetic=False):
    return SimpleNamespace(
        unified_msg_origin=umo,
        get_extra=lambda key: {"heartbeat": {"id": "background"}}.get(key) if synthetic else None,
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: "1",
        get_self_id=lambda: "bot",
        get_group_id=lambda: "",
        plain_result=lambda text: text,
    )


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
        self.config = {"startup_behavior": "auto"}
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
        self.plugin.state.sessions[second] = session_type()
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
