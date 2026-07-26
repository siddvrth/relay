---
name: relay
description: Preserve quality across long Codex sessions with bounded, self-contained continuation capsules and clean-task prompts. Use after major milestones, before compaction or pauses, in goal mode, or when a new session must resume without the old transcript.
---

# Relay

## Purpose

Create a session-scoped Fresh Handoff capsule that a clean Codex task can resume without a transcript or predecessor checkpoint. Each revision contains one self-contained kernel; earlier revisions may help compare changes but are never required for recovery.

Defaults are independent:

- capsule: at most 4096 UTF-8 bytes
- transported continuation prompt: at most 1024 UTF-8 bytes
- automatic threshold policy: `0.30` used context when the host supplies compatible telemetry

Byte limits are deterministic hard ceilings. The flags may select smaller limits for testing or stricter local policy, but values above 4096/1024 are rejected before capsule, pointer, or delivery state is written; hooks fail open without injecting a continuation prompt. Byte counts and `(bytes+3)//4` proxies are storage/transport diagnostics; not evidence of token or cost savings.

## Boundaries

| Expectation | Contract |
| --- | --- |
| Transfer hidden model state | Not supported. The capsule preserves explicit durable facts only. |
| Resume from a delta chain | Not supported. Every ready revision resumes independently. |
| Always create a new task | Only when Codex App exposes thread tools; otherwise emit the exact capsule pointer and prompt. |
| Transfer goal identity | Preserve the derived goal identity and objective text, then inspect live goal state before continuing or recreating it. |
| Close or archive the source | Acknowledgement authorizes source stop work; quiescence and visible archive/closure are separate observed outcomes. |
| Support other editor hosts | Not supported. Fresh Handoff is Codex-only. |

## Trigger Policy

The `0.30` threshold is experimental and host-dependent. Official Codex `UserPromptSubmit` input documents `session_id` and `prompt`, but not a context-used ratio. Therefore:

1. If compatible ratio telemetry is present and is below `0.30`, do not threshold-handoff.
2. If compatible ratio telemetry is present and reaches `0.30`, refresh the session revision and make it eligible for delivery.
3. If the ratio is absent, do not claim that `UserPromptSubmit` performed an exact 30%-used trigger.
4. On every `PreCompact`, advance the session revision even when durable state is unchanged. A recent delivery still suppresses a duplicate prompt.
5. A manual or explicitly forced checkpoint can run at any time.

## Required Resume Kernel

A ready capsule is edge-structured so critical facts occur at both attention boundaries:

- opening identity kernel: objective, phase/status, next unfinished action, completion/stop condition, and critical constraints
- compact supporting middle: required remaining work and authoritative files plus any known completed work, decisions, blockers, and historical `validation_evidence`
- closing execution/ownership block: `next_action`, exact `resume_validation.command` and `.expected`, source/transfer/goal/revision/nonce identity, authoritative transport-SHA comparison, and the acknowledgement ownership rule

It must include all of these fields with concrete values:

- `session_id` and monotonically increasing `revision`
- objective, active task, phase, and status
- completion criteria
- remaining work and constraints/non-goals
- authoritative files or symbols
- one exact `next_action`
- exact `resume_validation.command` and `resume_validation.expected`

`completed_work`, `decisions`, `blockers`, and `validation_evidence` are optional known-state arrays. They persist as empty arrays when absent, and the capsule records their absence in one compact line instead of requiring filler prose. `--validation-status` is a deprecated input alias for repeatable `--validation-evidence`; `--next-step` is a legacy input alias for canonical `--next-action`. Neither alias is written to new state.

The low-level structural writer rejects missing fields, forbidden placeholders, circular next actions, cross-session identity, revisions that are not exact positive integers, and completed/remaining overlap.
The orchestrator owns authoritative stale/current/new monotonicity against durable revision state while holding the per-session `.handoff.lock`. Neither layer claims to detect semantic contradictions or generic prose.

If the critical kernel cannot fit, the capsule records `resume_ready:false` and a content-addressed overflow path/hash. Never switch sessions from a non-ready capsule. Optional verbose evidence normally moves to overflow without invalidating a complete kernel; if the overflow reference itself cannot fit beside that kernel, emit compact non-ready metadata with `critical:overflow_reference_budget_exceeded`.

Capsule readiness and transport readiness are independent. The mandatory prompt carries the exact path, session, revision, and capsule SHA-256. If those lines exceed `prompt_budget_bytes`, keep the capsule's `resume_ready:true` result, set `prompt_guard.fits:false`, and block delivery.

## Standard Workflow

1. Seed complete task state for the current session:

   ```bash
   python3 .agents/skills/relay/scripts/write_handoff.py \
     --update-active-task-only \
     --session-id "$CODEX_SESSION_ID" \
     --revision 1 \
     --objective "Finish the scoped implementation" \
     --active-task "Implement and verify the approved change" \
     --phase "implementation" \
     --status "Runtime complete; validation pending" \
     --completion-criteria "Targeted and full validation pass" \
     --remaining-work "Run validation and resolve failures" \
     --constraints "Do not add dependencies" \
     --authoritative-files "src/example.py" \
     --resume-validation-command "python3 tests/test_example.py" \
     --resume-validation-expected "exit 0 and all selected tests pass" \
     --next-action "Run the targeted test and fix the first failure"
   ```

2. Refresh through `context_handoff.py` or a configured hook. Revision creation and prompt delivery are separate: every `PreCompact` advances the revision, while the recent-delivery cooldown suppresses duplicate transport.
3. Continue only when the result has `resume_ready:true`, `prompt_guard.fits:true`, and an emitted prompt containing the exact capsule path, SHA-256, session, and revision.
4. If Codex App thread tools are available and continuous execution is authorized, record launch intent, then create exactly one clean local project task with the single emitted continuation prompt as its initial prompt.
5. The destination verifies the exact transfer ID, source, goal identity, revision, capsule SHA-256, nonce, live repo/goal state, `next_action`, and both exact `resume_validation` fields, then explicitly acknowledges.
6. Exact acknowledgement atomically revokes source write authority and makes the destination owner. The destination remains control-only until status reports either observed source quiescence or durable read-only `termination_pending` and `can_continue:true`.
7. Only after acknowledgement request an actually supported stop capability. Visible archive/closure is separate evidence and never proves quiescence.

Do not use `fork_thread` for the production handoff path: a fork inherits completed conversation history and does not shed that context.

## Fresh-Session Resume Protocol

The mandatory transported prompt identifies one capsule by exact path, session, revision, and SHA-256. In the fresh session:

1. Read `AGENTS.md` and other applicable project instructions.
2. Read the prompt-specified capsule, not whichever file appears newest.
3. Verify its SHA-256 against the exact transport/pointer and confirm `resume_ready:true`, the expected source, transfer ID, goal identity, revision, and nonce. The capsule cannot embed its own final digest; its closing block requires comparison to the authoritative transported SHA-256.
4. Inspect `git status --short`, current diffs, and each authoritative file or symbol.
5. Treat live repository and goal state as authoritative.
6. Continue the recorded goal objective only when needed; never replace an unrelated active goal.
7. Run the recorded `resume_validation.command`, confirm its exact expected observable, and use `transfer_control.py verify` with the bound `next_action` and `resume_validation` values.
8. Acknowledge exactly once, request/record supported source stop behavior, and wait for `can_continue:true` before substantial implementation.
9. Execute the capsule's exact next action, then refresh the session revision.

## State And Delivery Model

Runtime state is under:

```text
.omx/state/relay/
|-- .latest.json                         # metadata-only convenience pointer
`-- sessions/<session-hash>/
    |-- .active-task.json
    |-- .revision.json
    |-- .delivery.json
    |-- .handoff.lock
    |-- .pointer.json                    # authoritative session pointer
    |-- <timestamp>-r<revision>-handoff.md
    `-- overflow/<sha256>.json
```

The repo-latest pointer cannot authorize cross-session resume. Pointers contain metadata only—path/hash, source/transfer/goal/revision/nonce identity, delivery state, and metrics—and never duplicate the capsule or continuation prompt. Before reusing a session pointer, the orchestrator checks identity/readiness, path containment, file type, and SHA-256; any failure forces a new revision.

## Goal Mode

Pass `--goal-objective` with the exact goal text when goal-mode evidence belongs in the capsule. The continuation prompt deliberately omits full goal prose; it carries `goal_identity` and directs the fresh task to inspect live goal state plus the exact capsule. A fresh task recreates or continues the objective only when necessary.

Goal-period `tokensUsed` is narrower than total context, billing usage, cached input, or full transcript consumption. V2 evidence requires at least 20 unique tasks, one per pair; four distinct safe repo-relative preregistration artifacts and hashes; offset-aware run starts after the freeze and within the validator's future-skew bound; every candidate outcome passed and ready; quality non-inferiority; positive median savings; and the exact sign-test gate. Release validation resolves real control-to-candidate-to-HEAD ancestry, matches the declared repository to `origin`, verifies a framed digest of the frozen shipped runtime contract at both commits and the current tree, and verifies each preregistration artifact as the same tracked regular blob at candidate, HEAD, and current. Evidence may live in a later evidence-only commit when runtime has not drifted. Until every gate passes, preserve the exact `.codex-plugin/release-policy.json` `experimental_non_claim` contract.

## Script Surface

| Script | Purpose |
| --- | --- |
| `scripts/write_handoff.py` | Write a bounded v2 capsule and one bounded prompt |
| `scripts/context_handoff.py` | Scope revisions, locks, and delivery by session; translate official hook envelopes |
| `scripts/transfer_control.py` | Canonical durable transfer journal, acknowledgement, ownership guard, and stop-result authority |
| `scripts/goal_telemetry_report.py` | Analyze preregistered paired goal-token evidence |
| `scripts/test_write_handoff.py` | Standard-library contract and lifecycle tests |

Important flags include `--session-id`, `--revision`, `--capsule-budget-bytes`, `--prompt-budget-bytes`, all critical-kernel fields, `--force-handoff`, `--update-active-task-only`, and `--emit-json`. See `reference.md` for the complete contract and `examples.md` for ready and intentionally non-ready examples.

## Validation

```bash
python3 skills/relay/scripts/test_write_handoff.py
python3 skills/relay/scripts/test_transfer_control.py
python3 skills/relay/scripts/test_transfer_integration.py
python3 scripts/test_release_readiness.py
python3 scripts/validate_distribution.py
bash validate.sh
```

Plugin installation is not proof that hooks will execute. Before release, use `/hooks` in Codex CLI to confirm the bundled definitions are loaded, reviewed, and trusted.
