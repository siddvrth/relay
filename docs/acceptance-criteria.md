# Acceptance Criteria

## Runtime And Safety

| Requirement | Required behavior | Evidence |
| --- | --- | --- |
| Independent defaults | Trigger ratio remains `0.30`, capsule remains 4096 UTF-8 bytes, and prompt remains 1024 UTF-8 bytes; changing one does not change another | Boundary/default tests |
| Self-contained resume | Every ready revision preserves every critical kernel field, including canonical `next_action` and exact `resume_validation.command`/`.expected`, and resumes without a predecessor | Kernel retention and predecessor-deletion tests |
| Fail closed on critical overflow | Capsule contains safe metadata, verified overflow path/hash, and `resume_ready:false`; no prompt or autonomous switch occurs | Critical-overflow tests |
| Bound optional evidence | Optional content-addressed overflow preserves a complete ready kernel when its reference fits; dense reference failure emits compact `resume_ready:false` metadata | Deterministic pruning and overflow-reference tests |
| Mandatory prompt identity | Prompt contains exact path/SHA/source/transfer/goal/revision/nonce identity plus `next_action`, both exact `resume_validation` values, and acknowledgement/ownership rules; full goal prose is omitted; if that block exceeds budget, capsule may remain ready but delivery is blocked | Exact-boundary and minimum-budget tests |
| One prompt copy | Transported prompt is at most 1024 bytes and appears once; capsule and pointers contain no prompt text | Separation and duplicate-marker tests |
| Structural readiness only | The low-level writer deterministically rejects missing critical fields, placeholders, circular next action, session mismatch, revisions that are not exact positive integers, and completed/remaining overlap; optional `completed_work`, `decisions`, `blockers`, and `validation_evidence` may be known-empty arrays | Guard fixture tests |
| Session isolation | Active state, revision, lock, session pointer, and delivery ledger cannot collide across sessions | Cross-session and concurrency tests |
| Fresh revision, deduped delivery | Under durable revision state and the per-session `.handoff.lock`, the orchestrator owns authoritative stale/current/new monotonicity; every `PreCompact`, including unchanged state, advances revision while transport remains limited to one delivery per session cooldown | Stale/current/new, concurrency, and `PreCompact`/dedup integration tests |
| Safe pointer reuse | Session/scope/revision/readiness, contained path/file, and SHA-256 validate before reuse; failure forces a revision | Corrupt/forged pointer tests |
| Edge-structured identity | Opening and closing deliberately repeat `next_action` and both exact `resume_validation` values; closing also preserves transfer/goal/revision/nonce identity, authoritative SHA comparison, and acknowledgement rule | Opening/middle/closing position tests |
| Replay-safe ownership | Exact acknowledgement retry is idempotent; stale, cross-session, replayed, or mismatched acknowledgement cannot mutate ownership or stop state | Transfer-control hostile tests |
| Single writer | Source remains authoritative before acknowledgement; acknowledgement commits destination ownership before stop; destination writes wait for quiescence/read-only `termination_pending` | Ownership/guard integration tests |

## Hook And Surface Contract

| Requirement | Required behavior | Evidence |
| --- | --- | --- |
| Host-effective threshold | Compatible telemetry or the latest trustworthy transcript usage is divided by the associated effective model window; absent or changed schema fails open | Context-usage extraction and boundary tests |
| Deterministic fallback | `PreCompact` follows the checkpoint path regardless of ratio | Hook lifecycle tests |
| Event-specific output | A ready `PreToolUse` adds one model-visible App launch envelope while preserving permission decisions; `PreCompact` reports checkpoint status only | Official-envelope schema tests |
| Internal/official separation | Internal metrics and orchestration fields do not leak into official stdout | Adapter tests |
| Cross-surface parity | Canonical skill, plugin, CLI, and workflow adapters agree on normalized kernel/readiness/revision/delivery decisions | Parity tests |
| Hook trust | `/hooks` evidence is <=24 hours old and binds loaded/trusted events to current plugin version, hook-manifest hash, and all three adapter hashes | Live Codex CLI evidence; static install is insufficient |
| `PreToolUse` guard | All distributed hook surfaces include `PreToolUse`; revoked sources and control-only destinations deny writes while exact transfer control remains available | Manifest, wrapper, and installed-parity tests |

The configurable `0.30` default is a conservative margin motivated by Qwen2.5-7B evidence of a 40–50% degradation region, not a proven optimum for GPT-5.6 or Codex.

## Installation

| Requirement | Required behavior | Evidence |
| --- | --- | --- |
| Locked canonical swap | Canonical skill/hook swap either commits or restores prior live surfaces | Fault-injection/retry tests |
| Installed budget enforcement | Installed calls use the canonical 4096/1024-bounded writer | Installed-path budget tests |
| Canonical fresh-install seed | Installed writer accepts a ready seed with optional arrays absent, persists them as `[]`, writes only `next_action`, binds exact `resume_validation`, and delivers through both installed adapters | Fresh-install CLI and hook smoke |
| Idempotent reinstall | Repeated install preserves source/installed parity without weakening safety | Temporary consumer install/repair/audit cycle |

## Clean-Task Resume

A production clean task is eligible only when the result reports `resume_ready:true`, `prompt_guard.fits:true`, exact capsule path/SHA-256/source/transfer/goal/revision/nonce identity, canonical `next_action`, exact `resume_validation.command`/`.expected`, and `delivery_emitted:true`. Delivery alone never transfers authority. The destination must verify the bound action/validation values and explicitly acknowledge; it becomes sole owner at acknowledgement but remains control-only until `status.can_continue:true` proves source quiescence or durable read-only `termination_pending`.

`thread/fork` is not a production handoff because it inherits completed conversation history. Automatic delivery uses host-side app-server `thread/start` and `turn/start`; the source model is not responsible for opening a destination. App-server failure retains exact continuation data without claiming task creation. Creating and running the destination is in scope; forcing Codex Desktop to visually navigate to it is not claimed without a supported API.

## Static Gate Versus Empirical Gate

Static acceptance proves byte bounds, full critical-field retention, prompt separation, readiness behavior, session isolation, adapters, and installation. It cannot prove lower token use or cost.

A token-efficiency claim requires a separate preregistered study with at least 20 unique tasks, one per pair. Its four distinct preregistration paths must be safe repo-relative tracked regular blobs whose hashes match candidate, release `HEAD`, and the clean current tree. Every row needs an offset-aware start strictly after `frozen_at` and within the future-skew guard. Release binding must prove matching `origin`, real control-to-candidate-to-HEAD ancestry, matching deterministic framed runtime digests, and no runtime drift after candidate; a later evidence-only commit is allowed. Pass only when:

1. every candidate outcome passed and every candidate capsule is ready
2. adjusted task quality is non-inferior for every pair, with no correctness or constraint regression
3. paired median goal-period token savings are positive
4. savings have a stable direction under the repository's exact sign-test rule

Report readiness/failure rates and capsule/prompt bytes separately. Goal `tokensUsed` is narrower than complete context, transcript, billing usage, cached input, or total model consumption.

Until a preregistered study passes those gates, preserve the experimental non-claim policy: no token-efficiency claim, no cost-savings claim, and no claim that Relay beats `/compact` on quality.
