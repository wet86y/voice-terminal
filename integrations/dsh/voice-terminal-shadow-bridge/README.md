# Voice Terminal Shadow Bridge

Project-owned DSH bundle for the formal Voice Terminal Shadow root Agent. It is
the only supported integration boundary between Voice Terminal and DSH.

The bundle exposes an authenticated loopback HTTP protocol, owns the mapping to
formal root Agents, injects Voice-authoritative context, registers root-scoped
Voice control tools, and projects committed DSH activity into bounded observer
snapshots. Each accepted activation also receives a non-waking convergence
advisory at the configured runtime reminder interval (20 seconds by default);
unclaimed reminders are replaced in place and are consumed only at a DSH
next-step boundary. It never transports audio, model credentials, browser
launch tokens, or ordinary assistant prose to the Voice mailbox.

The runtime requires `VOICE_DSH_BRIDGE_TOKEN` and `VOICE_DSH_CALLBACK_ORIGIN`.
Both are supplied only to the project-managed DSH child process.
