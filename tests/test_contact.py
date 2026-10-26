import unittest
from types import SimpleNamespace

from heartbeat.agent import ContactGate, guarded_send_tool, remember_tool
from heartbeat.policy import Settings
from heartbeat.state import HeartbeatState, SessionState
from tests.test_policy import NOW, UMO


class FakeTool:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ContactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = HeartbeatState(
            sessions={UMO: SessionState(enabled=True, last_user_at=NOW - 20000)}
        )
        self.saves = 0
        self.calls = 0

        async def save():
            self.saves += 1

        self.save = save
        self.gate = ContactGate(
            self.state,
            UMO,
            lambda: Settings(enabled_sessions=(UMO,), timezone="UTC"),
            save,
            clock=lambda: NOW,
        )
        self.messages = [{"type": "plain", "text": "hello"}]

    async def transport(self):
        self.calls += 1
        return True, "sent"

    async def test_success_counts_once_and_next_attempt_is_blocked(self):
        self.assertEqual(
            await self.gate.deliver(
                self.messages, "greeting", "relevant follow-up", self.transport
            ),
            "sent",
        )
        self.assertIn(
            "already_sent_this_turn",
            await self.gate.deliver(self.messages, "other", "reason", self.transport),
        )
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.state.sessions[UMO].unanswered, 1)
        self.assertEqual(self.saves, 1)

    async def test_new_user_message_prevents_send_at_last_moment(self):
        self.state.sessions[UMO].observe_user("1", NOW)
        self.assertIn(
            "new_user_message",
            await self.gate.deliver(self.messages, "greeting", "reason", self.transport),
        )
        self.assertEqual(self.calls, 0)

    async def test_failed_delivery_does_not_count_or_claim_success(self):
        async def failed():
            return False, "error: platform unavailable"

        self.assertIn(
            "error:", await self.gate.deliver(self.messages, "greeting", "reason", failed)
        )
        self.assertEqual(self.state.sessions[UMO].contacts, [])
        self.assertFalse(self.gate.sent)

    async def test_concurrent_tool_calls_cannot_double_send(self):
        import asyncio

        await asyncio.gather(
            *(
                self.gate.deliver(self.messages, "greeting", "reason", self.transport)
                for _ in range(5)
            )
        )
        self.assertEqual(self.calls, 1)

    async def test_contact_requires_reason_and_stable_topic(self):
        self.assertIn(
            "blocked:", await self.gate.deliver(self.messages, "", "reason", self.transport)
        )
        self.assertIn(
            "blocked:", await self.gate.deliver(self.messages, "topic", "", self.transport)
        )
        self.assertEqual(self.calls, 0)

    async def test_guarded_tool_keeps_original_media_schema_and_transport(self):
        original = FakeTool(
            name="send_message_to_user",
            description="send",
            parameters={
                "type": "object",
                "properties": {"messages": {"type": "array"}},
                "required": ["messages"],
            },
        )

        async def send(context, **kwargs):
            await context.context.context.send_message(UMO, kwargs["messages"])
            return "Message sent"

        original.call = send
        tool = guarded_send_tool(SimpleNamespace(FunctionTool=FakeTool), original, self.gate)
        context = SimpleNamespace(
            context=SimpleNamespace(context=SimpleNamespace(send_message=self.send_message))
        )
        self.assertIn("messages", tool.parameters["properties"])
        self.assertEqual(original.parameters["required"], ["messages"])
        result = await tool.call(context, messages=self.messages, reason="reason", topic="greeting")
        self.assertEqual(result, "Message sent")
        self.assertEqual(self.calls, 1)
        self.assertEqual(context.context.context.send_message, self.send_message)

    async def send_message(self, target, messages):
        self.calls += 1
        return True

    async def test_guarded_tool_blocks_cross_session_contact(self):
        original = FakeTool(
            name="send_message_to_user",
            description="send",
            parameters={"type": "object", "properties": {}, "required": []},
        )
        tool = guarded_send_tool(SimpleNamespace(FunctionTool=FakeTool), original, self.gate)
        self.assertIn("bound session", await tool.call(None, session="other"))

    async def test_task_notes_are_persisted_as_agent_reports(self):
        tool = remember_tool(SimpleNamespace(FunctionTool=FakeTool), self.state, UMO, self.save)
        await tool.call(None, key="book", text="Read chapter 1", status="pending")
        note = self.state.sessions[UMO].notes["book"]
        self.assertEqual(note["source"], "agent_report")
        await tool.call(None, key="book", text="Chapter 1 read", status="done")
        self.assertEqual(self.state.sessions[UMO].notes["book"]["status"], "done")

    async def test_removal_from_configuration_blocks_send_even_with_stale_state(self):
        self.gate.get_settings = lambda: Settings()
        self.assertIn(
            "session_disabled",
            await self.gate.deliver(self.messages, "topic", "reason", self.transport),
        )
        self.assertEqual(self.calls, 0)

    async def test_host_disable_during_a_turn_blocks_delivery(self):
        async def access(umo):
            return "host_llm_disabled", None

        self.gate.access = access
        self.assertIn(
            "host_llm_disabled",
            await self.gate.deliver(self.messages, "topic", "reason", self.transport),
        )
        self.assertEqual(self.calls, 0)
