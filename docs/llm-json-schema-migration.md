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

## Current status: post-switch bug-repair period

The protocol switch is active in the test environment: every normal LLM
decision uses Responses/strict JSON Schema with base reasoning effort `none`.
Chat/JSON Object is frozen and hidden as rollback-only code. Ordinary shadow
dual-running and both thinking flags are disabled.

During this period, changes must be limited to reproducible defects in request
projection, provider schema compatibility, SSE terminal handling, local
envelope/action validation, bounded recovery, prefix append/replacement, and
diagnostics that do not retain generated text. Do not enable adaptive thinking,
remove the rollback adapter, weaken local validation, or describe the switch as
release-complete.

The frozen rollback lane is `chat_completions` and is described by
`llm-protocol-baseline.json`. During the current test phase the only active
lane is Responses/strict JSON Schema. The Chat adapter remains installed but
hidden: normal requests never call it, and ordinary protocol traffic is not
dual-run. Restore it only through the explicit transport rollback switch.

The optional shadow runner is retained only for the later thinking experiment.
It stores normalized metadata in the bounded Agent snapshot and server log:
validity, state, action names, character counts, latency, usage and error
classification. Model output and reasoning text remain transient.

Protocol recovery is deliberately small and event-driven. A Responses decision
may use one immediate correction for blank output, an invalid envelope/action,
or `response.incomplete` caused by `max_output_tokens`. The immediate request
receives a short error-specific system instruction. If that correction also
fails, the runtime publishes a deterministic recoverable error, settles a
finalized turn with no active speech, and returns to `WAITING_EVENT`. Recovery
exhaustion never inserts another scheduler event: only a later user,
refinement, endpoint, or tool event may re-enter the ordinary 400 ms lane, at
which point the deferred problem code is supplied once. This prevents a 2.5 Hz
"slow storm" while keeping continuous conversation available.

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

## Test acceptance and release gate

The test environment already uses `llm.transport: responses`. Do not treat it
as a release-approved production setting until the fixed test suite passes and
at least 300 direct Responses decisions over three usage days meet all of these
conditions:

- local envelope validity is at least 99.5%, with no schema/local disagreement
  in the last 100 decisions;
- no blank-only completed response and no recovery chain beyond one attempt;
- labelled speech-state and action-contract cases are all correct;
- p95 first safe speech text regresses by no more than 200 ms;
- at least 50 representative decisions pass manual semantic review.

Rollback is an atomic config change to `llm.transport: chat_completions`; the
frozen adapter and local envelope validator remain installed.

## Thinking experiment

After the direct Responses test path is stable, enable `llm.shadow_enabled`
and `llm.shadow_thinking_enabled` to
let the shadow model request `request_thinking(effort=low)`. It may renew that
one-shot lease repeatedly; stop/reset, a new user turn, or disabling the flag
ends the chain. Business actions are not duplicated in shadow. A combined
thinking/tool request is recorded as a blocked barrier and must be exercised
with deterministic tool fixtures before any user-facing experiment.

`llm.thinking_action_enabled` enables the same state machine in the main Agent
for isolated testing. It is false by default and must remain false until a
separate iteration defines token, wall-clock and repetition limits.

## Exit from bug-repair period

Exit requires the acceptance metrics above, no unresolved P0/P1 protocol bug,
all automated tests green, a completed real-device conversation pass covering
tools/cancel/wait/prefix append/replacement, and an explicit documentation
change declaring the protocol stable. Exiting this period does not implicitly
enable the thinking experiment.
