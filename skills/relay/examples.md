# Fresh Handoff Examples

These examples use a concrete session ID and the full critical kernel. Replace sample values with current facts; do not use placeholders such as `TBD`, `unknown`, or `none` in critical fields. Optional `completed_work`, `decisions`, `blockers`, and `validation_evidence` may be absent; the writer persists their known-empty arrays without filler.

## Seed A Ready Session

```bash
SESSION_ID="example-session-01"
python3 .agents/skills/relay/scripts/write_handoff.py \
  --update-active-task-only \
  --session-id "$SESSION_ID" \
  --revision 1 \
  --objective "Finish and validate the scoped feature" \
  --active-task "Complete the implementation and evidence" \
  --phase "implementation" \
  --status "Core edit complete; tests pending" \
  --completion-criteria "Targeted and full validation pass" \
  --remaining-work "Run tests and resolve any regression" \
  --constraints "Do not add dependencies" \
  --authoritative-files "src/example.py" \
  --resume-validation-command "python3 tests/test_example.py" \
  --resume-validation-expected "exit 0 and all selected tests pass" \
  --next-action "Run the targeted test and fix the first failure"
```

This beginning-of-work seed is ready without optional filler. Add repeatable `--validation-evidence` only for historical results that actually exist. `--validation-status` and `--next-step` remain legacy input aliases; new state is written as `validation_evidence` and `next_action`, and historical evidence never substitutes for exact `resume_validation`.

## Manual Milestone Revision

Manual checkpoints do not depend on the experimental context ratio:

```bash
python3 .agents/skills/relay/scripts/context_handoff.py \
  --repo . \
  --session-id "$SESSION_ID" \
  --trigger manual \
  --reason "runtime implementation milestone" \
  --force-handoff
```

The seeded session state supplies the full kernel. Inspect `resume_ready`, `capsule_path`, `capsule_sha256`, `session_id`, `transfer_id`, `goal_identity`, `transfer_nonce`, `revision`, and `delivery_emitted` in the JSON result before initiating a clean task.

## Threshold Simulation With Compatibility Telemetry

This demonstrates an optional compatibility field; official `UserPromptSubmit` does not document a context ratio.

```bash
printf '%s\n' '{"session_id":"example-session-01","prompt":"continue the task","contextUsed":"31%"}' | \
  python3 .agents/skills/relay/scripts/context_handoff.py \
    --repo . \
    --stdin-json \
    --trigger threshold
```

At `31%`, a complete ready session can emit one delivery. Without `contextUsed` (or another accepted compatibility field), the threshold event does not claim an exact 30% trigger.

## Deterministic PreCompact Fallback

```bash
printf '%s\n' '{"session_id":"example-session-01","hook_event_name":"PreCompact","trigger":"auto"}' | \
  bash scripts/workflow/relay_hook.sh PreCompact
```

The official response contains common output fields only. It never includes the `UserPromptSubmit`-specific `additionalContext` shape.

## Goal-Mode Session

Seed the same complete kernel with the exact goal text:

```bash
python3 .agents/skills/relay/scripts/write_handoff.py \
  --update-active-task-only \
  --session-id "$SESSION_ID" \
  --revision 1 \
  --objective "Ship the verified change" \
  --active-task "Complete implementation, review, and QA" \
  --phase "verification" \
  --status "Implementation complete; QA pending" \
  --completion-criteria "Review and adversarial QA are clean" \
  --completed-work "Completed the implementation" \
  --remaining-work "Run review and adversarial QA" \
  --constraints "Preserve output quality" \
  --decisions "Use self-contained session revisions" \
  --blockers "Live hook trust check requires Codex CLI" \
  --authoritative-files "skills/relay/SKILL.md" \
  --validation-evidence "Targeted tests passed" \
  --resume-validation-command "bash validate.sh" \
  --resume-validation-expected "exit 0 and all checks pass" \
  --next-action "Run the full validation gate" \
  --goal-objective "Ship the feature without quality regression"
```

The new task inspects live goal state before recreating or continuing this objective. It never replaces an unrelated active goal.

## Intentionally Non-Ready Diagnostic

An incomplete kernel is useful only to demonstrate fail-closed behavior:

```bash
python3 skills/relay/scripts/write_handoff.py \
  --session-id "diagnostic-session" \
  --revision 1 \
  --objective "Demonstrate readiness failure" \
  --next-action "Inspect structural_guard in the JSON result" \
  --force-handoff \
  --emit-json
```

Expected: a capsule is written with `resume_ready:false`, no continuation prompt is emitted, and autonomous switching is blocked.

An undersized prompt budget is different: with a complete kernel, `--prompt-budget-bytes 128` can leave `resume_ready:true` while returning `prompt_guard.fits:false`, no prompt, and no delivery. Do not confuse capsule readiness with permission to switch.

## Safe Clean-Task Input

The generated prompt already carries the exact path, session, revision, SHA-256, `next_action`, and both `resume_validation` values. Use the exact values from one ready, delivered internal result; do not substitute a directory scan or a “latest” file:

```text
Use $relay.
Capsule: <exact capsule_path from this delivery>
SHA-256: <exact capsule_sha256 from this delivery>
Source session: <exact session_id from this delivery>
Transfer ID: <exact transfer_id from this delivery>
Goal identity: <exact goal_identity from this delivery>
Expected revision: <exact revision from this delivery>
Nonce: <exact transfer_nonce from this delivery>
Expected readiness: true
Exact next action: <exact next_action from this delivery>
Resume validation command: <exact resume_validation.command from this delivery>
Resume validation expected: <exact resume_validation.expected from this delivery>
Read AGENTS.md and the complete capsule. Verify the exact identity and final SHA-256 against the authoritative transport, inspect live repository/goal state, and run the recorded resume validation. Run transfer_control.py verify with the bound action/validation values, then acknowledge the exact identity. The source remains authoritative and this destination remains control-only before acknowledgement. After acknowledgement, wait for status can_continue:true before executing the exact next action.
```

Only one copy of this continuation instruction belongs in the transport. Do not embed it in the capsule or any pointer. Full goal prose is intentionally omitted from the prompt; the exact capsule and `goal_identity` bind the destination to the live goal check.

The exact `verify` and `acknowledge` commands require source session, transfer ID, destination session/task IDs, goal identity, capsule path/revision/SHA-256, and nonce. `verify` additionally requires `--repository-inspected`, `--goal-inspected`, the exact `next_action`, and both exact `resume_validation` values. Use values from the one delivered transfer record; never reconstruct them from a newest-file scan. An exact acknowledgement retry is idempotent, but any stale, replayed, or cross-session mismatch is rejected.
