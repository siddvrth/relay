# Validation And Metrics

Relay has two independent evidence lanes. Static validation proves the artifact contract; an empirical study is required to claim lower goal-token use. Passing one lane never implies passing the other.

## Static Validation

```bash
python3 skills/relay/scripts/test_write_handoff.py
python3 skills/relay/scripts/test_transfer_control.py
python3 skills/relay/scripts/test_transfer_integration.py
python3 scripts/test_release_readiness.py
python3 scripts/validate_distribution.py
bash validate.sh
```

Static checks cover:

1. 4096-byte capsule and 1024-byte prompt limits at UTF-8 boundaries
2. full critical-field retention for every ready capsule
3. structural readiness and `resume_ready:false` overflow behavior
4. transfer delivery, exact acknowledgement, ownership conflicts, stop outcome, and `termination_pending` recovery
5. prompt separation and metadata-only pointers
6. session-scoped locks, revision refresh, and delivery dedup
7. event-specific official hook envelopes
8. source/installed parity, bounded installed writer, and transactional legacy migration
9. temporary install, audit, repair, re-audit, and completion gate

Capsule bytes, prompt bytes, fixed duplicated-semantic marker counts, and `(bytes+3)//4` proxies may appear in deterministic reports. They are storage/transport diagnostics; not evidence of token or cost savings. They do not gate readiness or release.

A density comparison holds semantic values, revision, nonce/session lengths, byte budgets, and equal-length worktree paths constant across beginning, middle, and near-completion fixtures. It reports capsule and prompt counts separately, including deliberate capsule-edge repetition of `next_action` and both `resume_validation` values. The continuation prompt must retain exact identity/action/validation/ownership data while omitting full goal prose.

A live `/hooks` check is separate from static validation. Its JSON must be no more than 24 hours old and bind `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop`, the current plugin version, `hooks/hooks.json` SHA-256, and SHA-256 values for all three adapters.

## V2 Prior-Schema Evidence And Required V3 Study

The strict V2 parser/report remains available so prior studies are reproducible. V2 records exact goal-period `tokensUsed`, quality, readiness, outcomes, and bytes, but it does not contain aggregate source-to-destination chain accounting or post-acknowledgement source activity. V2 therefore cannot satisfy the acknowledgement-gated runtime's claim gate.

Before observing V3 results, freeze at least 20 representative fresh tasks, pair order/randomization, model, reasoning effort, repository state, task prompt, goal budget, scoring rubric, and the exact sign-test rule. For every pair, compare `candidate` with `current_canonical` under otherwise identical conditions. Retain the V2 document-level binding fields and record:

- shared run ID and condition label
- a unique task ID for every pair; at least 20 unique tasks total
- four distinct safe repo-relative preregistration paths and their exact task-set, randomization-plan, rubric, and analysis-plan SHA-256 values
- an offset-aware preregistration `frozen_at` and offset-aware per-row `run_started_at` strictly after it and within the future-skew guard
- real control-to-candidate-to-release-`HEAD` ancestry and a repository value matching `origin`
- a deterministic length-framed digest of the frozen shipped runtime contract at control and candidate, with the clean current runtime still matching candidate
- every preregistration artifact as a regular tracked blob whose content matches at candidate, release `HEAD`, and the clean current tree
- exact aggregate `tokensUsed` across `source_tokens_before_handoff`, `handoff_generation_tokens`, `destination_resume_tokens`, and `completion_tokens_after_resume`; the four components must sum exactly to `tokensUsed`
- outcome and `resume_ready`
- capsule and prompt bytes, separately
- adjusted output quality and rubric notes
- handoff and duplicated-work-action counts
- post-acknowledgement source tokens and actions
- transfer, acknowledgement, and source-stop latencies, with explicit `not_applicable` outcomes and null latencies for zero-handoff rows
- acknowledgement outcomes and attempt/failure counts
- source-stop capability, outcome, latency, and attempt/failure counts
- duplicate destinations, ownership conflicts, retries, `termination_pending` observation/recovery, and human-intervention count
- exact continuation checks for `next_action` selection, retained constraints, no repeated completed work, no skipped remaining work, repository/goal reconciliation, resume-validation quality, and middle-positioned fact recovery

All counts and token components are non-negative integers. Applicable latencies are finite and non-negative. A passing ready candidate row has zero post-acknowledgement source tokens and actions. Do not remove failed, blocked, `resume_ready:false`, termination-pending, or intervention rows. Their quality treatment and observability stay in the denominator.

The evidence document may be committed after the candidate in an evidence-only release commit. Candidate ancestry plus the no-drift and artifact checks prevent that allowance from weakening runtime or preregistration binding.

The timing experiment has exactly six diagnostic conditions: no proactive handoff, compatible `0.30`, compatible `0.50`, compatible `0.70`, `PreCompact`-only, and milestone. The `0.30` condition is the experimental generic default; `0.50` and `0.70` are numeric overrides. Manual milestone and `PreCompact` conditions are deterministic and do not require ratio telemetry. Every condition uses a non-claim label and `qualifies_as_claim_evidence:false`; none enters the canonical paired sign test unless a future study separately preregisters and powers it. Other diagnostics, including inherited/forked context, current canonical, edge-structured candidate, and beginning/middle/end fact positions, remain separately labeled non-claim observations rather than additional timing conditions.

## Quality Rubric

Score each dimension from 0 to 2:

| Dimension | Weight |
| --- | ---: |
| Task correctness | 40% |
| Constraint/non-goal adherence | 20% |
| Completeness | 15% |
| Validation evidence | 15% |
| Resume usefulness | 10% |

Raw quality is `50 × (0.40C + 0.20A + 0.15M + 0.15V + 0.10R)`, yielding 0–100. A failed, blocked, or non-ready run receives adjusted quality zero regardless of notes.

Task-level non-inferiority is strict: adjusted candidate quality must be at least adjusted control quality for every pair, with no correctness or constraint regression.

## Token-Efficiency Claim Gate

A future acknowledgement-gated token-efficiency claim requires a bound V3 study where all conditions pass:

1. at least 20 valid preregistered pairs are reported without excluding failures/non-ready runs
2. every candidate outcome passed and every candidate capsule is `resume_ready:true`
3. every candidate has accepted acknowledgement, an observed final source-stop outcome, no unresolved `termination_pending`, no human intervention, and all seven continuation checks passing
4. every passing candidate has zero post-acknowledgement source tokens and actions
5. output quality is task-level non-inferior
6. the paired median aggregate goal-token difference favors the candidate
7. the candidate-favoring direction is statistically stable under the exact sign-test rule implemented in `goal_telemetry_report.py`

V3 goal accounting spans the complete source-to-destination chain and does not reset at acknowledgement or task creation. It is still narrower than complete starting context, transcript size, cached/uncached input, output usage, billing totals, or total context consumption. Byte measurements and approximate token estimates never substitute for aggregate `tokensUsed`. Exact cost requires provider usage-category telemetry; otherwise cost numbers are sensitivity scenarios only.

Static V3 observability reports capsule/prompt byte distributions; readiness and failure rates; transfer, acknowledgement, and stop latency summaries; acknowledgement outcomes; post-acknowledgement source activity; duplicate work/destinations; ownership conflicts; stop capability/result distribution; retry distribution; termination-pending recovery; intervention; and each continuation check. These observations come from sanitized study rows or retained events and never mutate ownership state. Byte/proxy density measurements remain diagnostic even when the candidate is smaller.

Even passing evidence does not silently change release claims. `.codex-plugin/release-policy.json` must remain the exact four-field `experimental_non_claim` document until a separately approved release changes that public contract.

## Historical Pre-V2 Baseline

The retained 2026-07-11 clean-versus-fork dataset contains 20 historical pre-v2 pairs. Clean tasks structurally shed inherited turns, but they used fewer goal tokens in only 8 of 20 pairs. The paired median favored forks by 103.5 tokens and the exact two-sided sign-test p-value was about 0.5034.

That result failed the token-efficiency gate. It is useful negative baseline evidence about context isolation and the weakness of byte/turn proxies; it does not measure or validate the V2 self-contained kernel or the V3 acknowledgement-gated chain.

The historical report remains reproducible with caller-supplied pricing:

```bash
python3 skills/relay/scripts/goal_telemetry_report.py \
  --clean-tokens 1824,1863,1863,1826,2162,4823,3581,10931,3733,4760,6078,4870,3726,3176,10531,3690,21704,6485,5709,3809 \
  --fork-tokens 2017,2038,1960,2006,2033,5506,3659,7879,3367,4841,6000,4801,3300,2926,6164,3485,4590,4569,4422,5254 \
  --model <model-id> \
  --cached-input-rate <current-rate-per-million> \
  --input-rate <current-rate-per-million> \
  --output-rate <current-rate-per-million>
```

Recheck current official rates before any sensitivity report. Never describe those scenarios as a Codex bill.

## Experimental Timing Conditions

The `0.30` used-context threshold is a conservative configurable safety margin, not a payload budget or a proven optimum for GPT-5.6 or Codex. It is motivated by published Qwen2.5-7B evidence of a 40–50% long-context degradation region ([arXiv:2601.15300](https://arxiv.org/abs/2601.15300)). `0.50` and `0.70` remain numeric overrides for diagnostic comparison. Automatic operation uses compatible telemetry or the latest trustworthy transcript current-input/effective-window pair and fails open on missing or changed schema.

Compare identical multi-step tasks across the six frozen conditions: no proactive handoff, compatible `0.30`, compatible `0.50`, compatible `0.70`, `PreCompact`-only, and milestone. Manual milestone and `PreCompact` are deterministic; the three numeric conditions require compatible telemetry. Preserve failures and quality outcomes. Capsule/prompt budgets remain independent storage/transport controls and never select a timing condition.
