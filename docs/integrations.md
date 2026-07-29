# Codex Integrations

Relay supports Codex surfaces only. The Python runtime creates session-scoped capsules and internal orchestration results; each integration translates those results into capabilities allowed by its host.

## Codex App

For automatic continuous execution:

1. Obtain a ready internal result with exact capsule path/hash, session, revision, and one continuation prompt.
2. Persist `launching` state and start `codex app-server --stdio` with the user's normal environment and `CODEX_HOME`.
3. Perform `initialize` then `initialized`.
4. Call `thread/start` with the source repository's exact `cwd`; never copy source history.
5. Call `turn/start` with the returned thread ID and the single bounded prompt.
6. Persist both destination IDs, bind the transfer as delivered/started, and keep the worker alive through `turn/completed`.
7. Require destination verification and canonical exact acknowledgement; thread text is observation, never acknowledgement authority.
8. Record a cooperative stop result or durable read-only `termination_pending`; archive/visible closure and Desktop UI focus are separate optional outcomes.

Do not use `thread/fork` for production context shedding; a fork inherits completed conversation history. The source model does not invoke thread tools. If app-server launch or protocol handling fails, return the exact continuation data without claiming a task was created. Bounded deadlines stop stalled subprocesses; acknowledgement timeout is recorded as an unknown outcome and is not retried blindly. The detached transport has no approval UI, so it safely declines command/file approvals and grants no requested permission escalation.

Goal text can be preserved, but host-owned goal identity may not transfer. The clean task inspects current goal state before recreating or continuing the recorded objective.

## Codex Hooks

All command hooks receive JSON on stdin. Current [official hook documentation](https://learn.chatgpt.com/docs/hooks) documents `session_id` and `transcript_path` as common input, but warns that the transcript format is not stable.

Relay therefore treats additional ratio fields as compatibility telemetry only:

- compatible ratio at or above `0.30`: threshold path is eligible
- otherwise, `PreToolUse` reads a bounded transcript tail for the latest current input and associated effective context window
- missing, malformed, or changed usage: fail open
- `PreCompact`: deterministic checkpoint fallback before compaction, not a task launcher

The adapters keep internal and official JSON separate:

| Event | Allowed Relay response |
| --- | --- |
| `UserPromptSubmit` | Small host-launch success notice after both destination IDs, or exact manual fallback on failure |
| `PreToolUse` | The same host-launch result when ready, or a documented permission decision; allowed calls emit no unsupported common fields |
| `PreCompact` | Common fields reporting a refreshed checkpoint only |
| `Stop` | Common output fields only |

Internal capsule paths, metrics, guards, overflow records, delivery details, and V3 observation rows do not leak into official stdout unless the schema allows them. Adapters must not fabricate unavailable acknowledgement, stop, latency, token, or intervention telemetry. Errors fail open. `PreToolUse` emits `{}` when no event-specific output is required; other events use their documented common fields.

## Plugin-Bundled Hooks

The plugin uses the documented default `hooks/hooks.json`; an explicit `hooks` field in `.codex-plugin/plugin.json` is unnecessary. [Official plugin documentation](https://learn.chatgpt.com/docs/build-plugins) also establishes that installing or enabling a plugin does not automatically trust its non-managed hooks.

After install or any hook change:

1. In Codex CLI, open `/hooks`; in Codex App, use the plugin-hook trust prompt.
2. Confirm the Relay definitions and source are loaded.
3. Review and trust the current hook hash once.
4. Exercise `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` with a fully seeded transfer.
5. After that setup, normal Goal Mode handoff is automatic.

Installation and static validation alone do not prove load/trust.

## Codex CLI / OMX Compatibility Install

`install.sh` creates `scripts/workflow/relay_hook.sh`, which calls the installed canonical runtime. Example project configuration:

```toml
[hooks]
PreToolUse = ["bash scripts/workflow/relay_hook.sh PreToolUse"]
PreCompact = ["bash scripts/workflow/relay_hook.sh PreCompact"]
Stop = ["bash scripts/workflow/relay_hook.sh Stop"]
UserPromptSubmit = ["bash scripts/workflow/relay_hook.sh UserPromptSubmit"]
```

The CLI adapter can checkpoint and emit allowed hook context. Automatic clean-task creation still requires a Codex agent surface with thread tools.

## Capability Matrix

| Surface | Ready capsule | Official envelope | Auto-create clean task | Live trust check |
| --- | --- | --- | --- | --- |
| Codex App with thread tools | Yes | When hooks run | Yes | One-time plugin-hook trust |
| Codex App without thread tools | Yes | When hooks run | No | Host-dependent |
| Codex CLI/OMX | Yes | Yes | Requires agent orchestration | `/hooks` |

Cross-surface parity means equivalent normalized kernel, metrics, readiness, revision, delivery decisions, and aggregate chain accounting when telemetry exists. Unavailable host telemetry stays explicit rather than inferred. Parity does not mean byte-identical hook stdout where event schemas differ.

## Integration Invariants

- Scope active state, revision, lock, pointer, and delivery record by `session_id`.
- Keep capsule and prompt budgets independent from the experimental threshold.
- Emit at most one prompt copy per transport; never store that text in capsule or pointers.
- Do not switch from `resume_ready:false`.
- Do not use repo-latest metadata as cross-session authority.
- Never claim a hook is active until `/hooks` proves it is loaded and trusted.
- Hooks do not provide an OS-level ACL; exact ownership state remains authoritative.
