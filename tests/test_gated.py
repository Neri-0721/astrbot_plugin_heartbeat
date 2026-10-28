import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from heartbeat.agent import build_options
from heartbeat.gated import parse_decision, parse_expression, recent_history
from heartbeat.journal import DecisionJournal
from heartbeat.policy import Settings, contact_block
from tests import test_agent as fixture
from tests.test_policy import NOW, UMO


def decision(action="quiet", **kwargs):
    result = {
        "action": action,
        "reason": "Respond to an unfinished joke" if action == "contact" else "No new intention",
        "topic": "ongoing-joke" if action == "contact" else "",
        "intent": "Continue our joke" if action == "contact" else "",
        "summary": "Continue naturally next time",
        "notes": [],
    }
    result.update(kwargs)
    return json.dumps(result)


class Provider:
    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.requests = []
        self.effect = None

    def meta(self):
        return SimpleNamespace(id=self.name)

    def get_model(self):
        return self.name + "-model"

    async def text_chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if self.effect:
            await self.effect()
        return SimpleNamespace(completion_text=self.text, tools_call_name=[], usage=None)


class GatedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.AgentTests.asyncSetUp(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = replace(self.settings, execution_mode="gated", gate_provider_id="gate")
        self.agent.journal = DecisionJournal(Path(self.tmp.name))
        self.gate = Provider("gate", decision())
        self.expression = Provider(
            "expression", '{"action":"send","message":"A natural continuation"}'
        )
        self.context.get_provider_by_id = lambda key: {
            "gate": self.gate,
            "expression": self.expression,
        }.get(key)
        self.context.get_using_provider_async = AsyncMock(return_value=self.expression)
        self.persona = {"prompt": "Full unchanged PERSONA_MARKER", "_begin_dialogs_processed": []}
        self.context.persona_manager = SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                side_effect=lambda **kwargs: ("persona", self.persona, None, False)
            )
        )

        async def get_conv(*args, **kwargs):
            return SimpleNamespace(
                cid="conversation", persona_id="persona", history=json.dumps(self.history)
            )

        self.fw.get_session_conv = get_conv
        self.context.conversation_manager.get_conversation = get_conv
        self.context.conversation_manager.get_curr_conversation_id = AsyncMock(
            return_value="conversation"
        )
        self.fw.CronMessageEvent.get_platform_name = lambda event: event.platform_meta.name
        self.fw.MessageChain = lambda: SimpleNamespace(message=lambda text: text)
        self.fw.ProviderRequest = lambda **kw: SimpleNamespace(extra_user_content_parts=[], **kw)
        self.clock = patch("heartbeat.gated.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def start(self):
        self.events.append("reserve_wake")

    def record(self):
        paths = list(self.agent.journal.session_path(UMO).glob("*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    async def run_turn(self):
        return await self.agent.run(UMO, "Current time and handbook", self.start)

    async def test_quiet_is_one_gate_call_no_expression_no_history_pollution(self):
        self.history[:] = [{"role": "user", "content": "Hello"}]
        result = await self.run_turn()
        self.assertEqual(result.outcome, "quiet")
        self.assertEqual(len(self.gate.requests), 1)
        self.context.get_using_provider_async.assert_not_awaited()
        self.assertEqual(self.expression.requests, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.history, [{"role": "user", "content": "Hello"}])
        req = self.gate.requests[0]
        self.assertIn("PERSONA_MARKER", req["system_prompt"])
        self.assertIn("Injected memory", req["system_prompt"])
        self.assertIsNone(req["func_tool"])
        self.assertEqual(len(self.record()["steps"]), 1)

    async def test_contact_is_one_call_each_despite_recent_chat_and_only_actual_text_enters_history(
        self,
    ):
        self.gate.text = decision("contact")
        self.state.sessions[UMO].last_user_at = NOW - 60
        self.state.sessions[UMO].last_contact_at = NOW - 120
        result = await self.run_turn()
        self.assertEqual(result.outcome, "contacted")
        self.assertEqual([len(self.gate.requests), len(self.expression.requests)], [1, 1])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.history, [{"role": "assistant", "content": "A natural continuation"}])
        self.assertEqual(self.gate.requests[0]["contexts"], self.expression.requests[0]["contexts"])
        self.assertTrue(self.record()["contacted"])
        self.assertEqual(self.state.sessions[UMO].unanswered, 1)

    async def test_gate_notes_written_without_tools_or_second_request(self):
        self.gate.text = decision(
            notes=[{"key": "topic", "text": "Unfinished thought", "status": "pending"}]
        )
        await self.run_turn()
        self.assertEqual(self.state.sessions[UMO].notes["topic"]["source"], "agent_report")
        self.assertTrue(self.record()["notes_applied"])
        self.assertEqual(len(self.gate.requests), 1)

    async def test_invalid_or_duplicate_decision_never_invokes_expression(self):
        self.gate.text = '{"action":"contact","action":"quiet"}'
        result = await self.run_turn()
        self.assertEqual(result.outcome, "gate_failed")
        self.assertEqual(self.expression.requests, [])
        self.assertFalse(self.record()["contacted"])

    async def test_provider_error_never_falls_back_to_expression(self):
        async def fail():
            raise RuntimeError("fixture error")

        self.gate.effect = fail
        self.assertEqual((await self.run_turn()).outcome, "gate_failed")
        self.assertEqual(self.expression.requests, [])

    async def test_explicit_quiet_and_daily_budget_skip_expression_even_if_gate_says_contact(self):
        self.gate.text = decision("contact")
        self.state.sessions[UMO].quiet_until = NOW + 3600
        self.assertIn("user_quiet_period", (await self.run_turn()).outcome)
        self.assertEqual(self.expression.requests, [])

    async def test_expression_may_decline_without_sending(self):
        self.gate.text = decision("contact")
        self.expression.text = '{"action":"quiet","message":""}'
        self.assertEqual((await self.run_turn()).outcome, "expression_quiet")
        self.assertEqual(self.sent, [])
        self.assertFalse(self.record()["contacted"])

    async def test_daily_message_budget_skips_expression(self):
        self.gate.text = decision("contact")
        self.state.sessions[UMO].contacts = [{"day": self.settings.day(NOW)}] * 12
        self.assertIn("message_budget", (await self.run_turn()).outcome)
        self.assertEqual(self.expression.requests, [])

    async def test_usage_is_saved_without_model_reasoning(self):
        @dataclass
        class Usage:
            input_other: int = 100
            input_cached: int = 50
            output: int = 8

        async def response(**kwargs):
            return SimpleNamespace(
                completion_text=decision(),
                tools_call_name=[],
                usage=Usage(),
                reasoning_content="DO_NOT_STORE",
            )

        self.gate.text_chat = response
        await self.run_turn()
        record = self.record()
        self.assertEqual(record["steps"][0]["usage"]["input_cached"], 50)
        self.assertNotIn("DO_NOT_STORE", json.dumps(record))

    async def test_journal_failure_before_model_prevents_paid_calls(self):
        self.agent.journal.save = AsyncMock(side_effect=OSError("fixture disk failure"))
        with self.assertRaises(OSError):
            await self.run_turn()
        self.assertEqual(self.gate.requests, [])
        self.assertEqual(self.expression.requests, [])

    async def test_new_message_during_gate_prevents_notes_expression_and_send(self):
        self.gate.text = decision(
            "contact", notes=[{"key": "stale", "text": "old plan", "status": "pending"}]
        )

        async def new_message():
            self.state.sessions[UMO].observe_user("real-user", NOW)

        self.gate.effect = new_message
        self.assertEqual((await self.run_turn()).outcome, "interrupted")
        self.assertEqual(self.state.sessions[UMO].notes, {})
        self.assertEqual(self.expression.requests, [])

    async def test_new_message_during_expression_prevents_send(self):
        self.gate.text = decision("contact")

        async def new_message():
            self.state.sessions[UMO].observe_user("real-user", NOW)

        self.expression.effect = new_message
        self.assertEqual((await self.run_turn()).outcome, "interrupted")
        self.assertEqual(self.sent, [])

    async def test_interrupt_cancels_inflight_request_and_releases_session_lock(self):
        entered = asyncio.Event()

        async def hold():
            entered.set()
            await asyncio.Event().wait()

        self.gate.effect = hold
        task = asyncio.create_task(self.run_turn())
        await entered.wait()
        self.agent.interrupt(UMO)
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result.outcome, "interrupted")
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.agent.runners, {})

    async def test_parent_cancellation_preserves_record_and_releases_lock(self):
        entered = asyncio.Event()

        async def hold():
            entered.set()
            await asyncio.Event().wait()

        self.gate.effect = hold
        task = asyncio.create_task(self.run_turn())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.record()["status"], "cancelled")
        self.assertFalse(self.lock.locked())

    async def test_failed_send_not_counted_or_saved_as_spoken_history(self):
        self.gate.text = decision("contact")
        self.context.send_message = AsyncMock(return_value=False)
        self.assertEqual((await self.run_turn()).outcome, "delivery_failed")
        self.assertEqual(self.state.sessions[UMO].contacts, [])
        self.assertEqual(self.history, [])
        self.assertFalse(self.record()["contacted"])

    async def test_invalid_expression_never_sent(self):
        self.gate.text = decision("contact")
        self.expression.text = "I am not valid JSON"
        self.assertEqual((await self.run_turn()).outcome, "expression_failed")
        self.assertEqual(self.sent, [])

    async def test_missing_gate_does_not_spend_wake_or_select_expression(self):
        self.settings = replace(self.settings, gate_provider_id="missing")
        result = await self.run_turn()
        self.assertFalse(result.started)
        self.assertEqual(result.outcome, "gate_provider_missing")
        self.assertNotIn("reserve_wake", self.events)

    async def test_full_note_store_rejects_whole_update_without_partial_mutation(self):
        self.state.sessions[UMO].notes = {str(i): {"text": "old"} for i in range(64)}
        self.gate.text = decision(
            "contact", notes=[{"key": "extra", "text": "new", "status": "pending"}]
        )
        self.assertEqual((await self.run_turn()).outcome, "gate_failed")
        self.assertEqual(len(self.state.sessions[UMO].notes), 64)
        self.assertEqual(self.expression.requests, [])

    async def test_changed_persona_invalidates_gate_result(self):
        async def change():
            self.persona["prompt"] = "Another persona"

        self.gate.effect = change
        self.assertEqual((await self.run_turn()).outcome, "interrupted")

    async def test_switching_conversation_persona_invalidates_gate_result(self):
        async def change():
            self.context.conversation_manager.get_conversation = AsyncMock(
                return_value=SimpleNamespace(persona_id="new-persona")
            )

        self.gate.effect = change
        self.assertEqual((await self.run_turn()).outcome, "interrupted")
        self.assertEqual(self.expression.requests, [])

    async def test_sent_truth_survives_history_write_failure(self):
        self.gate.text = decision("contact")
        self.context.conversation_manager.update_conversation = AsyncMock(
            side_effect=OSError("fixture history failure")
        )
        self.assertEqual((await self.run_turn()).outcome, "delivery_failed")
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.record()["contacted"])
        self.assertEqual(len(self.state.sessions[UMO].contacts), 1)


class ContractTests(unittest.TestCase):
    def test_json_contract_is_strict_and_fenced_json_is_accepted(self):
        self.assertEqual(parse_decision("```json\n" + decision() + "\n```")["action"], "quiet")
        for raw in [
            decision(action="contact", topic=""),
            decision(notes=[{"key": "a", "text": "x", "status": "bad"}]),
            decision(extra="field"),
        ]:
            with self.assertRaises(ValueError):
                parse_decision(raw)
        with self.assertRaises(ValueError):
            parse_expression('{"action":"quiet","message":"hidden message"}')

    def test_history_keeps_real_text_filters_internal_summaries_and_never_fetches_images(self):
        h = [
            {"role": "user", "content": "[Heartbeat background activity; not a user request]"},
            {"role": "assistant", "content": "[Heartbeat completed]\ninternal"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "latest"},
                    {"type": "image_url", "image_url": "https://never-fetch.invalid"},
                ],
            },
        ]
        actual = recent_history(h, Settings())
        self.assertEqual(len(actual), 1)
        self.assertIn("latest", actual[0]["content"])
        self.assertNotIn("https://", actual[0]["content"])

    def test_new_config_adapter_uses_structured_host_values(self):
        @dataclass
        class Build:
            tool_call_timeout: int = 120
            llm_safety_mode: bool = True
            max_context_length: int = 50
            provider_settings: dict | None = None

        fw = SimpleNamespace(
            MainAgentBuildConfig=Build,
            resolve_compression=lambda c: {"max_context_length": c["max_turns"]},
        )
        cfg = {
            "provider_settings": {},
            "agent_runner": {
                "config": {
                    "misc": {"tool_call_timeout": 42},
                    "persona": {"safety_mode": False, "persona_id": "test"},
                    "compression": {"max_turns": 100},
                }
            },
        }
        actual = build_options(fw, cfg, "UTC")
        self.assertFalse(actual["llm_safety_mode"])
        self.assertEqual(actual["max_context_length"], 100)
        self.assertEqual(actual["tool_call_timeout"], 42)

    def test_gated_social_rules_are_advice_but_quiet_and_budget_are_hard(self):
        from heartbeat.state import SessionState

        s = SessionState(enabled=True, last_user_at=NOW - 1, last_contact_at=NOW - 2, unanswered=5)
        settings = Settings(execution_mode="gated")
        self.assertEqual(contact_block(s, settings, NOW, started_user_at=s.last_user_at), "")
        s.quiet_until = NOW + 1
        self.assertEqual(
            contact_block(s, settings, NOW, started_user_at=s.last_user_at), "user_quiet_period"
        )


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_separation_atomic_update_retention_and_path_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp), limit=2)
            for i in range(3):
                await journal.save(UMO, {"run_id": f"{i:032x}", "status": "quiet"})
            self.assertEqual(len(list(journal.session_path(UMO).glob("*.json"))), 2)
            await journal.save("other", {"run_id": "0" * 32, "status": "quiet"})
            self.assertEqual(len(list(journal.session_path("other").glob("*.json"))), 1)
            with self.assertRaises(ValueError):
                await journal.save(UMO, {"run_id": "../bad"})
