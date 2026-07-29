# Handoff Lifecycle

Relay repeats five stages for each Codex session: seed, refresh, decide delivery, create a clean task, and resume.

## 1. Seed A Complete Session Kernel

Seed the current session before automatic hooks need it. Use the full command in [Relay examples](../skills/relay/examples.md#seed-a-ready-session); objective and next action alone are intentionally insufficient.

The session's active state must contain concrete values for:

- identity: `session_id`, `revision`
- intent: objective, active task, phase, status, completion criteria
- progress: remaining work
- safety: constraints/non-goals
- evidence: authoritative files/symbols
- execution: one exact `next_action` and exact `resume_validation.command`/`.expected`

`completed_work`, `decisions`, `blockers`, and historical `validation_evidence` are optional known-state arrays. Omit their CLI flags when there is no fact to record; canonical state persists `[]` and the capsule emits one compact absence line. Legacy `validation`/`--validation-status` and `next_step`/`--next-step` are input compatibility only and never replace exact resume validation.

This state is stored under the hashed session directory, not in a repo-global active-task file.

## 2. Refresh A Revision

A manual `/relay`, an automatic `PreToolUse` threshold event, or `PreCompact` can invoke the orchestrator. `PreCompact` is the deterministic last-resort checkpoint when proactive usage cannot be read. Every ready revision is a self-contained capsule at or below 4096 UTF-8 bytes.

The configurable default `0.30` uses compatible hook telemetry when present; otherwise `PreToolUse` reads the latest trustworthy current-input/window pair from a bounded `transcript_path` tail. Missing or changed transcript schema fails open. The margin is motivated by Qwen2.5-7B 40–50% degradation evidence and is not proven optimal for GPT-5.6 or Codex.

Timing experiments compare exactly six conditions: no proactive handoff, `0.30`, `0.50`, `0.70`, `PreCompact`-only, and milestone. The 4096-byte capsule and 1024-byte prompt budgets govern storage/transport fit independently; they do not change the trigger comparison.

Structural guards run before readiness is reported. Critical overflow writes safe metadata with `resume_ready:false` and a content-addressed path/hash; it does not produce a continuation prompt. Optional evidence can overflow while a complete kernel remains ready, unless even the overflow reference cannot fit—in that case compact non-ready metadata is emitted.

## 3. Decide Delivery Separately

The revision ledger tracks state freshness. The delivery ledger tracks whether a prompt was recently transported. These are separate:

```text
state changed -> create revision r2 -> delivery allowed -> emit one prompt
state changed -> create revision r3 -> cooldown active -> suppress prompt
unchanged PreCompact -> create revision r4 -> cooldown active -> suppress prompt
```

Every `PreCompact` advances revision even when state is unchanged. The invariant is stable: one delivery per session cooldown does not limit revision refreshes. Another session uses another ledger and lock.

If no revision is otherwise needed, an old pointer is reusable only after checking its session/scope/revision/readiness, contained file path, and actual SHA-256. Any failure creates a new revision.

## 4. Create A Clean Task

Launch exactly one clean task only when the internal result reports all of the following:

- `resume_ready:true`
- exact `capsule_path` and `capsule_sha256`
- exact `session_id` and `revision`
- exact `transfer_id`, `goal_identity`, and one-use nonce
- canonical `next_action` and exact `resume_validation.command`/`.expected`
- `prompt_guard.fits:true`
- `delivery_emitted:true` with one continuation prompt

Record launch intent before spawning `codex app-server --stdio`. After `initialize` and `initialized`, call `thread/start` exactly once with the source repository's exact `cwd`, then call `turn/start` with the single bounded prompt. Persist `launching` before the spawn, `running` only after both destination IDs are returned, and `completed` or `failed` after the worker observes the terminal event. A repeated delivery ID is rejected before another process starts. Do not use `thread/fork`: copied history defeats context shedding.

If app-server cannot launch or the protocol fails, keep the capsule and manual prompt, record the failure atomically, and do not claim delivery. Acknowledgement timeout terminates the owned worker process group and records an unknown outcome; corrupt or missing delivery state fails closed before spawn. A terminal destination failure keeps its real IDs as evidence, clears the delivered claim, and surfaces the exact manual fallback. Never replace the exact identity with a “latest checkpoint” directory scan.

## 5. Resume Safely

The clean task must:

1. Read applicable `AGENTS.md` instructions.
2. Read the exact delivered capsule.
3. Verify its SHA-256, source session, transfer ID, goal identity, revision, nonce, and `resume_ready:true` against the exact transport/pointer/transfer record. The capsule close requires the SHA comparison because it cannot embed its own final digest.
4. Inspect live git state, diffs, and authoritative files/symbols.
5. Treat the repository and current goal state as authoritative.
6. Continue or recreate the recorded goal objective only when needed; never replace an unrelated active goal.
7. Run the recorded `resume_validation.command`, confirm its exact expected observable, then call the canonical `verify` transition with every exact identity, `next_action`, and `resume_validation` field.
8. Explicitly acknowledge. This atomically revokes source writes and commits destination ownership; an exact retry is idempotent, while stale/replay/cross-session variants fail.
9. Request and record only an actually supported stop capability. Visible archive/closure is separate. If no trustworthy stop is available, preserve source read-only state plus `termination_pending`.
10. Wait for `status.can_continue:true`, then execute the exact next action and refresh state with the result.

Goal telemetry, when a preregistered V3 study is running, continues across this boundary. The aggregate total includes source work before handoff, handoff generation, destination resume, and completion after resume. A passing candidate records zero old-source tokens/actions after acknowledgement. Rows without a handoff use explicit `not_applicable` acknowledgement/stop outcomes and null lifecycle latencies; no value is inferred from missing host telemetry.

## Repeating The Loop

```text
session A -> ready revision and one delivery -> session B
session B -> newer ready revision and one delivery -> session C
session C -> complete, or repeat
```

At every boundary, the new task needs one self-contained capsule and one bounded prompt—not the old transcript, a prior capsule chain, or prompt text duplicated in pointers. The prompt carries goal identity but omits full goal prose; the capsule and live goal inspection remain authoritative. Goal accounting can span multiple clean tasks even though authority and runtime state remain session-scoped.

## Stop Conditions

- Do not switch when `resume_ready:false`.
- Do not switch from a ready capsule when mandatory prompt fit failed or delivery was not emitted.
- Do not infer readiness from a file timestamp or repo-latest pointer.
- Do not deliver a duplicate prompt during the session cooldown.
- Do not keep two write-capable sessions in the same worktree.
- Do not stop the source before exact acknowledgement or treat delivery/read-thread text as acknowledgement.
- Do not treat archive/visible closure as proof of source quiescence.
- Stop the loop when the task is verified complete or a genuine authority blocker remains.
