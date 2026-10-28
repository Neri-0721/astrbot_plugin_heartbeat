"""Version-scoped adapter to AstrBot's complete main-agent lifecycle."""

from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass, fields
from types import SimpleNamespace

from .policy import contact_block, message_digest
from .sessions import session_access

BACKGROUND_INSTRUCTIONS = """This is a quiet interval in the ongoing conversation, not a new user message.
Continue the established persona, relationship, interests and unfinished intentions.
Use the conversation, memory, handbook and previous activity to choose what matters to you.
You may follow your curiosity, continue something, share a thought, ask a question, make a
playful callback, invite the other person, or leave this moment quiet. Wanting to talk can
be a valid reason; you do not need a service task or a newly discovered fact to justify it.
Available time and contact allowances are not duties to send messages. Clock time does not
establish someone's sleep schedule, and silence alone does not prove distress or rejection.
Respect their stated wishes and space. Base real-world observations and completed actions
on context or actual tool results; keep imagination within an already shared fictional scene.

Separate silent work from contacting the user. Respect the supplied contact restrictions.
Use send_message_to_user for contact and give a concrete reason and a stable topic key
(e.g. ongoing-book-talk, weekend-plans). Personal curiosity or a relevant playful thought
can be the reason. Do not circumvent its limits using other tools,
other sessions, shell, HTTP, or MCP. Do not schedule replacement messages to bypass them.
Use heartbeat_remember for useful continuity: unfinished intentions, follow-ups or actual
results. Do not claim completion without doing it.
Finish with a brief factual activity summary for the next wake. Your final text is an
internal result and is not automatically sent to the user. Never expose private reasoning.
"""


def load_framework():
    # Keep AstrBot imports here so state/policy tests do not need a running host.
    from astrbot.core import sp
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.astr_main_agent import (
        MainAgentBuildConfig,
        _get_session_conv,
        _plugin_tool_fix,
        build_main_agent,
    )
    from astrbot.core.config.agent_runner import resolve_context_compression_config
    from astrbot.core.cron.events import CronMessageEvent
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.pipeline.context_utils import call_event_hook
    from astrbot.core.platform.astrbot_message import MessageMember
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.star.session_llm_manager import SessionServiceManager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType
    from astrbot.core.utils.session_lock import session_lock_manager

    return SimpleNamespace(
        FunctionTool=FunctionTool,
        ToolSet=ToolSet,
        MainAgentBuildConfig=MainAgentBuildConfig,
        resolve_compression=resolve_context_compression_config,
        get_session_conv=_get_session_conv,
        build_main_agent=build_main_agent,
        CronMessageEvent=CronMessageEvent,
        MessageChain=MessageChain,
        MessageMember=MessageMember,
        MessageSession=MessageSession,
        ProviderRequest=ProviderRequest,
        call_event_hook=call_event_hook,
        EventType=EventType,
        session_lock_manager=session_lock_manager,
        SessionServiceManager=SessionServiceManager,
        sp=sp,
        star_map=star_map,
        filter_plugin_tools=_plugin_tool_fix,
    )


def build_options(fw, cfg, timezone):
    """Translate both the old and the structured host config at one seam."""
    settings = dict(cfg.get("provider_settings", {}))
    allowed = {f.name for f in fields(fw.MainAgentBuildConfig)}
    options = {k: v for k, v in settings.items() if k in allowed}
    runner = cfg.get("agent_runner", {}).get("config", {})
    if runner:
        misc = runner.get("misc", {})
        persona = runner.get("persona", {})
        model = runner.get("model", {})
        options.update({k: v for k, v in misc.items() if k in allowed})
        options.update(fw.resolve_compression(runner.get("compression", {})))
        options.update(
            llm_safety_mode=persona.get("safety_mode", True),
            safety_mode_strategy=persona.get("safety_mode_strategy", "system_prompt"),
            fallback_provider_ids=model.get("fallback_provider_ids", []),
            request_max_retries=model.get("request_max_retries", 5),
        )
        settings["default_personality"] = persona.get("persona_id", "default")
    options.update(
        tool_call_timeout=runner.get("misc", {}).get(
            "tool_call_timeout", settings.get("tool_call_timeout", 120)
        ),
        streaming_response=False,
        provider_settings=settings,
        timezone=timezone or None,
        kb_agentic_mode=cfg.get("kb_agentic_mode", False),
        subagent_orchestrator=cfg.get("subagent_orchestrator", {}),
    )
    return {k: v for k, v in options.items() if k in allowed}


@dataclass
class RunResult:
    started: bool = False
    outcome: str = "skipped"
    summary: str = ""


class ContactGate:
    """Recheck policy at actual send time and count successful delivery only."""

    def __init__(self, state, umo, get_settings, persist, *, clock=time.time, access=None):
        self.state = state
        self.umo = umo
        self.get_settings = get_settings
        self.persist = persist
        self.clock = clock
        self.started_user_at = state.sessions[umo].last_user_at
        self.sent = False
        self.lock = asyncio.Lock()
        self.access = access

    async def deliver(self, messages, topic, reason, transport):
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 96:
            return "blocked: supply a stable topic key (1-96 characters)"
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            return "blocked: supply a concrete reason (1-500 characters)"
        if not isinstance(messages, list) or not messages:
            return "blocked: messages must be a non-empty list"
        topic = topic.strip().casefold()
        digest = message_digest(messages)
        async with self.lock:
            settings = self.get_settings()
            if self.umo not in settings.enabled_sessions:
                return "blocked: session_disabled"
            if self.access and (blocked := (await self.access(self.umo))[0]):
                return f"blocked: {blocked}"
            if self.state.paused:
                return "blocked: heartbeat_paused"
            if self.sent:
                return "blocked: already_sent_this_turn"
            session = self.state.sessions[self.umo]
            settings = self.get_settings()
            if ":GroupMessage:" in self.umo and not settings.allow_group_sessions:
                return "blocked: group_sessions_disabled"
            now = self.clock()
            blocked = contact_block(
                session,
                settings,
                now,
                started_user_at=self.started_user_at,
                topic=topic,
                digest=digest,
            )
            if blocked:
                return f"blocked: {blocked}; continue silently or finish"
            success, result = await transport()
            if success:
                self.sent = True
                now = self.clock()
                session.record_contact(now, settings.day(now), topic, digest, reason.strip())
                await self.persist()
            return result


def guarded_send_tool(fw, original, gate):
    parameters = copy.deepcopy(original.parameters)
    parameters["properties"].update(
        {
            "reason": {"type": "string", "description": "Concrete reason for contacting now."},
            "topic": {
                "type": "string",
                "description": "Stable topic key; reused for the same reminder.",
            },
        }
    )
    parameters["required"] = list(
        dict.fromkeys([*parameters.get("required", []), "reason", "topic"])
    )

    class GuardedSend(fw.FunctionTool):
        async def call(self, run_context, **kwargs):
            target = kwargs.get("session") or gate.umo
            if target != gate.umo:
                return "blocked: heartbeat contact is restricted to its bound session"

            async def transport():
                delivered = False
                original_context = run_context.context.context

                class DeliveryContext:
                    def __getattr__(self, name):
                        return getattr(original_context, name)

                    async def send_message(self, session, message):
                        nonlocal delivered
                        result = await original_context.send_message(session, message)
                        delivered = result is not False
                        return result

                scoped = copy.copy(run_context)
                scoped.context = copy.copy(run_context.context)
                scoped.context.context = DeliveryContext()
                args = {k: v for k, v in kwargs.items() if k not in {"reason", "topic"}}
                result = await original.call(scoped, **args)
                return delivered, result

            return await gate.deliver(
                kwargs.get("messages"), kwargs.get("topic"), kwargs.get("reason"), transport
            )

    return GuardedSend(
        name=original.name,
        description=original.description
        + " Heartbeat contact limits apply; reason and topic are required.",
        parameters=parameters,
    )


def remember_tool(fw, state, umo, persist):
    class Remember(fw.FunctionTool):
        async def call(self, run_context, **kwargs):
            key = kwargs.get("key", "")
            text = kwargs.get("text", "")
            status = kwargs.get("status", "pending")
            if not isinstance(key, str) or not 1 <= len(key.strip()) <= 96:
                return "error: key must have 1-96 characters"
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
                return "error: text must have 1-1000 characters"
            if status not in {"pending", "done", "drop"}:
                return "error: status must be pending, done or drop"
            notes = state.sessions[umo].notes
            if key not in notes and len(notes) >= 64:
                return "error: note limit reached; update or drop an existing note"
            if status == "drop":
                notes.pop(key, None)
            else:
                notes[key] = {
                    "text": text.strip(),
                    "status": status,
                    "source": "agent_report",
                    "at": time.time(),
                }
            await persist()
            return "Saved background note. This is an Agent report, not independent proof of completion."

    return Remember(
        name="heartbeat_remember",
        description="Keep brief continuity for the next heartbeat: intentions, follow-ups or actual results. Use stable keys; mark completed actions done.",
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "text": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "done", "drop"]},
            },
            "required": ["key", "text", "status"],
        },
    )


class FullAgent:
    def __init__(self, context, state, get_settings, persist, *, journal=None):
        self.context = context
        self.state = state
        self.get_settings = get_settings
        self.persist = persist
        self.runners = {}
        self.journal = journal

    def interrupt(self, umo):
        if runner := self.runners.get(umo):
            runner.request_stop()

    async def access(self, umo, *, fw=None, cfg=None):
        settings = self.get_settings()
        if umo not in settings.enabled_sessions or not self.state.sessions[umo].enabled:
            return "session_disabled", None
        if self.state.paused:
            return "heartbeat_paused", None
        fw = fw or load_framework()
        cfg = cfg if cfg is not None else self.context.get_config(umo=umo)
        return await session_access(fw, umo, cfg)

    async def run(self, umo, note, on_start):
        fw = load_framework()
        if blocked := (await self.access(umo, fw=fw))[0]:
            return RunResult(outcome=blocked)
        # A background turn yields immediately when foreground chat owns the lock.
        lock = fw.session_lock_manager.acquire_lock(umo)
        try:
            async with asyncio.timeout(0.1):
                await lock.__aenter__()
        except TimeoutError:
            return RunResult(outcome="foreground_busy")
        try:
            async with asyncio.timeout(self.get_settings().agent_timeout_seconds):
                return await self._run_locked(fw, umo, note, on_start)
        finally:
            self.runners.pop(umo, None)
            await lock.__aexit__(None, None, None)

    async def _run_locked(self, fw, umo, note, on_start):
        if self.get_settings().execution_mode == "gated":
            from .gated import run_gated

            return await run_gated(self, fw, umo, note, on_start)
        session = self.state.sessions[umo]
        gate = ContactGate(self.state, umo, self.get_settings, self.persist, access=self.access)
        context = self.context

        class HeartbeatEvent(fw.CronMessageEvent):
            async def send(self, message):
                if message is None:
                    return

                async def transport():
                    result = await context.send_message(self.session, message)
                    if result is False:
                        return False, "error: platform did not deliver the message"
                    await super(fw.CronMessageEvent, self).send(message)
                    return True, "Message sent by background tool"

                # Tools which send through event.send still share the contact budget.
                result = await gate.deliver(
                    [{"type": "plain", "text": str(message)}],
                    "tool-output",
                    "Background tool output",
                    transport,
                )
                if result.startswith("blocked:"):
                    raise RuntimeError(result)

        msg_session = fw.MessageSession.from_str(umo)
        event = HeartbeatEvent(
            context=context,
            session=msg_session,
            message=note,
            extras={"heartbeat": {"run_id": uuid.uuid4().hex}},
            message_type=msg_session.message_type,
        )
        event.message_obj.sender = fw.MessageMember(user_id=session.sender_id, nickname="Heartbeat")
        # Preserve adapter type for persona selection; the extra marks the synthetic origin.
        if session.platform_type:
            event.platform_meta.name = session.platform_type
        cfg = context.get_config(umo=umo)
        event.role = "admin" if session.sender_id in cfg.get("admins_id", []) else "member"
        blocked, event.plugins_name = await self.access(umo, fw=fw, cfg=cfg)
        if blocked:
            return RunResult(outcome=blocked)
        provider_settings = cfg.get("provider_settings", {})
        build_config = fw.MainAgentBuildConfig(
            **build_options(fw, cfg, self.get_settings().timezone)
        )
        conv = await fw.get_session_conv(event=event, plugin_context=context)
        req = fw.ProviderRequest(
            prompt=note,
            system_prompt=BACKGROUND_INSTRUCTIONS,
            conversation=conv,
            contexts=json.loads(conv.history or "[]"),
        )
        event.set_extra("provider_request", req)
        result = await fw.build_main_agent(
            event=event, plugin_context=context, config=build_config, req=req, apply_reset=False
        )
        if result is None:
            return RunResult(outcome="no_provider")
        reset = result.reset_coro
        try:
            # Match normal chat: decorate with persona/skills, then run request hooks,
            # then reset and execute. Heartbeat's own interaction hook ignores the marker.
            if await fw.call_event_hook(event, fw.EventType.OnLLMRequestEvent, req):
                return RunResult(outcome="stopped_by_request_hook")
            if blocked := (await self.access(umo, fw=fw))[0]:
                return RunResult(outcome=blocked)
            if (
                session.last_user_at > gate.started_user_at
                or self.state.paused
                or not session.enabled
            ):
                return RunResult(outcome="state_changed_before_start")
            req.func_tool = fw.ToolSet(list(req.func_tool.tools) if req.func_tool else [])
            fw.filter_plugin_tools(event, req)
            original = req.func_tool.get_tool("send_message_to_user")
            if original is not None:
                req.func_tool.add_tool(guarded_send_tool(fw, original, gate))
            req.func_tool.add_tool(remember_tool(fw, self.state, umo, self.persist))
            if reset is None:
                raise RuntimeError("AstrBot did not provide a deferred agent reset")
            await reset
            reset = None
            if blocked := (await self.access(umo, fw=fw))[0]:
                return RunResult(outcome=blocked)
            if (
                session.last_user_at > gate.started_user_at
                or self.state.paused
                or not session.enabled
            ):
                return RunResult(outcome="state_changed_before_start")
            runner = result.agent_runner
            self.runners[umo] = runner
            await on_start()
            if session.last_user_at > gate.started_user_at or (await self.access(umo, fw=fw))[0]:
                return RunResult(
                    started=True, outcome="interrupted", summary="Stopped before model step"
                )
            max_steps = (
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("misc", {})
                .get("max_steps", provider_settings.get("max_agent_step", 30))
            )
            async for _ in runner.step_until_done(max_steps):
                if (
                    self.state.paused
                    or session.last_user_at > gate.started_user_at
                    or (await self.access(umo, fw=fw))[0]
                ):
                    runner.request_stop()
            response = runner.get_final_llm_resp()
            summary = (response.completion_text or "")[:4000] if response else ""
            outcome = (
                "interrupted" if runner.was_aborted() else "completed" if response else "incomplete"
            )
            # The shared session lock excludes foreground history writes. Re-read the
            # conversation instead of overwriting it with the pre-turn snapshot.
            fresh = await context.conversation_manager.get_conversation(umo, conv.cid)
            if fresh is not None:
                history = json.loads(fresh.history or "[]")
                history.extend(
                    [
                        {
                            "role": "user",
                            "content": "[Heartbeat background activity; not a user request]",
                        },
                        {
                            "role": "assistant",
                            "content": f"[Heartbeat {outcome}; contacted={gate.sent}]\n{summary}",
                        },
                    ]
                )
                await context.conversation_manager.update_conversation(
                    umo, conv.cid, history=history
                )
            return RunResult(started=True, outcome=outcome, summary=summary)
        finally:
            if reset is not None:
                reset.close()
