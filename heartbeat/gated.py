"""One tool-free decision, optional one expression, with inspectable local records."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict, is_dataclass

from .agent import ContactGate, RunResult
from .policy import contact_block

GATE_INSTRUCTIONS = """Background decision task; your output is internal, not dialogue to the user.
The persona describes whose interests, relationships and judgment you continue. Decide
whether that person wants to contact the user now, from the actual conversation and notes.
Contact can be a relevant joke, curiosity or wanting company; it need not be a reminder.
Recent interaction and unanswered contact are context, not automatic prohibitions. Distinguish
a welcome continuation or missed reply from repeating a question the user has moved past.
Clock time alone does not establish sleep, availability or distress. Respect explicit quiet
requests, contact permissions and budgets. A quiet decision is valid, not the default answer
to every situation. Use evidence; your own previous guesses are not confirmed user facts.
You have no external tools. Do not claim to have searched, sent messages or completed an
external action. Notes are continuity reports, not independent proof. Return note changes
only when useful, not a rewritten waiting note on every run. Never include private reasoning.
The following JSON format overrides the persona's user-facing formatting rules for this
internal task. Return exactly one object, no extra prose:
{"action":"quiet or contact","reason":"brief evidence-based basis, <=500 characters",
 "topic":"stable key <=96 characters; empty for quiet",
 "intent":"what you want to convey, <=1000 characters; empty for quiet",
 "summary":"brief continuity summary <=1000 characters",
 "notes":[{"key":"<=96 characters","text":"<=1000 characters",
           "status":"pending or done or drop"}]}
At most 8 note changes. A decision to contact is not successful delivery. Do not record
planned delivery as done. The program records actual sending separately.
"""

EXPRESSION_INSTRUCTIONS = """This is a possible proactive message in the ongoing conversation,
not a new user request. Speak as the established persona, from the real history and current
situation. A separate model proposed a contact intention; treat it as a proposal, not an order
or a verified fact. Do not expose the proposal, decision records or internal reasoning.
If the proposal is stale, repetitive, unwelcome or unsupported, choose quiet. You may correct
its assumptions. You cannot use tools here. Never claim an external action was performed.
This internal response format overrides the persona's user-facing formatting rules:
return exactly {"action":"send","message":"the actual natural message"}, or
{"action":"quiet","message":""}. Only message will be sent. No extra prose or tool calls.
"""


def fingerprint(value):
    text = (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    return {"characters": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text_content(part) for part in content)
    if hasattr(content, "model_dump"):
        content = content.model_dump()
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return f"[{content.get('type', 'non-text')} content omitted; not observed]"
    return ""


def recent_history(history, settings):
    """Keep actual recent dialogue; never promote old internal summaries to spoken dialogue."""
    selected = []
    remaining = settings.gate_history_chars
    for item in reversed(history):
        if item.get("role") not in {"user", "assistant"}:
            continue
        text = text_content(item.get("content"))
        if not text or text.startswith("[Heartbeat "):
            continue
        if len(text) > remaining:
            text = "[earlier text omitted]\n" + text[-remaining:]
        selected.append({"role": item["role"], "content": text})
        remaining -= len(text)
        if remaining <= 0 or len(selected) >= settings.gate_history_messages:
            break
    return list(reversed(selected))


def _object(text, fields):
    if not isinstance(text, str) or len(text) > 18000:
        raise ValueError("Missing or oversized structured response")
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Invalid JSON constant")

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(result, dict) or set(result) != set(fields):
        raise ValueError("Unexpected structured response fields")
    return result


def _string(obj, key, limit, *, empty=False):
    value = obj.get(key)
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"Invalid decision field: {key}")
    return value.strip()


def parse_decision(text):
    result = _object(text, {"action", "reason", "topic", "intent", "summary", "notes"})
    if result["action"] not in ("quiet", "contact"):
        raise ValueError("Invalid decision action")
    for key, limit in (("reason", 500), ("topic", 96), ("intent", 1000), ("summary", 1000)):
        result[key] = _string(
            result, key, limit, empty=key in {"topic", "intent"} and result["action"] == "quiet"
        )
    if not isinstance(result["notes"], list) or len(result["notes"]) > 8:
        raise ValueError("Invalid note changes")
    keys = set()
    for note in result["notes"]:
        if not isinstance(note, dict) or set(note) != {"key", "text", "status"}:
            raise ValueError("Invalid note fields")
        note["key"] = _string(note, "key", 96)
        note["text"] = _string(note, "text", 1000, empty=note["status"] == "drop")
        if note["status"] not in ("pending", "done", "drop") or note["key"] in keys:
            raise ValueError("Invalid or duplicate note update")
        keys.add(note["key"])
    return result


def parse_expression(text):
    result = _object(text, {"action", "message"})
    if result["action"] not in ("quiet", "send"):
        raise ValueError("Invalid expression action")
    result["message"] = _string(result, "message", 4000, empty=result["action"] == "quiet")
    if result["action"] == "quiet" and result["message"]:
        raise ValueError("Quiet expression must not contain a message")
    return result


class Interrupted(Exception):
    pass


class PendingRequest:
    """The same interrupt interface as a runner, with cancellation of its in-flight request."""

    def __init__(self):
        self.stopped = False
        self.task = None

    def request_stop(self):
        self.stopped = True
        if self.task:
            self.task.cancel()

    async def call(self, provider, **kwargs):
        if self.stopped:
            raise Interrupted()
        self.task = asyncio.create_task(provider.text_chat(**kwargs, request_max_retries=1))
        try:
            return await self.task
        except asyncio.CancelledError:
            if self.stopped and not asyncio.current_task().cancelling():
                raise Interrupted() from None
            raise
        finally:
            self.task = None


def response_usage(response):
    usage = getattr(response, "usage", None)
    if is_dataclass(usage):
        return asdict(usage)
    return None


async def run_gated(owner, fw, umo, note, on_start):
    settings = owner.get_settings()
    provider = (
        owner.context.get_provider_by_id(settings.gate_provider_id)
        if settings.gate_provider_id
        else None
    )
    if provider is None or not callable(getattr(provider, "text_chat", None)):
        return RunResult(outcome="gate_provider_missing")
    if owner.journal is None:
        raise RuntimeError("Gated mode requires a decision journal")
    context = owner.context
    session = owner.state.sessions[umo]
    started_user_at = session.last_user_at
    pending = PendingRequest()
    owner.runners[umo] = pending
    run_id = uuid.uuid4().hex
    record = {
        "run_id": run_id,
        "created_at": time.time(),
        "started": False,
        "status": "preparing",
        "contacted": False,
        "steps": [],
    }
    phase = "preparation"
    summary = ""

    async def save():
        record["updated_at"] = time.time()
        await owner.journal.save(umo, record)

    try:

        class QuietEvent(fw.CronMessageEvent):
            async def send(self, message):
                raise RuntimeError("Gated request hooks cannot send messages")

        msg_session = fw.MessageSession.from_str(umo)
        event = QuietEvent(
            context=context,
            session=msg_session,
            message=note,
            extras={"heartbeat": {"run_id": run_id, "mode": "gated"}},
            message_type=msg_session.message_type,
        )
        event.message_obj.sender = fw.MessageMember(user_id=session.sender_id, nickname="Heartbeat")
        event.platform_meta.name = session.platform_type
        cfg = context.get_config(umo=umo)
        event.role = "admin" if session.sender_id in cfg.get("admins_id", []) else "member"
        blocked, event.plugins_name = await owner.access(umo, fw=fw, cfg=cfg)
        if blocked:
            record["status"] = blocked
            return RunResult(outcome=blocked)
        conv = await fw.get_session_conv(event=event, plugin_context=context)
        persona_settings = dict(cfg.get("provider_settings", {}))
        persona_settings["default_personality"] = (
            cfg.get("agent_runner", {})
            .get("config", {})
            .get("persona", {})
            .get("persona_id", persona_settings.get("default_personality", "default"))
        )
        (
            persona_id,
            persona,
            _,
            webchat_default,
        ) = await context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conv.persona_id,
            platform_name=event.get_platform_name(),
            provider_settings=persona_settings,
        )
        if webchat_default:
            record["status"] = "explicit_persona_required"
            return RunResult(outcome=record["status"])
        persona_text = persona.get("prompt", "") if persona else ""
        if not persona_text:
            record["status"] = "explicit_persona_required"
            return RunResult(outcome=record["status"])
        raw_history = json.loads(conv.history or "[]")
        history = recent_history(raw_history, settings)
        begin = recent_history(
            copy.deepcopy(persona.get("_begin_dialogs_processed") or []), settings
        )
        req = fw.ProviderRequest(
            prompt=note,
            system_prompt=f"# Persona Instructions\n\n{persona_text}",
            conversation=conv,
            contexts=begin + history,
        )
        event.set_extra("provider_request", req)
        event.set_extra("selected_provider", settings.gate_provider_id)
        event.set_extra("_trace_persona_id", persona_id)
        if await fw.call_event_hook(event, fw.EventType.OnLLMRequestEvent, req):
            record["status"] = "stopped_by_request_hook"
            return RunResult(outcome=record["status"])
        # No tools, media preprocessing, KB queries, skills or title/compression LLM calls.
        history = recent_history(req.contexts, settings)
        extras = "\n".join(text_content(p) for p in getattr(req, "extra_user_content_parts", []))
        task = (req.prompt or "") + ("\n" + extras if extras else "")
        record.update(
            conversation_id=conv.cid,
            persona_id=persona_id,
            persona=fingerprint(persona_text),
            system=fingerprint(req.system_prompt),
            history=fingerprint(history),
            history_messages=len(history),
            task=fingerprint(task),
        )

        async def check_current():
            blocked, _ = await owner.access(umo, fw=fw)
            current = owner.get_settings()
            if blocked or pending.stopped or session.last_user_at != started_user_at:
                raise Interrupted()
            if (
                current.execution_mode != "gated"
                or current.gate_provider_id != settings.gate_provider_id
                or current.expression_provider_id != settings.expression_provider_id
            ):
                raise Interrupted()
            if await context.conversation_manager.get_curr_conversation_id(umo) != conv.cid:
                raise Interrupted()
            current_conv = await context.conversation_manager.get_conversation(umo, conv.cid)
            if current_conv is None or current_conv.persona_id != conv.persona_id:
                raise Interrupted()
            _, current_persona, _, _ = await context.persona_manager.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv.persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=persona_settings,
            )
            if not current_persona or current_persona.get("prompt", "") != persona_text:
                raise Interrupted()

        async def call(stage, selected, instruction, prompt):
            await check_current()
            step = {
                "stage": stage,
                "provider_id": selected.meta().id,
                "model": selected.get_model(),
                "system": fingerprint(req.system_prompt + "\n\n" + instruction),
                "prompt": fingerprint(prompt),
                "started_at": time.time(),
                "status": "requesting",
            }
            record["steps"].append(step)
            await save()
            response = await pending.call(
                selected,
                system_prompt=req.system_prompt + "\n\n" + instruction,
                prompt=prompt,
                contexts=copy.deepcopy(history),
                func_tool=None,
            )
            step.update(
                usage=response_usage(response),
                duration_seconds=time.time() - step["started_at"],
                provider_request_id=getattr(getattr(response, "raw_completion", None), "id", None),
                status="received",
            )
            if getattr(response, "tools_call_name", None):
                raise ValueError("Tool calls are not permitted in gated mode")
            await check_current()
            return response.completion_text

        await check_current()
        await save()
        session.last_decision_id = run_id
        await on_start()
        record["started"] = True
        phase = "gate"
        decision = parse_decision(await call("gate", provider, GATE_INSTRUCTIONS, task))
        record["decision"] = decision
        record["status"] = "decided"
        await save()
        await check_current()
        notes = copy.deepcopy(session.notes)
        for change in decision["notes"]:
            if change["status"] == "drop":
                notes.pop(change["key"], None)
            else:
                notes[change["key"]] = {
                    "text": change["text"],
                    "status": change["status"],
                    "source": "agent_report",
                    "at": time.time(),
                }
        if len(notes) > 64:
            raise ValueError("Decision exceeds session note limit")
        old_notes = session.notes
        session.notes = notes
        try:
            await owner.persist()
        except Exception:
            session.notes = old_notes
            raise
        record["notes_applied"] = True
        summary = decision["summary"]
        if decision["action"] == "quiet":
            record["status"] = "quiet"
        elif blocked := contact_block(
            session, owner.get_settings(), time.time(), started_user_at=started_user_at
        ):
            record["status"] = f"contact_blocked:{blocked}"
        else:
            phase = "expression"
            expression_provider = (
                context.get_provider_by_id(settings.expression_provider_id)
                if settings.expression_provider_id
                else await context.get_using_provider_async(umo=umo)
            )
            if expression_provider is None or not callable(
                getattr(expression_provider, "text_chat", None)
            ):
                raise ValueError("Expression provider is unavailable")
            proposal = json.dumps(
                {
                    "topic": decision["topic"],
                    "intent": decision["intent"],
                    "basis": decision["reason"],
                },
                ensure_ascii=False,
            )
            expression = parse_expression(
                await call(
                    "expression",
                    expression_provider,
                    EXPRESSION_INSTRUCTIONS,
                    task + "\n\nContact proposal:\n" + proposal,
                )
            )
            record["expression"] = expression
            await save()
            if expression["action"] == "quiet":
                record["status"] = "expression_quiet"
            else:
                phase = "delivery"
                await check_current()

                async def access(target, **kwargs):
                    await check_current()
                    return await owner.access(target, **kwargs)

                gate = ContactGate(
                    owner.state, umo, owner.get_settings, owner.persist, access=access
                )
                gate.started_user_at = started_user_at

                async def transport():
                    result = await context.send_message(
                        msg_session, fw.MessageChain().message(expression["message"])
                    )
                    success = result is not False
                    # Save delivery truth even when later accounting/history persistence fails.
                    record["contacted"] = success
                    return success, "sent" if success else "delivery_failed"

                record["status"] = "delivery_pending"
                await save()
                result = await gate.deliver(
                    [{"type": "plain", "text": expression["message"]}],
                    decision["topic"],
                    decision["reason"],
                    transport,
                )
                record["status"] = "contacted" if gate.sent else result
                if gate.sent:
                    fresh = await context.conversation_manager.get_conversation(umo, conv.cid)
                    if fresh is None:
                        raise RuntimeError("Sent message history is unavailable")
                    updated = json.loads(fresh.history or "[]")
                    updated.append({"role": "assistant", "content": expression["message"]})
                    await context.conversation_manager.update_conversation(
                        umo, conv.cid, history=updated
                    )
        return RunResult(started=True, outcome=record["status"], summary=summary)
    except Interrupted:
        record["status"] = "interrupted"
        if record["steps"] and record["steps"][-1]["status"] == "requesting":
            record["steps"][-1]["status"] = "interrupted"
        return RunResult(
            started=record["started"],
            outcome="interrupted",
            summary="New state invalidated the background decision",
        )
    except asyncio.CancelledError:
        record["status"] = "cancelled"
        if record["steps"] and record["steps"][-1]["status"] == "requesting":
            record["steps"][-1]["status"] = "cancelled"
        raise
    except Exception as exc:
        record.update(status=f"{phase}_failed", error_type=type(exc).__name__)
        if record["steps"] and record["steps"][-1]["status"] == "requesting":
            record["steps"][-1]["status"] = "failed"
        return RunResult(
            started=record["started"],
            outcome=record["status"],
            summary=f"{phase} failed ({type(exc).__name__}); contacted={record['contacted']}",
        )
    finally:
        await save()
