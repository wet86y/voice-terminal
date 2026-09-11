# LLM JSON Schema migration runbook

## LLM provider profiles

The default real-conversation backend is the official DeepSeek endpoint using
the public model ID `deepseek-flash`, `https://api.deepseek.com`, and the local
secret `DEEPSEEK_API_KEY`. The existing Responses/strict JSON Schema request
path and the V0.6 WebSocket protocol are unchanged.

Use OpenCode Go for debugging without editing YAML:

```powershell
./scripts/start.ps1 -LlmProvider opencode_go
```

The debug profile uses `https://opencode.ai/zen/go/v1`, the same public model
ID `deepseek-flash`, and `OPENCODE_GO_API_KEY`. For direct Python commands,
set `$env:VOICE_LLM_PROVIDER = "opencode_go"` before starting the process.

OpenCode Go requires a client-owned User-Agent and a stable
`x-opencode-session` header. The runtime binds that header to the current
`conversation_id`; `session.reset` creates a new upstream session identity.
The header is provider metadata only and is not added to public event payloads.

The compatibility switch is `llm.provider`, with the process-level
`VOICE_LLM_PROVIDER` override taking precedence. Provider fallback is explicit
rather than automatic, so one user turn is never sent to both backends.

## Current status: production Responses and thinking modes

The active P1 reliability issue is tracked in
[`known-issues/llm-invalid-json-responses.md`](known-issues/llm-invalid-json-responses.md).
It distinguishes the current non-empty invalid Responses content from the
historical Chat Completions whitespace storm and records the live-session
failure rate, recovery gaps, and diagnostic acceptance criteria.

The protocol is in production: every normal LLM decision uses Responses/strict
JSON Schema, and Chat/JSON Object is frozen as explicit rollback-only code.
Thinking is also part of the production Agent protocol. `fast` sends
`reasoning.effort=none` and exposes no thinking action; `balanced` sends an
initial `none` request and may issue one local `request_thinking`-controlled
`low` continuation; `deep` sends `low` directly. `request_thinking` is a
local control action, not a provider-native tool and not an external side
effect.

Production changes remain limited to reproducible defects in request
projection, provider schema compatibility, SSE terminal handling, local
envelope/action validation, thinking cancellation, bounded recovery, prefix
append/replacement, and diagnostics that do not retain generated text. Do not
weaken local validation or silently alter the explicit rollback path.

The frozen rollback lane is `chat_completions` and is described by
`llm-protocol-baseline.json`. The production lane is Responses/strict JSON
Schema. The Chat adapter remains installed but normal requests never call it,
ordinary protocol traffic is not dual-run, and restore happens only through
the explicit transport rollback switch.

The optional shadow runner remains a non-production diagnostic facility. It
does not enable or gate the production thinking path. Its thinking-action
validation still expects the historical `{"effort":"low"}` arguments shape,
whereas the production strict Schema and main Agent require
`{"request":true}`; therefore shadow thinking must remain disabled until that
separate diagnostic contract is aligned and retested. If used for non-thinking
diagnostics, it stores only normalized metadata in the bounded Agent snapshot
and server log: validity, state, action names, character counts, latency, usage
and error classification. Model output and reasoning text remain transient.

Protocol recovery is deliberately bounded and remains inside the single Agent
400 ms scheduler. A Responses decision may use one immediate correction for
blank output, an invalid envelope/action, or `response.incomplete` caused by
`max_output_tokens`. The immediate request receives a short error-specific
system instruction. If that correction also fails, the runtime publishes a
deterministic recoverable error, settles a finalized turn with no active
speech, and returns to `WAITING_EVENT`. Recovery exhaustion never creates an
independent retry/watchdog loop: deferred feedback is supplied only when a
later ordinary dispatch or an eligible staircase forced dispatch already has
a reason to run. Forced dispatch uses the same provider stream path and is
still subject to ordinary-event priority, active-request/TTS/user-input gates,
and the one-shot final-silence boundary. This keeps the recovery budget finite
without recreating the historical 2.5 Hz "slow storm".

`response.incomplete/content_filter`, `response.failed`, and a stream missing
its terminal event are provider/policy failures rather than model-format
errors. They do not consume the protocol correction budget or ask the model to
repair itself. Transient HTTP retries use a separate bounded budget.

The 2026-09-10 live capability probe accepted the three production shapes
(`no_actions`, `tools`, and `tools_and_thinking`) with HTTP 200/completed. The
provider requires every declared object property in `required` and still
rejects the diagnostic `oneOf` speech union. At the Responses boundary,
wait/cancel therefore use an empty `speak.text`, and heterogeneous actions
carry neutral filler fields; the adapter removes those fields before the
existing local validator/action registry sees the envelope.

## Production quality gate and rollback

The active production setting is `llm.transport: responses`. The following
are continuing release-safety and rollback criteria; they do not redefine the
already active production state. At least 300 direct Responses decisions over
three usage days should meet all of these conditions:

- local envelope validity is at least 99.5%, with no schema/local disagreement
  in the last 100 decisions;
- no blank-only completed response and no recovery chain beyond one attempt;
- labelled speech-state and action-contract cases are all correct;
- p95 first safe speech text regresses by no more than 200 ms;
- at least 50 representative decisions pass manual semantic review.

Rollback is an atomic config change to `llm.transport: chat_completions`; the
frozen adapter and local envelope validator remain installed.

## Production thinking behavior

The production thinking path is bounded by the Agent protocol, rather than by
shadow execution. In `balanced` mode the model may return exactly the local
shape `{"name":"request_thinking","arguments":{"request":true}}`; the
runtime then schedules one `effort=low` continuation for that turn. If the
same envelope starts business tools, the continuation may wait up to two
seconds for those tools and otherwise receives only their pending state. Tool
results subsequently return through the normal 400 ms scheduler lane.

New user input, stop, reset, supersession, or the applicable cancellation
boundary invalidates pending thinking work. Reasoning text remains ephemeral
and is only available through the controlled local debug UI. Shadow
flags (`shadow_enabled` and `shadow_thinking_enabled`) remain diagnostic-only;
they are not prerequisites for production `balanced` or `deep` behavior, and
shadow thinking remains disabled until its historical arguments shape is
aligned with the production Schema.

The current code freezes thinking-action visibility at episode creation from
the selected reasoning mode. Therefore `llm.thinking_action_enabled` must not
be documented as the production activation switch until the implementation is
changed to make it authoritative; `reasoning_mode` is the current source of
truth for the normal Agent schema.
