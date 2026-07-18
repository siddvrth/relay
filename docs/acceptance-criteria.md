# Acceptance Criteria

## Runtime And Safety

| Requirement | Required behavior | Evidence |
| --- | --- | --- |
| Independent defaults | Trigger ratio remains `0.30`, capsule remains 4096 UTF-8 bytes, and prompt remains 1024 UTF-8 bytes; changing one does not change another | Boundary/default tests |
| Self-contained resume | Every ready revision preserves every critical kernel field and resumes without a predecessor | Kernel retention and predecessor-deletion tests |
| Fail closed on critical overflow | Capsule contains safe metadata, verified overflow path/hash, and `resume_ready:false`; no prompt or autonomous switch occurs | Critical-overflow tests |
| Bound optional evidence | Optional content-addressed overflow preserves a complete ready kernel when its reference fits; dense reference failure emits compact `resume_ready:false` metadata | Deterministic pruning and overflow-reference tests |
| Mandatory prompt identity | Prompt contains exact path/SHA/source/transfer/goal/revision/nonce identity plus smallest validation and acknowledgement rule; if that block exceeds budget, capsule may remain ready but delivery is blocked | Exact-boundary and minimum-budget tests |
| One prompt copy | Transported prompt is at most 1024 bytes and appears once; capsule and pointers contain no prompt text | Separation and duplicate-marker tests |
| Structural readiness only | Missing fields, placeholders, circular next action, session mismatch, stale revision, and completed/remaining overlap fail deterministically | Guard fixture tests |
| Session isolation | Active state, revision, lock, session pointer, and delivery ledger cannot collide across sessions | Cross-session and concurrency tests |
| Fresh revision, deduped delivery | Every `PreCompact`, including unchanged state, advances revision while transport remains limited to one delivery per session cooldown | `PreCompact`/dedup integration tests |
| Safe pointer reuse | Session/scope/revision/readiness, contained path/file, and SHA-256 validate before reuse; failure forces a revision | Corrupt/forged pointer tests |
| Edge-structured identity | Opening preserves intent/constraints; closing preserves action, smallest validation, exact transfer/goal/revision/nonce identity, authoritative SHA comparison, and acknowledgement rule | Opening/middle/closing position tests |
| Replay-safe ownership | Exact acknowledgement retry is idempotent; stale, cross-session, replayed, or mismatched acknowledgement cannot mutate ownership or stop state | Transfer-control hostile tests |
| Single writer | Source remains authoritative before acknowledgement; acknowledgement commits destination ownership before stop; destination writes wait for quiescence/read-only `termination_pending` | Ownership/guard integration tests |

## Hook And Surface Contract

| Requirement | Required behavior | Evidence |
| --- | --- | --- |
| Host-dependent threshold | Compatible telemetry at or above `0.30` can trigger; absent ratio does not produce an exact 30%-used claim | Compatibility and missing-ratio tests |
| Deterministic fallback | `PreCompact` follows the checkpoint path regardless of ratio | Hook lifecycle tests |
| Event-specific output | `UserPromptSubmit` may add one `additionalContext`; `PreToolUse` emits a permission decision; `PreCompact` and `Stop` emit common fields only | Official-envelope schema tests |
| Internal/official separation | Internal metrics and orchestration fields do not leak into official stdout | Adapter tests |
| Cross-surface parity | Canonical skill, plugin, CLI, and workflow adapters agree on normalized kernel/readiness/revision/delivery decisions | Parity tests |
| Hook trust | `/hooks` evidence is <=24 hours old and binds loaded/trusted events to current plugin version, hook-manifest hash, and all three adapter hashes | Live Codex CLI evidence; static install is insufficient |
| `PreToolUse` guard | All distributed hook surfaces include `PreToolUse`; revoked sources and control-only destinations deny writes while exact transfer control remains available | Manifest, wrapper, and installed-parity tests |

The threshold remains experimental because current [Codex hook documentation](https://learn.chatgpt.com/docs/hooks) does not document a context ratio on `UserPromptSubmit`.

## Migration And Installation

| Requirement | Required behavior | Evidence |
| --- | --- | --- |
| Preserve legacy bytes | `.omx/state/session-continuity` is never mutated | Before/after checksum tests |
| Copy-on-write import | Newest valid legacy checkpoint is copied with provenance/checksum only when no newer canonical checkpoint exists | Migration tests |
| Canonical wins | Newer canonical state is never replaced by legacy state | Ordering tests |
| One locked publish transaction | Canonical skill/hook swap, verified migration publication, and legacy-skill archival either all commit or all restore their prior live surfaces | Combined fault-injection/retry tests |
| Deactivate unbounded legacy writer | `.agents/skills/session-continuity` is archived outside the active namespace; installed calls use the canonical 4096/1024-bounded writer; audit fails while legacy remains active | Installed-path budget and temporary lifecycle tests |

## Clean-Task Resume

A production clean task is eligible only when the result reports `resume_ready:true`, `prompt_guard.fits:true`, exact capsule path/SHA-256/source/transfer/goal/revision/nonce identity, and `delivery_emitted:true`. Delivery alone never transfers authority. The destination must verify and explicitly acknowledge; it becomes sole owner at acknowledgement but remains control-only until `status.can_continue:true` proves source quiescence or durable read-only `termination_pending`.

`fork_thread` is not a production handoff because it inherits completed conversation history. If thread tools are absent, the package returns exact continuation data without claiming task creation.

## Static Gate Versus Empirical Gate

Static acceptance proves byte bounds, full critical-field retention, prompt separation, readiness behavior, session isolation, migration, adapters, and installation. It cannot prove lower token use or cost.

A token-efficiency claim requires a separate preregistered study with at least 20 unique tasks, one per pair. Its four distinct preregistration paths must be safe repo-relative tracked regular blobs whose hashes match candidate, release `HEAD`, and the clean current tree. Every row needs an offset-aware start strictly after `frozen_at` and within the future-skew guard. Release binding must prove matching `origin`, real control-to-candidate-to-HEAD ancestry, matching deterministic framed runtime digests, and no runtime drift after candidate; a later evidence-only commit is allowed. Pass only when:

1. every candidate outcome passed and every candidate capsule is ready
2. adjusted task quality is non-inferior for every pair, with no correctness or constraint regression
3. paired median goal-period token savings are positive
4. savings have a stable direction under the repository's exact sign-test rule

Report readiness/failure rates and capsule/prompt bytes separately. Goal `tokensUsed` is narrower than complete context, transcript, billing usage, cached input, or total model consumption.

The retained 20-pair clean-versus-fork dataset is historical pre-v2 evidence. It proves structural context isolation but failed the token gate; it is not evidence that v2 saves tokens or cost.
