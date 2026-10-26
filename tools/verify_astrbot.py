"""Exercise the real AstrBot builder/runner with an offline provider and temp data.

Run with the host's Python: python tools/verify_astrbot.py --source <AstrBot source root>
No API credentials, live databases, platform adapters, or real messages are used.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


async def verify():
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.entities import LLMResponse, ProviderMeta, ProviderType
    from astrbot.core.provider.provider import Provider
    from astrbot.core.star.context import Context

    from heartbeat.agent import FullAgent
    from heartbeat.policy import Settings
    from heartbeat.state import HeartbeatState, SessionState

    umo = "offline:FriendMessage:test-user"
    steps = []
    requests = []
    deliveries = []
    history = []

    class OfflineProvider(Provider):
        def __init__(self):
            super().__init__({"id": "offline", "type": "offline", "max_context_tokens": 128000}, {})
            self.set_model("offline-fixture")
            self.calls = 0

        def get_current_key(self):
            return ""

        def set_key(self, key):
            pass

        async def get_models(self):
            return ["offline-fixture"]

        def meta(self):
            return ProviderMeta(
                id="offline",
                model=self.get_model(),
                type="offline",
                provider_type=ProviderType.CHAT_COMPLETION,
            )

        async def text_chat(self, **kwargs):
            self.calls += 1
            requests.append(kwargs)
            messages = [
                m.model_dump() if hasattr(m, "model_dump") else m
                for m in kwargs.get("contexts", [])
            ]
            system = str(messages[0].get("content"))
            assert "OFFLINE_PERSONA_MARKER" in system, "Selected persona was not injected"
            assert "OFFLINE_MEMORY_MARKER" in system, "Request hook did not run before reset"
            if self.calls % 2:
                return LLMResponse(
                    role="assistant",
                    tools_call_name=["heartbeat_remember", "send_message_to_user"],
                    tools_call_args=[
                        {
                            "key": "fixture-task",
                            "text": "Prepared fixture information",
                            "status": "done",
                        },
                        {
                            "messages": [{"type": "plain", "text": "Offline fixture message"}],
                            "reason": "Relevant task result",
                            "topic": "fixture-result",
                        },
                    ],
                    tools_call_ids=["remember", "send"],
                )
            return LLMResponse(
                role="assistant",
                completion_text="Prepared task result; contact obeyed the current policy.",
            )

    provider = OfflineProvider()

    class ToolManager:
        def get_full_tool_set(self):
            return ToolSet()

        def get_builtin_tool(self, cls):
            return cls()

    async def resolve_persona(**kwargs):
        return (
            "test-persona",
            {
                "prompt": "You are a test assistant. Persona: OFFLINE_PERSONA_MARKER.",
                "tools": None,
                "skills": [],
            },
            [],
            False,
        )

    async def get_conv(*args, **kwargs):
        return SimpleNamespace(
            cid="offline-conversation",
            persona_id="test-persona",
            history=json.dumps(history),
            token_usage=0,
        )

    async def current_id(*args):
        return "offline-conversation"

    async def update(umo, cid, **kwargs):
        history[:] = kwargs["history"]

    async def send(session, message):
        deliveries.append(str(session))
        return True

    cfg = {
        "plugin_set": ["*"],
        "admins_id": [],
        "kb_names": [],
        "provider_settings": {"computer_use_runtime": "none", "web_search": False},
    }
    context = Context.__new__(Context)
    context.__dict__.update(
        get_config=lambda **kwargs: cfg,
        get_using_provider=lambda **kwargs: provider,
        get_llm_tool_manager=lambda: ToolManager(),
        persona_manager=SimpleNamespace(resolve_selected_persona=resolve_persona),
        subagent_orchestrator=None,
        kb_manager=None,
        conversation_manager=SimpleNamespace(
            get_curr_conversation_id=current_id,
            get_conversation=get_conv,
            update_conversation=update,
        ),
        send_message=send,
    )
    state = HeartbeatState(
        sessions={
            umo: SessionState(
                sender_id="test-user", platform_type="aiocqhttp", last_user_at=time.time() - 20000
            )
        }
    )
    settings = Settings(active_hours_start="00:00", active_hours_end="00:00")

    async def save():
        pass

    async def start():
        steps.append("reserve_wake")

    async def hook(event, event_type, *args, **kwargs):
        steps.append(event_type.name)
        assert event.get_sender_id() == "test-user"
        if event_type.name == "OnLLMRequestEvent":
            args[0].system_prompt += "\nOFFLINE_MEMORY_MARKER"
        return False

    agent = FullAgent(context, state, lambda: settings, save)
    with (
        patch("astrbot.core.pipeline.context_utils.call_event_hook", side_effect=hook),
        patch("astrbot.core.astr_agent_hooks.call_event_hook", side_effect=hook),
    ):
        state.sessions[umo].quiet_until = time.time() + 3600
        result = await agent.run(umo, "Check an existing task; contact is optional.", start)
        assert result.outcome == "completed", result
        assert state.sessions[umo].notes["fixture-task"]["status"] == "done"
        assert not deliveries, "Quiet mode must allow silent work but block sends"
        state.sessions[umo].quiet_until = 0
        result = await agent.run(umo, "Share a relevant result if appropriate.", start)
        assert result.outcome == "completed", result
        assert len(deliveries) == 1, deliveries
        assert state.sessions[umo].unanswered == 1
        assert len(history) == 4
        assert "OnAgentBeginEvent" in steps and "OnAgentDoneEvent" in steps
        assert "OnUsingLLMToolEvent" in steps and "OnLLMToolRespondEvent" in steps
        assert steps.count("OnLLMRequestEvent") == 2
        assert provider.calls == 4, provider.calls
    print(
        json.dumps(
            {
                "real_framework": "builder + runner + tool executor",
                "offline_provider_calls": provider.calls,
                "real_network_calls": 0,
                "quiet_task_completed": True,
                "fixture_deliveries": len(deliveries),
                "request_hooks": steps.count("OnLLMRequestEvent"),
                "agent_begin_hooks": steps.count("OnAgentBeginEvent"),
                "agent_done_hooks": steps.count("OnAgentDoneEvent"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "heartbeat_plugin_candidate", root / "main.py", submodule_search_locations=[str(root)]
    )
    candidate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = candidate
    spec.loader.exec_module(candidate)
    with patch.object(
        candidate.StarTools,
        "get_data_dir",
        return_value=Path(os.environ["ASTRBOT_ROOT"]) / "plugin-data",
    ):
        plugin = candidate.HeartbeatPlugin(context, {})
        await plugin.initialize()
        await plugin.terminate()
    print("Native plugin import, decorators, initialization and termination: OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    source = args.source.resolve()
    if not (source / "astrbot" / "core" / "astr_main_agent.py").is_file():
        parser.error("--source must contain astrbot/core/astr_main_agent.py")
    sys.path[:0] = [str(source), str(Path(__file__).resolve().parents[1])]
    if args.worker_root:
        os.environ["ASTRBOT_ROOT"] = str(args.worker_root)
        asyncio.run(verify())
        return
    # The imported host owns database pools/background threads. Let the worker
    # exit before cleaning its isolated data directory (also works on Windows).
    with tempfile.TemporaryDirectory(prefix="heartbeat-framework-") as tmp:
        assert Path(tmp).resolve().parent == Path(tempfile.gettempdir()).resolve()
        subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                "-B",
                __file__,
                "--source",
                str(source),
                "--worker-root",
                tmp,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
