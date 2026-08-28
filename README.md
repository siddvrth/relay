# Relay

Relay is an experimental fresh-thread alternative to automatic compaction for
long-running Codex Goals. When Codex reaches `PreCompact` with `trigger: auto`,
Relay attempts a genuinely fresh `thread/start` continuation. It stops the
source compaction only after the destination Goal is restored, the explicit
continuation turn is observed as started, and the destination IDs are bound to
durable Relay state. If any required step fails, Relay returns control and
Codex compacts normally.

Explicit manual `/compact` is never matched. It remains the user's same-thread
escape hatch.

Relay carries only bounded deterministic context: the exact Goal objective,
the current user request when present in the hook transcript, recent agent
progress, repository path, changed-file hint, and chain identity. The live
repository remains authoritative. Relay never calls `thread/fork` and does not
copy the transcript.

## Safety contract

- Only `PreCompact(auto)` can launch a successor.
- `UserPromptSubmit` and `PreToolUse` are guard-only hooks that quiesce an
  acknowledged predecessor; Goal control, cancel, stop, and shutdown escape.
- The destination Goal is restored paused, Relay starts and observes its
  bounded explicit turn, then reactivates the Goal. This avoids racing Codex's
  host-owned automatic Goal continuation.
- Model, personality, approval policy/reviewer, reasoning effort, summary,
  service tier, and the current named permission profile or sandbox projection
  are forwarded when exposed by the source turn context.
- Duplicate hooks serialize on one source lock. Stale running state is rejected,
  worker cleanup is process-group scoped, and repeated failures open Relay's
  circuit without blocking the Goal or native compaction.

## Desktop boundary

Current Codex app-server exposes no supported Desktop present/select/focus RPC,
and an already-running Desktop does not reliably reconcile threads created by a
separate app-server client. The current Desktop source is `vscode`, so Relay
treats `vscode` and unknown host sources as requiring presentation and fails
open before creating a destination.
It does not use deep links, UI automation, internal IPC, synthetic presenters,
or application restarts.

CLI/exec and explicitly app-server-created threads are headless paths. Other
host surfaces remain native-compaction-only until Codex exposes a supported
presentation acknowledgement.

## Install and verify

This checkout does not advertise a fabricated marketplace selector. Install
the package through the real marketplace that publishes it, then review and
trust the bundled hooks. Installing or enabling a plugin does not automatically
trust its hook definitions.

```bash
python3 skills/relay/scripts/test_relay.py
bash validate.sh
```

The authenticated isolated-home smoke proves a real A → B → C chain, real work
in B, Goal/settings preservation, worker cleanup, and duplicate suppression:

```bash
python3 skills/relay/scripts/smoke_codex_app_transport.py
```

Relay uses only Python's standard library and Bash. Its release policy remains
`experimental_non_claim`: it makes no token-efficiency, cost-savings, or
quality-superiority claim over native compaction.
