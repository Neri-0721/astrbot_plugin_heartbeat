import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from heartbeat.agent import FullAgent
from heartbeat.policy import Settings
from heartbeat.state import HeartbeatState, SessionState
from tests.test_contact import FakeTool
from tests.test_policy import NOW, UMO


class FakeToolSet:
    def __init__(self, tools=None):
        self.tools = list(tools or [])

    def get_tool(self, name):
        return next((t for t in self.tools if t.name == name), None)

    def add_tool(self, tool):
        self.tools = [t for t in self.tools if t.name != tool.name] + [tool]


class FakeEvent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.message_obj = SimpleNamespace()
        self.platform_meta = SimpleNamespace(name="cron")

    @property
    def unified_msg_origin(self):
        return UMO

    def set_extra(self, key, value):
        self.extras[key] = value

    async def send(self, message):
        await self.context.send_message(self.session, message)


@dataclass
class FakeBuildConfig:
    tool_call_timeout: int
    streaming_response: bool = False
    provider_settings: dict | None = None
    timezone: str | None = None
    kb_agentic_mode: bool = False


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = HeartbeatState(
            sessions={
                UMO: SessionState(
                    sender_id="real-user", platform_type="aiocqhttp", last_user_at=NOW - 20000
                )
            }
        )
        self.events = []
        self.history = []
        self.sent = []
        self.lock = asyncio.Lock()
        self.stop_hook = False
        self.runner_error = False
        self.no_provider = False
        self.action = None
        self.saved = 0

        async def save():
            self.saved += 1

        async def get_conv(*args, **kwargs):
            return SimpleNamespace(cid="conversation", history=json.dumps(self.history))

        async def update(umo, cid, **kwargs):
            self.history = kwargs["history"]

        async def send(umo, message):
            self.sent.append(message)
            return True

        self.context = SimpleNamespace(
            get_config=lambda **kwargs: {
                "admins_id": ["real-user"],
                "plugin_set": ["allowed", "astrbot_plugin_heartbeat"],
                "provider_settings": {},
            },
            conversation_manager=SimpleNamespace(
                get_conversation=get_conv, update_conversation=update
            ),
            send_message=send,
        )
        self.agent = FullAgent(self.context, self.state, lambda: Settings(timezone="UTC"), save)
        owner = self

        class Runner:
            def request_stop(self):
                owner.events.append("stop")

            async def step_until_done(self, steps):
                owner.events.append("agent_begin")
                if owner.runner_error:
                    raise RuntimeError("provider failed")
                if owner.action:
                    await owner.action(owner.request, owner.event)
                owner.events.append("agent_done")
                yield None

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="Finished silently")

            def was_aborted(self):
                return False

        async def hook(event, event_type, req):
            self.events.append("request_hook")
            req.system_prompt += "\nInjected memory"
            return self.stop_hook

        async def build(**kwargs):
            self.events.append("build")
            self.assertFalse(kwargs["apply_reset"])
            self.request = kwargs["req"]
            self.event = kwargs["event"]
            self.request.func_tool = FakeToolSet()
            if self.no_provider:
                return None

            async def reset():
                self.events.append("reset")
                self.assertIn("Injected memory", self.request.system_prompt)
                self.assertIsNotNone(self.request.func_tool.get_tool("heartbeat_remember"))

            return SimpleNamespace(reset_coro=reset(), agent_runner=Runner())

        @asynccontextmanager
        async def acquire_lock(umo):
            async with self.lock:
                yield

        self.fw = SimpleNamespace(
            FunctionTool=FakeTool,
            ToolSet=FakeToolSet,
            MainAgentBuildConfig=FakeBuildConfig,
            CronMessageEvent=FakeEvent,
            MessageMember=lambda **kw: SimpleNamespace(**kw),
            MessageSession=SimpleNamespace(
                from_str=lambda _: SimpleNamespace(message_type="FriendMessage")
            ),
            ProviderRequest=lambda **kw: SimpleNamespace(**kw),
            EventType=SimpleNamespace(OnLLMRequestEvent="request"),
            call_event_hook=hook,
            get_session_conv=get_conv,
            build_main_agent=build,
            session_lock_manager=SimpleNamespace(acquire_lock=acquire_lock),
        )
        self.patch = patch("heartbeat.agent.load_framework", return_value=self.fw)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def start(self):
        self.events.append("reserve_wake")

    async def test_full_turn_runs_hooks_before_reset_and_saves_internal_summary(self):
        result = await self.agent.run(UMO, "task handbook", self.start)
        self.assertEqual(
            self.events,
            ["build", "request_hook", "reset", "reserve_wake", "agent_begin", "agent_done"],
        )
        self.assertTrue(result.started)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.event.message_obj.sender.user_id, "real-user")
        self.assertEqual(self.event.platform_meta.name, "aiocqhttp")
        self.assertEqual(self.event.plugins_name, ["allowed", "astrbot_plugin_heartbeat"])
        self.assertEqual(self.event.role, "admin")
        self.assertIn("Finished silently", self.history[-1]["content"])
        self.assertEqual(self.agent.runners, {})

    async def test_request_hook_can_stop_turn_without_spending_wake(self):
        self.stop_hook = True
        result = await self.agent.run(UMO, "task", self.start)
        self.assertFalse(result.started)
        self.assertNotIn("reserve_wake", self.events)
        self.assertNotIn("agent_begin", self.events)
        self.assertEqual(self.history, [])

    async def test_busy_foreground_defers_without_building_or_spending(self):
        await self.lock.acquire()
        result = await self.agent.run(UMO, "task", self.start)
        self.lock.release()
        self.assertEqual(result.outcome, "foreground_busy")
        self.assertEqual(self.events, [])

    async def test_missing_provider_does_not_spend_wake(self):
        self.no_provider = True
        result = await self.agent.run(UMO, "task", self.start)
        self.assertEqual(result.outcome, "no_provider")
        self.assertNotIn("reserve_wake", self.events)

    async def test_disabled_model_or_session_plugin_does_not_run(self):
        self.context.get_config = lambda **kwargs: {"plugin_set": ["other"]}
        result = await self.agent.run(UMO, "task", self.start)
        self.assertEqual(result.outcome, "plugin_disabled_for_session")
        self.context.get_config = lambda **kwargs: {"provider_settings": {"enable": False}}
        result = await self.agent.run(UMO, "task", self.start)
        self.assertEqual(result.outcome, "model_disabled")
        self.assertEqual(self.events, [])

    async def test_failure_releases_session_lock_and_runner(self):
        self.runner_error = True
        with self.assertRaises(RuntimeError):
            await self.agent.run(UMO, "task", self.start)
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.agent.runners, {})

    async def test_new_user_during_prepare_aborts_before_first_model_step(self):
        original_hook = self.fw.call_event_hook

        async def changed(event, event_type, req):
            self.state.sessions[UMO].last_user_at += 1
            return await original_hook(event, event_type, req)

        self.fw.call_event_hook = changed
        result = await self.agent.run(UMO, "task", self.start)
        self.assertEqual(result.outcome, "state_changed_before_start")
        self.assertNotIn("reserve_wake", self.events)

    async def test_agent_can_do_silent_task_during_contact_quiet_period(self):
        self.state.sessions[UMO].quiet_until = NOW + 3600

        async def action(req, event):
            await req.func_tool.get_tool("heartbeat_remember").call(
                None, key="todo", text="Prepared information", status="done"
            )

        self.action = action
        result = await self.agent.run(UMO, "task", self.start)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(self.state.sessions[UMO].notes["todo"]["status"], "done")
        self.assertEqual(self.sent, [])
