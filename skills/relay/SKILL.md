---
name: relay
description: Continue eligible long-running Codex CLI Goals in a verified fresh thread at automatic compaction, while failing open to native compaction.
---

# Relay

Relay is an experimental Codex CLI plugin that replaces automatic Goal
compaction at `PreCompact(auto)` with a verified fresh continuation when
possible; otherwise native compaction proceeds normally.

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

If any required step fails, Relay returns `continue: true` and Codex CLI
compacts normally. Manual `/compact`, inactive/terminal Goals, malformed input,
missing settings, open circuit breakers, unsupported roots, and other failures
are fail-open paths.

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

## CLI contract

User-originated roots are admitted only when their persisted session source is
`cli` (interactive Codex CLI) or `exec` (`codex exec`). Any other or missing
source fails open without creating Relay state or a destination.

Relay-owned successors are admitted through their durable Relay parent record,
so their next handoff does not depend on the source label attached to an
internally created thread.

Relay uses the local `codex app-server --stdio` protocol internally to inspect
Goals, create fresh threads, restore settings, and verify continuation turns.
That implementation transport is not a separate supported user-facing mode.

## Compatibility and state

Relay requires Python 3.10 or newer. The hook checks the interpreter and emits
an actionable diagnostic before failing open when no supported interpreter is
available; set `RELAY_PYTHON` when `python3` is not the intended executable.

The v0.7.0 release checks use Codex CLI 0.150.1 for clean installation and
hook-contract validation. Other Codex versions are unverified.

Relay stores bounded local handoff metadata and worker outcomes under
`.omx/state/relay/`; it does not store full transcripts. The directory is
ignored by Git, is not automatically expired, and should be treated as
sensitive. Stop Relay workers before moving that directory to the operating
system's Trash when you want to delete the local record.

## Verify

```bash
python3 skills/relay/scripts/test_relay.py
bash validate.sh
```

Use `python3 skills/relay/scripts/smoke_codex_app_transport.py` for the
authenticated, isolated-home A → B → C smoke. It defaults to
`gpt-5.6-luna`; set `RELAY_SMOKE_MODEL` to a model available to your account.
Review and trust installed hook definitions; plugin installation alone does
not authorize them.
