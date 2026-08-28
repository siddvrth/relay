---
name: relay
description: Continue eligible long-running Codex Goals in a verified fresh thread at automatic compaction, while failing open to native compaction.
---

# Relay

Relay is a Codex-only experimental alternative to automatic Goal compaction.

## Automatic flow

For `PreCompact` with `trigger: auto`, Relay:

1. Reads the active source Goal through `thread/goal/get`.
2. Reads bounded current settings and recent request/progress from the supplied
   hook transcript, then treats live repository state as authoritative.
3. Creates a different durable thread with `thread/start`, never `thread/fork`.
4. Restores and verifies the Goal paused, starts the bounded continuation turn,
   observes `turn/started`, and reactivates/verifies the Goal.
5. Binds the exact destination thread/turn IDs to durable state and only then
   returns `continue: false` so Codex does not compact the predecessor.

If any required step fails, Relay returns `continue: true` and Codex compacts
normally. Manual `/compact`, inactive/terminal Goals, malformed input, missing
settings, open circuit breakers, and surfaces without required presentation
proof are fail-open paths.

`UserPromptSubmit` and `PreToolUse` never launch. They only guard an already
acknowledged predecessor; Goal control, cancellation, stop, and shutdown bypass
the guard. `SessionEnd` performs scoped worker cleanup.

## Continuation and state

The continuation contains the exact objective, current request when available,
recent progress, repository path, changed-file hint, and stable chain/sequence
identity. It does not copy a transcript or model hidden state. Each source uses
one atomic state record under `.omx/state/relay/` plus a worker outcome record.

Repeated no-progress or launch failures open Relay's circuit for that chain.
The Goal is not marked blocked; future automatic compaction remains native.

## Surface boundary

Relay has no supported way to present an externally created thread in the
already-running Desktop app, so ordinary app-server/Desktop sources fail open
before `thread/start`. CLI/exec and app-server-created threads are headless
paths; current Desktop (`source: vscode`) is not. This is not supported Desktop
presentation.

## Verify

```bash
python3 skills/relay/scripts/test_relay.py
bash validate.sh
```

Use `python3 skills/relay/scripts/smoke_codex_app_transport.py` for the
authenticated, isolated-home A → B → C smoke. Review and trust installed hook
definitions; plugin installation alone does not authorize them.
