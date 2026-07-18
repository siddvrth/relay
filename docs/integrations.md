# Codex Integrations

Fresh Handoff supports Codex surfaces only. The Python runtime creates session-scoped capsules and internal orchestration results; each integration translates those results into capabilities allowed by its host.

## Codex App

When thread tools are available and continuous execution is authorized:

1. Obtain a ready internal result with exact capsule path/hash, session, revision, and one continuation prompt.
2. Resolve the current repository's project.
3. Persist launch intent and create exactly one clean local task with the single prompt as its initial turn.
4. Require destination verification and canonical exact acknowledgement; thread text is observation, never acknowledgement authority.
5. After acknowledgement, use `handoff_thread(source)` only when authority and checkout compatibility prove its target-interrupt semantics are safe. It is not a generic close API.
6. Otherwise record a cooperative stop result or durable read-only `termination_pending`; archive/visible closure is separate optional evidence.

Do not use `fork_thread` for production context shedding; a fork inherits completed conversation history. If thread tools are unavailable, return the exact continuation data without claiming a task was created.

Goal text can be preserved, but host-owned goal identity may not transfer. The clean task inspects current goal state before recreating or continuing the recorded objective.

## Codex Hooks

All command hooks receive JSON on stdin. Current [official hook documentation](https://learn.chatgpt.com/docs/hooks) documents `session_id` as common input. `UserPromptSubmit` also documents `prompt`; it does not document a context-used ratio.

Fresh Handoff therefore treats additional ratio fields as compatibility telemetry only:

- compatible ratio at or above `0.30`: threshold path is eligible
- missing ratio on `UserPromptSubmit`: no exact 30%-used claim
- `PreCompact`: deterministic checkpoint fallback before compaction

The adapters keep internal and official JSON separate:

| Event | Allowed Fresh Handoff response |
| --- | --- |
| `UserPromptSubmit` | Common fields plus one `hookSpecificOutput.additionalContext` prompt when delivery is emitted |
| `PreToolUse` | Deny source/pending-destination writes; allow strict read-only and canonical transfer-control operations |
| `PreCompact` | Common output fields only |
| `Stop` | Common output fields only |

Internal capsule paths, metrics, guards, overflow records, delivery details, and V3 observation rows do not leak into official stdout unless the schema allows them. Adapters must not fabricate unavailable acknowledgement, stop, latency, token, or intervention telemetry. Errors fail open with `{"continue":true}`.

## Plugin-Bundled Hooks

The plugin uses the documented default `hooks/hooks.json`; an explicit `hooks` field in `.codex-plugin/plugin.json` is unnecessary. [Official plugin documentation](https://learn.chatgpt.com/docs/build-plugins) also establishes that installing or enabling a plugin does not automatically trust its non-managed hooks.

After install or any hook change:

1. Open `/hooks` in Codex CLI.
2. Confirm the Fresh Handoff definitions and source are loaded.
3. Review and trust the current hook hash.
4. Exercise `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` with a fully seeded transfer.
5. Record the live evidence for release acceptance.

Installation and static validation alone do not prove load/trust.

## Codex CLI / OMX Compatibility Install

`install.sh` creates `scripts/workflow/checkpoint_and_continue_hook.sh`, which calls the installed canonical runtime. Example project configuration:

```toml
[hooks]
PreToolUse = ["bash scripts/workflow/checkpoint_and_continue_hook.sh PreToolUse"]
PreCompact = ["bash scripts/workflow/checkpoint_and_continue_hook.sh PreCompact"]
Stop = ["bash scripts/workflow/checkpoint_and_continue_hook.sh Stop"]
UserPromptSubmit = ["bash scripts/workflow/checkpoint_and_continue_hook.sh UserPromptSubmit"]
```

The CLI adapter can checkpoint and emit allowed hook context. Automatic clean-task creation still requires a Codex agent surface with thread tools.

## Capability Matrix

| Surface | Ready capsule | Official envelope | Auto-create clean task | Live trust check |
| --- | --- | --- | --- | --- |
| Codex App with thread tools | Yes | When hooks run | Yes | Host-dependent |
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
