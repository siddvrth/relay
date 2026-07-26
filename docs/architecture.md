# Architecture

Relay has four layers: plugin distribution, canonical portable source, compatibility-installed surfaces, and session-scoped runtime state.

## Source And Installed Surfaces

| Path | Responsibility |
| --- | --- |
| `.codex-plugin/plugin.json` | Relay identity and install metadata |
| `.codex-plugin/release-policy.json` | Exact four-field experimental non-claim policy |
| `hooks/` | Plugin-bundled lifecycle definitions and adapter |
| `skills/relay/` | Canonical protocol, reference, examples, and Python runtime |
| `codex/` | Portable Codex CLI/OMX adapter source |
| `.agents/skills/relay/` | Generated compatibility install |
| `scripts/workflow/relay_hook.sh` | Generated compatibility adapter |

Edit canonical source first, then reinstall and audit generated surfaces.

## Runtime State

```text
.omx/state/relay/
|-- .latest.json                         # metadata-only convenience pointer
|-- .ownership.json                      # acknowledgement linearization point
|-- .transfer.lock                       # global lifecycle/ownership serialization
`-- sessions/<session-hash>/
    |-- .active-task.json                # current complete kernel facts
    |-- .revision.json                   # revision and state hash
    |-- .delivery.json                   # last transported prompt
    |-- .handoff.lock                    # session-scoped writer lock
    |-- .pointer.json                    # authoritative session pointer
    |-- .revoked.json                    # fail-safe source revocation tombstone
    |-- .active-transfer.json            # latest transfer projection
    |-- transfers/r<revision>-<nonce-hash>.json
    |-- <timestamp>-r<revision>-handoff.md
    `-- overflow/<sha256>.json
```

The runtime directory is gitignored because it may contain private worktree details. The repo-latest pointer cannot authorize cross-session resume. Pointers contain path/hash, session/revision, delivery state, and authority-facing runtime metrics only; capsule and prompt bodies are excluded. Sanitized V3 study/observation telemetry is a separate release-evidence surface and never authorizes or mutates transfer ownership.

## Self-Contained Kernel

Every ready revision contains enough critical state to resume independently. Its opening identity kernel carries objective, phase/status, canonical `next_action`, exact `resume_validation.command`/`.expected`, completion condition, and constraints; the middle carries required remaining work and authoritative files plus optional completed work, decisions, blockers, and historical `validation_evidence`; the closing block deliberately repeats action and resume validation with exact source/transfer/goal/revision/nonce identity and the sole-writer-after-acknowledgement rule. Known-empty optional arrays render as one compact absence line rather than filler. The final capsule SHA-256 is necessarily external to the bytes it hashes, so the close requires comparison to the exact transport/pointer/transfer-record digest.

The previous revision is optional evidence. It can help rank changed facts or compute metrics, but it is never an ancestry dependency.

Encoded UTF-8 byte caps are authoritative:

- capsule: 4096-byte hard ceiling
- transported prompt: 1024-byte hard ceiling

Callers may choose smaller byte budgets, but larger overrides are rejected before state or delivery writes. If optional evidence does not fit, it moves to a content-addressed overflow object. If the critical kernel does not fit—or if the optional overflow reference cannot fit beside the kernel—the emitted safe capsule has `resume_ready:false` and no continuation prompt; autonomous switching is blocked.

Prompt transport has its own guard. The mandatory prompt block contains exact path/SHA/source/transfer/goal/revision/nonce identity, `next_action`, both exact `resume_validation` values, and acknowledgement/ownership rules. It omits full goal prose. If it exceeds the prompt budget, the capsule can remain ready while `prompt_guard.fits:false` blocks delivery.

## State Flow

1. A session seeds or refreshes its complete active-task state.
2. `PreToolUse` evaluates current host usage while preserving the authority guard; manual `/relay` uses the same clean-task path and `PreCompact` remains the checkpoint fallback.
3. The orchestrator locks that session, computes a canonical state hash, and decides whether to create a revision.
4. `write_handoff.py` applies structural guards and byte budgets, then writes a self-contained capsule atomically.
5. Before pointer reuse, the orchestrator verifies session/scope/revision/readiness, path containment/file type, and actual SHA-256. Failure forces a new revision.
6. The orchestrator writes metadata-only session and repo-latest pointers.
7. Independently, prompt fit and the session delivery ledger decide whether the one bounded prompt may be transported.
8. The adapter translates internal JSON into the event's official hook envelope.
9. A clean task verifies exact identity plus live repo/goal state, the bound `next_action`, and exact `resume_validation`, then explicitly acknowledges.
10. Acknowledgement writes the source tombstone and atomically publishes destination ownership before any stop request. Destination implementation waits for observed source quiescence or durable read-only `termination_pending` with `can_continue:true`.

## Revision And Delivery Invariant

Revision freshness is not delivery frequency. Changed task state can create a newer revision during the delivery cooldown, and every `PreCompact` advances the revision even when state is unchanged; the recent delivery record can still suppress another prompt. A second session has independent active state, lock, revision, pointer, and delivery ledger.

This separation prevents stale state while retaining one delivery per session cooldown.

## Hook Boundary

The internal orchestrator result includes capsule, readiness, revision, metrics, overflow, and delivery data. Official hook stdout is a separate translation:

- a ready `UserPromptSubmit` may return a `relay.codex_app.clean_task.v1` launch envelope when compatible telemetry is already present
- a ready `PreToolUse` returns the same model-visible launch envelope while preserving its write-authority permission decision
- `PreCompact` writes or refreshes a checkpoint and reports that fact through common output only; it does not claim task creation
- `Stop` uses common fields only

Official [Codex hook documentation](https://learn.chatgpt.com/docs/hooks) documents `transcript_path` but warns that its JSONL format is unstable. Relay therefore accepts compatible telemetry first, then reads only the final 256 KiB of the transcript for the latest exact `event_msg/token_count` record. It uses `last_token_usage.input_tokens`, never cumulative spend, with the sibling effective model window and fails open on schema drift.

V3 aggregate telemetry spans the source-before-handoff, handoff-generation, destination-resume, and completion-after-resume portions of one goal. Accounting does not reset at acknowledgement or session change. Static observability is derived from retained lifecycle events or sanitized study rows; rejected operations are never counted by modifying authoritative acknowledgement, stop, or ownership state.

## Failure Model

- Before an acknowledgement boundary exists, malformed hook/checkpoint input retains the legacy fail-open behavior. Durable revocation or destination ownership makes missing/corrupt authority handling fail closed for affected writers.
- Readiness fails closed: a non-ready capsule never authorizes autonomous switching.
- Install audits and validation fail closed on source drift or an active legacy skill conflict.
- Thread-tool absence returns exact continuation data without pretending a clean task exists.
- `fork_thread` is excluded from production handoffs because it inherits completed conversation history.
- Exact acknowledgement makes the destination sole owner before stop work; hooks are defense-in-depth and not an OS write ACL.

## Legacy Boundary

`skills/relay/` is canonical. `.agents/skills/session-continuity` and `.omx/state/session-continuity` are legacy. Canonical copies are fully staged and verified before any live change. Under one install lock, a single rollback transaction swaps the canonical skill and hook, publishes any verified copy-on-write import, and archives the active legacy skill. Failure restores all prior live surfaces. Original legacy bytes remain untouched; archived legacy writer bytes are retained but inactive, so they cannot bypass canonical budgets.
