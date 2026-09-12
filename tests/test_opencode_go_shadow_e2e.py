"""Acceptance loop for the OpenCode Go main-agent/shadow-agent path.

This is deliberately a local transport test.  Both fake providers carry the
same provider metadata and request shape as the OpenCode Go Responses path,
but no API key or network request is used.  The test drives the public voice
contracts (AgentEpisodeRuntime.observe and task action JSON); it never calls a
ShadowTaskManager private method.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.options import AgentOptions
from voice_terminal.shadow_agent.contracts import task_action_definition
from voice_terminal.core.events import utc_now_iso
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.shadow_agent.component import ShadowAgentComponent
from voice_terminal.shadow_agent.contracts import ShadowTaskCommand, ShadowTaskEvent
from voice_terminal.shadow_agent.provider import ShadowProviderResponse
from voice_terminal.shadow_agent.tools import ShadowToolExecutor


def _decision(text: str = "", *, state: str = "wait", actions: list[dict] | None = None) -> str:
    return json.dumps({
        "speak": {"state": state, "emotion": "calm", "text": text if state == "speak" else ""},
        "actions": actions or [],
    }, ensure_ascii=False, separators=(",", ":"))


def _task(identifier: int, state: str, text: str = "", question_id: str = "") -> dict:
    return {"name": "task", "arguments": {
        "id": identifier, "state": state, "text": text, "question_id": question_id,
    }}


def _call(call_id: str, name: str, arguments: dict) -> ShadowProviderResponse:
    return ShadowProviderResponse(({
        "type": "function_call", "status": "completed", "call_id": call_id,
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    },), {})


def _message(text: str) -> ShadowProviderResponse:
    return ShadowProviderResponse(({
        "type": "message", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    },), {})


class _LocalTtsOutput:
    """Small local TTS/output adapter exercising speech.completed delivery."""

    status = "IDLE"

    def __init__(self) -> None:
        self.runtime = None
        self.handle = None
        self.events: list[str] = []
        self.last_completed_speech = None

    @property
    def active(self) -> bool:
        return self.handle is not None and not self.handle.finished

    @property
    def speak_snapshot(self) -> dict:
        if not self.active or self.handle is None:
            return {"queue_status": "empty", "text_only_queue": True}
        return {
            "queue_status": "speaking", "text_only_queue": True,
            "conversation_id": self.handle.identity[0], "trace_id": self.handle.identity[1],
            "turn_id": self.handle.turn_id, "speech_id": self.handle.speech_id,
            "output_generation": 1, "text": self.handle.text,
        }

    async def begin(self, identity, turn_id, *, emotion="calm"):
        self.events.append("TTS_STARTED")
        self.handle = _LocalSpeechHandle(self, identity, turn_id, emotion)
        return self.handle

    async def replace_current(self, identity, turn_id, *, emotion="calm"):
        self.events.append("TTS_REPLACED")
        if self.handle is not None and not self.handle.finished:
            self.handle.finished = True
        return await self.begin(identity, turn_id, emotion=emotion)

    def cancel_all(self, reason="cancelled"):
        self.events.append("TTS_CANCELLED")
        if self.handle is not None:
            self.handle.finished = True

    async def close(self):
        self.cancel_all("closed")


class _LocalSpeechHandle:
    def __init__(self, owner, identity, turn_id, emotion):
        self.owner = owner
        self.identity = (identity[0], identity[1])
        self.turn_id = turn_id
        self.speech_id = uuid4().hex
        self.emotion_requested = emotion
        self.text = ""
        self.finished = False
        self.workspace = SimpleNamespace(timings={})

    async def feed(self, text: str):
        if self.finished:
            return
        self.text += text
        self.owner.events.append("TTS_SEGMENT")

    async def finish(self):
        if self.finished:
            return
        self.finished = True
        self.owner.events.append("TTS_COMPLETED")
        record = {
            "speech_id": self.speech_id,
            "conversation_id": self.identity[0], "episode_id": self.identity[0],
            "request_id": self.identity[1], "trace_id": self.identity[1],
            "turn_id": self.turn_id, "status": "completed", "kind": "formal",
            "generation_id": 1, "text": self.text,
            "delivery": {"status": "full", "termination": "completed",
                          "precision": "complete", "heard_text": self.text,
                          "uncertain_text": "", "unheard_text": ""},
            "delivery_status": "full", "heard_text": self.text,
            "output_generation": 1, "completed_at": utc_now_iso(),
        }
        self.owner.last_completed_speech = record
        if self.owner.runtime is not None:
            await self.owner.runtime.output_completed(record)


class _RecordingMainOpenCodeGo:
    """OpenCode Go Responses-shaped local recording transport for main Agent."""

    provider = "opencode_go"
    base_url = "https://opencode.ai/zen/go/v1"
    model = "deepseek-flash"
    transport = "responses"
    supports_decision_schema = True
    protocol_recovery_limit = 1

    def __init__(self):
        self.calls: list[dict] = []
        self.question_ids: dict[int, str] = {}
        self.question_status: dict[int, str] = {}
        self.announced: set[int] = set()
        self.request_submitted: set[int] = set()
        self.stale_emitted = False
        self.decisions: list[tuple[str, str]] = []
        self.segment_delay = True

    def observe_shadow_event(self, event: ShadowTaskEvent):
        if event.question_id:
            self.question_ids[event.id] = event.question_id
        self.question_status[event.id] = event.status

    @staticmethod
    def _latest_user(messages) -> str:
        values = [item.get("content", "") for item in messages
                  if isinstance(item, dict) and item.get("role") == "user"]
        return str(values[-1]) if values else ""

    def _choose(self, messages) -> str:
        latest = self._latest_user(messages)
        actions: list[dict] = []
        speak = ""
        if ("旧问题" in latest and 1 in self.question_ids
                and self.question_status.get(1) in {"awaiting_user", "paused"}
                and not self.stale_emitted):
            # Deliberately stale; the public dispatcher must reject it.  Keep
            # this one-shot branch ahead of clarification narration so a
            # queued event cannot consume the stale-answer acceptance turn.
            self.stale_emitted = True
            actions = [_task(1, "reply", "我选北京", "task-1-question-stale")]
            speak = "我先核对一下刚才的问题。"
        # Event-driven clarification has priority over re-interpreting the
        # original request still present as the latest user message.
        elif any(status == "awaiting_user" and identifier not in self.announced
               for identifier, status in self.question_status.items()):
            identifier = next(identifier for identifier, status in self.question_status.items()
                              if status == "awaiting_user" and identifier not in self.announced)
            self.announced.add(identifier)
            speak = {1: "影子任务需要确认城市：请选择北京或上海。",
                     3: "影子任务需要确认出行日期：请选择周六或周日。",
                     4: "影子任务需要确认提醒时间：请选择上午或下午。"}.get(
                         identifier, "影子任务需要一个确认，请告诉我你的选择。")
        elif ("同时整理项目资料并规划周末行程" in latest
              and 2 not in self.question_status and 3 not in self.question_status):
            self.request_submitted.update({2, 3})
            actions = [_task(2, "request", "整理项目资料"),
                       _task(3, "request", "规划周末行程")]
            speak = "好的，我同时安排这两项工作。"
        elif ("请查北京天气并持续跟进" in latest
              and 1 not in self.question_status and 1 not in self.request_submitted):
            self.request_submitted.add(1)
            actions = [_task(1, "request", "查北京天气并持续跟进")]
            speak = "好的，我先去核实北京天气。"
        elif ("我选北京" in latest and 1 in self.question_ids
              and self.question_status.get(1) in {"awaiting_user", "paused"}):
            actions = [_task(1, "reply", latest, self.question_ids[1])]
            speak = "收到，我按北京继续。"
        elif "我选择周六" in latest or "深圳" in latest:
            if (3 in self.question_ids
                    and self.question_status.get(3) in {"awaiting_user", "paused"}):
                actions = [_task(3, "reply", latest, self.question_ids[3])]
                speak = "收到，我按这个安排继续规划。"
        elif ("创建一个等待中的提醒" in latest
              and 4 not in self.question_status and 4 not in self.request_submitted):
            self.request_submitted.add(4)
            actions = [_task(4, "request", "创建一个等待中的提醒")]
            speak = "好的，我开始准备这个提醒。"
        elif ("修改提醒为周一" in latest
              and self.question_status.get(4) in {"awaiting_user", "paused"}):
            actions = [_task(4, "modify", "把提醒改为下周一", "")]
            speak = "好的，提醒已改为下周一。"
        elif ("启动一个可取消任务" in latest
              and 5 not in self.question_status and 5 not in self.request_submitted):
            self.request_submitted.add(5)
            actions = [_task(5, "request", "启动一个可取消任务")]
            speak = "好的，我启动这项任务。"
        elif "取消可取消任务" in latest and self.question_status.get(5, "running") == "running":
            actions = [_task(5, "cancel", "", "")]
            speak = "好的，已取消这项任务。"
        elif "reset后再次查询" in latest:
            actions = [_task(1, "request", "reset后再次查询本地资料")]
            speak = "会话已重新开始，我来查询。"
        elif "查询本地资料" in latest:
            actions = [_task(1, "request", "查询本地资料")]
            speak = "好的，我来查询本地资料。"
        # A paused task is intentionally quiet on unrelated turns.
        elif any(status == "paused" for status in self.question_status.values()):
            # Paused is intentionally quiet on unrelated turns.
            return _decision()
        else:
            speak = "好的，我记下了。"
        return _decision(speak, state="speak", actions=actions)

    def stream_events(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        output = self._choose(messages)
        self.decisions.append((self._latest_user(messages), output))
        delay = self.segment_delay
        self.segment_delay = False

        async def stream():
            if delay:
                await asyncio.sleep(0.01)
            midpoint = max(1, len(output) // 2)
            yield {"type": "content", "text": output[:midpoint]}
            yield {"type": "content", "text": output[midpoint:]}
            yield {"type": "usage", "usage": {"prompt_tokens": 20, "completion_tokens": len(output)}}
            yield {"type": "finish", "reason": "completed",
                   "stream_metrics": {"sse_event_count": 4, "sse_malformed_count": 0}}

        return stream()


class _RecordingShadowOpenCodeGo:
    """Scripted shadow provider using the public ``complete`` protocol."""

    provider = "opencode_go"
    base_url = "https://opencode.ai/zen/go/v1"
    model = "deepseek-flash"
    calls: list[dict]

    def __init__(self):
        self.calls = []
        self.per_goal_calls: dict[str, int] = {}
        self.cancel_started = asyncio.Event()

    @staticmethod
    def _goal(items) -> str:
        for item in items:
            if isinstance(item, dict) and "【影子任务目标】" in str(item.get("content", "")):
                return str(item["content"]).split("【影子任务目标】", 1)[1].strip()
        return ""

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        goal = self._goal(kwargs.get("input_items", ()))
        count = self.per_goal_calls.get(goal, 0) + 1
        self.per_goal_calls[goal] = count
        if "北京天气" in goal and count == 1:
            return _call("clarify-1", "request_user_clarification",
                         {"question": "请确认城市", "options": ["北京", "上海"]})
        if "北京天气" in goal:
            return _message('{"completed":true,"text":"北京天气任务已根据你的选择完成。"}')
        if "项目资料" in goal and count == 1:
            return ShadowProviderResponse((
                {"type": "function_call", "status": "completed", "call_id": "file-1",
                 "name": "files_search", "arguments": '{"query":"项目"}'},
                {"type": "function_call", "status": "completed", "call_id": "file-2",
                 "name": "files_search", "arguments": '{"query":"资料"}'},
            ), {})
        if "项目资料" in goal:
            return _message('{"completed":true,"text":"项目资料已整理完成。"}')
        if "周末行程" in goal and count == 1:
            return _call("clarify-3", "request_user_clarification",
                         {"question": "请确认出行日期", "options": ["周六", "周日"]})
        if "周末行程" in goal and count == 2:
            return _call("file-3", "files_search", {"query": "深圳周末活动"})
        if "周末行程" in goal:
            return _message('{"completed":true,"text":"周末行程已结合本地资料完成。"}')
        if "等待中的提醒" in goal and count == 1:
            return _call("clarify-4", "request_user_clarification",
                         {"question": "请确认提醒时间", "options": ["上午", "下午"]})
        if "等待中的提醒" in goal:
            return _message('{"completed":true,"text":"提醒已按新安排完成。"}')
        if "可取消任务" in goal:
            self.cancel_started.set()
            await asyncio.Event().wait()
        return _message('{"completed":true,"text":"本地查询任务完成。"}')


class _LocalShadowTools:
    """Read-only local fake external tool, preserving call/output order."""

    available = True
    service_status = "READY"

    def __init__(self):
        self.calls: list[dict] = []

    def responses_tools(self):
        return [{"type": "function", "name": "files_search", "description": "local fake search",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                 "required": ["query"], "additionalProperties": False}}]

    # ShadowToolExecutor uses the backend-neutral catalog name while the
    # provider adapter consumes the Responses-shaped catalog above.
    def action_definitions(self):
        # ShadowToolExecutor expects the backend-neutral FileTools contract;
        # its own adapter adds the Responses ``type=function`` wrapper.
        return [{key: value for key, value in definition.items() if key != "type"}
                for definition in self.responses_tools()]

    def normalize(self, name, arguments):
        return name, arguments

    def execute(self, name, arguments):
        self.calls.append({"name": name, "arguments": arguments})
        return {"source": "local-fake", "query": arguments.get("query", ""),
                "result": "本地资料片段"}

    async def execute_calls(self, calls):
        self.calls.extend(calls)
        return [{"type": "function_call_output", "call_id": call["call_id"],
                 "output": json.dumps({"source": "local-fake", "query": json.loads(call["arguments"])["query"],
                                        "result": "本地资料片段"}, ensure_ascii=False)} for call in calls]

    def close(self):
        pass


class _Registry:
    def begin_turn(self, _turn):
        pass

    def cancel_turn(self, _turn):
        pass

    def action_definitions(self):
        return [task_action_definition()]


async def _settle(runtime: AgentEpisodeRuntime, session, *, wait_for_shadow: bool = True):
    """Wait for the single scheduler and relay to become idle."""
    stable = 0
    for _ in range(500):
        await asyncio.sleep(0.003)
        # The real loop is heartbeat driven.  The acceptance harness nudges
        # the same production heartbeat once when a coalesced mailbox event
        # is observed, avoiding a test-only wall-clock dependency while still
        # exercising _on_heartbeat/_start_dispatch/_step.
        if runtime.active_request is None and runtime.pending:
            await runtime._on_heartbeat(max(time.monotonic(), runtime.scheduler.next_heartbeat or 0) + 0.001)
        shadow_running = any(item["status"] == "running"
                             for item in (session.production.snapshot(i) for i in range(1, 8))
                             if item is not None)
        quiet = (runtime.active_request is None and not runtime.pending and
                 not runtime.recovery_queue and not runtime._background_tools and
                 (not wait_for_shadow or not shadow_running))
        stable = stable + 1 if quiet else 0
        if stable >= 4:
            return
    snapshots = {i: session.production.snapshot(i) for i in range(1, 8)}
    raise AssertionError(
        f"scheduler/agent did not settle: state={runtime.state!r}, "
        f"active={runtime.active_request!r}, pending={runtime.pending!r}, "
        f"recovery={runtime.recovery_queue!r}, snapshots={snapshots!r}"
    )


async def _voice_turn(runtime, turn_id: str, partial: str, final: str, *, refined: str | None = None):
    await runtime.observe("USER_SEGMENT_UPDATED", turn_id=turn_id, text=partial,
                          provisional_transcript=partial)
    await asyncio.sleep(0.004)
    await runtime.observe("USER_INPUT_FINALIZED", turn_id=turn_id, text=final,
                          input_finished_at=asyncio.get_running_loop().time())
    if refined is not None:
        await runtime.observe("USER_INPUT_REFINED", turn_id=turn_id, text=refined,
                              sentences=[{"sentence_id": 1, "text": partial, "revision": 1}])


def test_opencode_go_shadow_agent_20_plus_round_acceptance_loop():
    async def scenario():
        main = _RecordingMainOpenCodeGo()
        shadow_provider = _RecordingShadowOpenCodeGo()
        tools = _LocalShadowTools()
        component = ShadowAgentComponent(shadow_provider, ShadowToolExecutor(tools),
                                         instructions_loader=lambda: "local acceptance rules")
        conversation = ConversationRuntime()
        ledger = SessionLedger()
        output = _LocalTtsOutput()
        emitted: list[tuple[str, dict]] = []
        shadow_events: list[ShadowTaskEvent] = []
        runtime = None

        async def emit(kind, payload):
            emitted.append((kind, payload))

        async def sink(events):
            shadow_events.extend(events)
            for item in events:
                main.observe_shadow_event(item)
                await runtime.accept_shadow_task_event(item)

        session = component.new_session(production_sink=sink)
        # Keep the production manager's two-minute user wait semantics, but use a
        # deterministic shortened clock window for this local acceptance run.
        session.production.user_wait_seconds = 0.05

        async def dispatch(command: ShadowTaskCommand, history):
            return await session.handle_voice(command, history)

        runtime = AgentEpisodeRuntime(
            main, _Registry(), output, emit, conversation=conversation, ledger=ledger,
            options=AgentOptions(enabled=True, scheduler_tick_ms=2),
            task_dispatcher=dispatch,
        )
        output.runtime = runtime

        assert main.base_url == shadow_provider.base_url == "https://opencode.ai/zen/go/v1"
        assert main.provider == shadow_provider.provider == "opencode_go"

        # Rounds 1-5: truncated/streaming ASR, final/refinement, stale then
        # correct natural-language answer to shadow clarification.
        await _voice_turn(runtime, "turn-01", "请查北京天", "请查北京天气并持续跟进",
                          refined="请查北京天气并持续跟进")
        await _settle(runtime, session, wait_for_shadow=False)
        await _voice_turn(runtime, "turn-02", "请把刚才的任务记住", "请把刚才的任务记住，不用现在回答")
        await _settle(runtime, session, wait_for_shadow=False)
        for _ in range(100):
            if 1 in main.announced and 1 in main.question_ids:
                break
            await asyncio.sleep(0.003)
        assert 1 in main.announced and 1 in main.question_ids
        await _voice_turn(runtime, "turn-03", "我先回答旧问题", "我先回答旧问题")
        await _settle(runtime, session, wait_for_shadow=False)
        await _voice_turn(runtime, "turn-04", "我选北", "我选北京")
        await _settle(runtime, session)
        await _voice_turn(runtime, "turn-05", "补充一下", "补充一下今天的安排")
        await _settle(runtime, session, wait_for_shadow=False)

        # Rounds 6-8: one main decision fans out to two task requests; task 3
        # clarifies, then the natural-language answer becomes task(reply).
        await _voice_turn(runtime, "turn-06", "同时整理项目", "同时整理项目资料并规划周末行程")
        await _settle(runtime, session, wait_for_shadow=False)
        await _voice_turn(runtime, "turn-07", "我选择周六", "我选择周六")
        await _settle(runtime, session)
        await _voice_turn(runtime, "turn-08", "我还想去深圳", "我还想去深圳")
        await _settle(runtime, session)

        # Rounds 9-11: paused (shortened two-minute wait), modify while
        # paused, and a quiet unrelated turn that must not wake it repeatedly.
        await _voice_turn(runtime, "turn-09", "创建一个等待中", "创建一个等待中的提醒")
        await _settle(runtime, session, wait_for_shadow=False)
        await asyncio.sleep(0.06)
        for _ in range(100):
            snapshot = session.production.snapshot(4)
            if snapshot is not None and snapshot["status"] == "paused":
                break
            await asyncio.sleep(0.003)
        assert session.production.snapshot(4)["status"] == "paused"
        await _settle(runtime, session, wait_for_shadow=False)
        await _voice_turn(runtime, "turn-10", "普通闲聊", "普通闲聊，不要重复追问")
        await _settle(runtime, session, wait_for_shadow=False)
        await _voice_turn(runtime, "turn-11", "修改提醒", "修改提醒为周一")
        await _settle(runtime, session)

        # Rounds 12-13: cancel an in-flight shadow task through task(cancel).
        await _voice_turn(runtime, "turn-12", "启动一个可取消", "启动一个可取消任务")
        await _settle(runtime, session, wait_for_shadow=False)
        for _ in range(100):
            if shadow_provider.cancel_started.is_set():
                break
            await asyncio.sleep(0.003)
        assert shadow_provider.cancel_started.is_set()
        await _voice_turn(runtime, "turn-13", "取消可取消", "取消可取消任务")
        await _settle(runtime, session)

        # Reset/start boundary: reset the session manager and the conversation
        # identity, then continue ten ordinary scheduler rounds (14-23).
        await runtime.end("reset")
        await session.reset()
        conversation.reset()
        runtime.closed = False
        main.question_ids.clear()
        main.question_status.clear()
        main.announced.clear()
        main.request_submitted.clear()
        for index in range(14, 24):
            text = "reset后再次查询本地资料" if index == 14 else f"第{index}轮确认本地状态"
            await _voice_turn(runtime, f"turn-{index:02d}", text[:6], text)
            await _settle(runtime, session, wait_for_shadow=False)

        # Coverage assertions are semantic, not implementation-private calls.
        kinds = [payload.get("kind") for kind, payload in emitted if kind == "agent.trace"]
        assert len(main.calls) >= 20
        assert len({payload.get("request_id") for kind, payload in emitted
                    if kind == "agent.trace" and payload.get("kind") == "LLM_DISPATCHED"}) >= 20
        assert any(event.status == "awaiting_user" for event in shadow_events)
        assert any(event.status == "paused" for event in shadow_events)
        assert any(event.status == "completed" and event.id in {1, 2, 3, 4} for event in shadow_events)
        assert any(event.status == "cancelled" and event.id == 5 for event in shadow_events)
        assert any(
            kind == "agent.trace"
            and payload.get("kind") == "SHADOW_TASK_RECEIPT"
            and payload.get("accepted") is False
            for kind, payload in emitted
        )  # stale question_id rejected by the public task receipt
        assert any("USER_INPUT_REFINED" == kind for kind in kinds)
        assert any("USER_SEGMENT_UPDATED" == kind for kind in kinds)
        assert any("USER_INPUT_FINALIZED" == kind for kind in kinds)
        assert any("TTS_COMPLETED" == event for event in output.events)
        assert any(kind == "speech.completed" for kind, _payload in emitted)
        assert len(tools.calls) >= 3  # fan-out (2) plus mixed clarification/tool path
        assert any(call["kwargs"].get("schema", {}).get("properties", {}).get("actions")
                   for call in main.calls)

        await runtime.stop()
        await component.close()

    asyncio.run(scenario())
