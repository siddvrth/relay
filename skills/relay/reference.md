# Fresh Handoff Reference

This is the low-level v2 contract for the `relay` skill. Start with the [project overview](../../README.md) and [installation guide](../../docs/installation.md).

## Independent Defaults

| Setting | Default | Meaning |
| --- | ---: | --- |
| `--capsule-budget-bytes` | `4096` | Encoded UTF-8 capsule limit; may be lowered, never raised above 4096 |
| `--prompt-budget-bytes` | `1024` | Transported prompt limit; may be lowered, never raised above 1024 |
| `--handoff-threshold` | `0.30` | Experimental generic default when compatible host telemetry exists; `0.50` and `0.70` are numeric overrides |
| `--dedup-seconds` | `300` | Session-scoped transport cooldown |

Changing a byte budget does not change the threshold. Overrides above the canonical 4096/1024-byte ceilings are rejected before state or delivery is written; official hooks fail open without additional context. Byte counts and `(bytes+3)//4` proxies are storage/transport diagnostics; not evidence of token or cost savings. Capsule readiness does not guarantee transport readiness: the mandatory prompt must fit independently.

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `RELAY_OBJECTIVE` | Default `--objective` |
| `RELAY_NEXT_STEP` | Legacy input-only default for canonical `--next-action`; `--next-step` is also input-only |
| `RELAY_GOAL_OBJECTIVE` | Goal text for goal-mode handoffs |
| `RELAY_THRESHOLD` | Override the hook stub's experimental `0.30` trigger ratio |

Critical resume state should be passed explicitly or seeded in the session's `.active-task.json`; those three environment variables do not make an incomplete kernel ready.

## Critical Fields And Structural Guard

The critical ready kernel contains `session_id`, `revision`, deterministic `transfer_id`, `goal_identity`, `transfer_nonce`, `objective`, `active_task`, `phase`, `status`, `completion_criteria`, `remaining_work`, `constraints`, `authoritative_files`, canonical `next_action`, and exact `resume_validation.command` plus `resume_validation.expected`. `completed_work`, `decisions`, `blockers`, and historical `validation_evidence` are optional arrays whose known-empty value is `[]`. Rendering is edge-structured: critical intent opens the capsule, supporting state occupies the middle, and exact action/resume-validation/ownership instructions close it.

New writes use only `validation_evidence`, `resume_validation`, and `next_action`. Repeat `--validation-evidence` for historical observations; `--validation-status` is its deprecated input-only alias. `--next-step` is the legacy input-only alias for `--next-action`. Legacy reads may map `validation` to `validation_evidence` and `next_step` to `next_action`, but they never derive exact resume validation from history.

The structural guard rejects:

- missing or placeholder critical values
- a session mismatch
- a revision that is not an exact positive integer
- a next action that circularly asks to find or create the next action through another checkpoint
- overlap between completed and remaining work

The low-level writer's revision check is deliberately structural.
The orchestrator owns authoritative stale/current/new monotonicity against durable revision state while holding the per-session `.handoff.lock`. Neither layer classifies semantic contradiction, factual accuracy, or boilerplate quality.

If the critical kernel exceeds the capsule budget, the emitted capsule contains safe metadata, `resume_ready:false`, and the content-addressed overflow path/SHA-256. No continuation prompt is emitted. Optional evidence normally moves to content-addressed overflow while a complete kernel remains ready. If the reference cannot fit beside the complete kernel, the runtime emits compact non-ready metadata and records `critical:overflow_reference_budget_exceeded`.

The mandatory continuation block contains the exact capsule path, source session, transfer ID, goal identity, revision, SHA-256, nonce, `next_action`, both `resume_validation` values, and the acknowledgement/ownership rule. Full goal prose is not repeated in the prompt. The final SHA-256 lives in the transport/pointer/transfer record because embedding a file's own final digest would be self-referential; the capsule close explicitly requires comparison to that authoritative digest. If the block exceeds the prompt budget, `build_continuation_prompt` returns no prompt: the capsule remains ready, `prompt_guard.fits` is false, `delivery_emitted` is false, and `skip_reason` identifies the prompt-budget failure.

## Runtime Paths

For session `S`, the implementation hashes the session identifier and stores:

```text
.omx/state/relay/
|-- .latest.json
|-- .ownership.json
|-- .transfer.lock
`-- sessions/<sha256(S)[:16]>/
    |-- .active-task.json
    |-- .revision.json
    |-- .delivery.json
    |-- .handoff.lock
    |-- .pointer.json
    |-- <timestamp>-r<revision>-handoff.md
    |-- .revoked.json
    |-- .active-transfer.json
    |-- transfers/r<revision>-<nonce-hash>.json
    `-- overflow/<sha256>.json
```

`.pointer.json` is authoritative for its session. `.latest.json` is a metadata-only convenience pointer and cannot authorize cross-session resume. Both pointers exclude capsule and prompt bodies.

Before reusing `.pointer.json`, the orchestrator verifies exact session, hashed scope, revision, `resume_ready:true`, a real file contained in that session directory, a lowercase 64-character SHA-256, and the file's actual digest. Any validation failure is recorded in `metrics.pointer_guard.failures` and forces a new revision instead of unsafe reuse.

## Revision Refresh Versus Delivery Dedup

Revision creation and prompt transport are separate state machines:

- changed task state can create a newer session revision
- every `PreCompact` advances the revision, even when the canonical state hash is unchanged
- a recent `.delivery.json` suppresses a duplicate prompt for that session
- a second session uses independent state, lock, revision, and delivery records

Therefore one delivery per cooldown does not mean one revision per cooldown. A newer revision may exist even when `delivery_emitted:false`. Use `--dedup-seconds 0` only for deterministic tests that require immediate transport.

## Context Telemetry Compatibility

`context_handoff.py` accepts documented hook input plus these optional compatibility fields, including inside `context`, `telemetry`, `usage`, or `metrics` objects:

- `contextUsed`, `context_used`
- `contextUsage`, `context_usage`
- `contextUsedPercent`, `context_used_percent`
- `contextUsedRatio`, `context_used_ratio`
- `tokenUsagePercent`, `token_usage_percent`
- `usageRatio`, `usage_ratio`
- `usagePercent`, `usage_percent`
- `context_usage_percent`
- `context_tokens` with `context_window_size`

Ratios accept `0.31`, `31`, or `31%`. Names ending in `Percent`, plus `context_usage_percent`, always use a `0..100` scale, so `1` means 1%.

These fields are compatibility inputs, not a documented Codex guarantee. Current [Codex hook documentation](https://learn.chatgpt.com/docs/hooks) says every command hook receives `session_id`; `UserPromptSubmit` additionally has `prompt`, but it does not document a context ratio. Missing telemetry cannot support a threshold claim: threshold mode stays inactive for missing or invalid ratios. Manual milestone and `PreCompact` triggers remain deterministic without ratio telemetry.

The timing experiment compares exactly six conditions: no proactive handoff, `0.30`, `0.50`, `0.70`, `PreCompact`-only, and milestone. `0.30` is the experimental generic default; `0.50` and `0.70` are numeric overrides, and no threshold is proven optimal. The 4096/1024-byte budgets are independent storage/transport limits and do not alter trigger decisions.

## Internal JSON Contract

Without `--official-hook-event`, `context_handoff.py` emits orchestration JSON. A successful ready result has this shape (values abbreviated):

```json
{
  "contract_version": 2,
  "should_handoff": true,
  "checkpoint_written": true,
  "revision_created": true,
  "capsule_path": "/abs/repo/.omx/state/relay/sessions/<scope>/<stamp>-r2-handoff.md",
  "capsule_sha256": "<sha256>",
  "continuation_prompt": "Use $relay. Continue from ...",
  "context_used_ratio": 0.31,
  "handoff_trigger_ratio": 0.3,
  "session_id": "<session-id>",
  "session_scope": "<scope>",
  "revision": 2,
  "transfer_id": "r2-<nonce-hash>",
  "goal_identity": "goal:sha256:<digest>",
  "transfer_nonce": "<one-use nonce>",
  "next_action": "<exact next action>",
  "validation_evidence": [],
  "resume_validation": {
    "command": "python3 <focused-test>",
    "expected": "exit 0 and selected tests pass"
  },
  "resume_ready": true,
  "prompt_guard": {"fits": true, "budget_bytes": 1024, "reason": null},
  "delivery_emitted": true,
  "deduped": false,
  "overflow": null,
  "metrics": {
    "capsule_budget_bytes": 4096,
    "capsule_bytes": 1840,
    "prompt_budget_bytes": 1024,
    "prompt_bytes": 390,
    "token_estimate_label": "approximate UTF-8 byte proxy; not a pass/fail gate"
  }
}
```

The continuation prompt exists in this transport result only. Its mandatory block includes the exact capsule path, source session, transfer ID, goal identity, revision, SHA-256, nonce, canonical `next_action`, exact `resume_validation.command`/`.expected`, and acknowledgement/ownership rule. It omits full goal prose and is not embedded in the capsule or pointers.

`write_handoff.py --emit-json` uses the same capsule, readiness, budget, session, revision, overflow, and metric fields without the orchestrator's delivery ledger.

## Official Hook Envelopes

Internal JSON is not written directly to a hook's stdout. The adapters translate it by event:

- `UserPromptSubmit`: common output fields plus `hookSpecificOutput.hookEventName="UserPromptSubmit"` and one `additionalContext` prompt only when delivery is emitted; acknowledged/revoked sources are blocked
- `PreToolUse`: exact transfer-control/read-only commands are allowed while pending; write-capable tools are denied for revoked sources and control-only destinations
- `PreCompact`: common output fields only; no UserPromptSubmit-specific object
- `Stop`: common output fields only; acknowledgement-gated stop state may force `continue:false`

This matches the documented event-specific shapes in [Codex hooks](https://learn.chatgpt.com/docs/hooks). Extra internal metrics never leak into the official envelope. Hook errors fail open with `{"continue":true}`.

The plugin uses the documented default `hooks/hooks.json`, so `.codex-plugin/plugin.json` does not need an explicit `hooks` field. Per [Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins), installing or enabling the plugin does not trust its non-managed hooks. Use `/hooks` to review and trust the current definitions; a changed hook hash requires review again.

## Codex CLI / OMX Hook Stub

Compatibility installation creates:

```text
scripts/workflow/relay_hook.sh
```

Example project configuration:

```toml
[hooks]
PreToolUse = ["bash scripts/workflow/relay_hook.sh PreToolUse"]
PreCompact = ["bash scripts/workflow/relay_hook.sh PreCompact"]
Stop = ["bash scripts/workflow/relay_hook.sh Stop"]
UserPromptSubmit = ["bash scripts/workflow/relay_hook.sh UserPromptSubmit"]
```

The stub reads one hook JSON object from stdin, calls the installed orchestrator, and writes only the allowed official response.

## Legacy Migration

`install.sh` treats `.agents/skills/session-continuity` and `.omx/state/session-continuity` as legacy:

- the active legacy skill, including its unbounded writer, is moved outside `.agents/skills/`; installed callers use only the bounded canonical writer
- original legacy runtime bytes remain unchanged
- only the newest valid legacy checkpoint is copied, with checksum/provenance, when no newer canonical checkpoint exists
- a newer canonical checkpoint wins
- canonical skill and hook surfaces are staged, compiled/syntax-checked, and compared before any live change
- one install lock covers both canonical swaps, verified migration publication, and legacy-skill archival
- any failure in that transaction restores the prior canonical skill, hook, imported state, and active legacy skill
- repeated install is idempotent
- `audit_install.sh` fails while an active legacy skill conflict remains

## Empirical Gate

`goal_telemetry_report.py` analyzes exact goal-period `tokensUsed`, which is not complete context or billing telemetry. V2 evidence requires at least 20 unique task IDs, one per pair; four distinct safe repo-relative preregistration paths and SHA-256 values; an offset-aware `frozen_at`; and an offset-aware `run_started_at` for every row that is strictly post-freeze and passes the future-skew guard. The statistical gate additionally requires every candidate to pass and be ready, quality non-inferiority, positive median savings, and a stable exact sign test.

Release binding resolves the declared control and candidate IDs as real commits, requires control to be an ancestor of candidate and candidate to be an ancestor of release `HEAD`, and requires the declared repository to equal the `origin` URL. A deterministic length-framed SHA-256 covers the frozen shipped runtime path set; declared control/candidate digests must match their commits, and the current runtime must still match candidate. Each preregistration path must be a regular tracked blob with the declared content at candidate, `HEAD`, and the clean current tree. This permits a later evidence-only release commit without permitting runtime or preregistration drift.

## Troubleshooting

| Symptom | Likely cause | Corrective action |
| --- | --- | --- |
| No threshold handoff from documented `UserPromptSubmit` input | No documented context ratio is present | Use `PreCompact`, a manual checkpoint, or host-provided compatibility telemetry; do not claim exact 30% triggering |
| `resume_ready:false` | Missing/invalid critical fields or critical byte overflow | Inspect `structural_guard` and any verified overflow path/hash; do not switch sessions |
| Ready capsule but no prompt | Mandatory identity block exceeded the configured prompt budget | Inspect `prompt_guard`; shorten the locator/session or use a budget up to the 1024-byte hard ceiling before delivery |
| New revision but no prompt | Session delivery cooldown is active, including after unchanged `PreCompact` | Resume only from an actually delivered exact prompt, or wait for a later eligible delivery |
| Wrong session or stale revision | Convenience pointer or old prompt was used | Use the prompt-specified path/SHA/source/transfer/goal/revision/nonce identity and session pointer |
| Hook appears installed but never runs | Definition is untrusted or not loaded | Open `/hooks`, review the source/hash, and trust it |
| Active legacy conflict | `.agents/skills/session-continuity` still exists | Run `repair_active_install.sh`, then `audit_install.sh` |
| Fresh task is not created | Codex App thread tools are unavailable | Return the exact capsule pointer and prompt without claiming creation |
| Destination cannot write | Transfer is unacknowledged or source stop is unresolved | Verify/acknowledge the exact identity, then request/record a supported stop result and wait for `status.can_continue:true` |
| Source cannot be interrupted | Host has no trustworthy target-stop adapter | Keep it read-only, persist `termination_pending`, and never claim closure or quiescence without adapter evidence |
