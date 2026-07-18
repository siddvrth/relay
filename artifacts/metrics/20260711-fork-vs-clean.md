# Fork Versus Clean Task Comparison

Date: 2026-07-11

> Historical scope: this study predates the v2 self-contained, session-scoped kernel. It proves structural context isolation and failed its token-efficiency gate; it is not evidence of v2 token or cost improvement.

## Method

Two read-only Codex App tasks received the same nonce-bearing prompt, repository state, capsule hash, model, and reasoning effort. One task was a same-directory fork of the completed source task; the other was a clean local project task created with `create_thread`. Both were instructed to return only the nonce acknowledgement and not to modify files.

The repository status hash, tracked/untracked file-manifest hash, file count, and capsule hash were identical before and after the comparison. Both tasks returned the exact requested acknowledgement and were archived afterward.

## Exact Host-Visible Results

| Variant | Completed turns returned by `read_thread` | Inherited completed turns | Serialized visible turn-summary characters | UTF-8 bytes |
| --- | ---: | ---: | ---: | ---: |
| Fork | 7 | 6 | 25,032 | 25,296 |
| Clean task | 1 | 0 | 1,350 | 1,350 |

The fork exposed about 18.5 times as many serialized visible-summary characters as the clean task. This is direct evidence that the fork inherited completed conversation history while the clean task did not.

## Token Telemetry Boundary

The App `create_thread`, `fork_thread`, and `read_thread` responses contained no `input_tokens`, `context_tokens`, context-window, or context-usage fields. Therefore the first run does not claim exact token totals.

Using the rough English-text proxy of four visible characters per token gives approximately 6,258 visible-summary tokens for the fork and 338 for the clean task, or about 94.6% fewer for the clean task. These are estimates only. They exclude hidden system and developer instructions, tool schemas, cached context, internal reasoning, and any source messages replaced by summaries, so they are not billed-token or model-context measurements.

## Conclusion

The verifiable result is structural: the fork inherited six completed turns; the clean task inherited none and still acknowledged the same controlled prompt. This supports `create_thread` as the production context-shedding path while reserving `fork_thread` for branching or diagnostics.

## Goal-Mode Follow-Up

The goal-mode experiment now contains 20 valid paired, read-only trials: the original five acknowledgements plus 15 repository-reading tasks across handoff reconstruction, installation portability, validation coverage, hook behavior, capsule boundaries, release readiness, and metric interpretation. Every task used `gpt-5.6-sol`, high reasoning effort, and a requested 5,000-token goal budget. One additional attempted pair was excluded before analysis because the clean task never produced a turn result.

The raw sanitized observations are stored in `20260711-goal-telemetry-20-pairs.json`. Positive differences below mean the clean task used fewer goal tokens.

| Result | Exact value |
| --- | ---: |
| Valid pairs | 20 |
| Clean used fewer tokens | 8 |
| Clean used more tokens | 12 |
| Clean total | 107,144 |
| Fork total | 80,817 |
| Clean mean | 5,357.2 |
| Fork mean | 4,040.85 |
| Mean fork-minus-clean difference | -1,316.35 |
| Median fork-minus-clean difference | -103.5 |
| Paired-difference standard deviation | 3,940.82 |
| Exact two-sided sign-test p-value | 0.5034 |

The expanded evidence does **not** show a stable goal-token improvement. Clean tasks used more goal-period tokens in 12 of 20 pairs, the paired median favored forks by 103.5 tokens, and the result was extremely variable. One clean installation-audit task used 21,704 goal tokens versus 4,590 in its fork; preserving that outlier is important because hiding it would overstate efficiency.

Substantive answers agreed in 19 of 20 valid pairs. The remaining pair exposed an ambiguous rubric rather than a clear implementation failure: “PLUGIN_ROOT required” can mean manifest injection is required or that the shell script has no fallback. Future quality trials should use unambiguous, machine-gradable fields.

The goal is created after the task prompt is ingested. Consequently, `tokensUsed` measures exact goal-period work but not complete starting context, context prefill, or billing usage. The structural context-shedding result remains valid: clean tasks inherited zero completed turns while the controlled fork inherited six.

## Cost Sensitivity

Official GPT-5.6 Sol API rates were rechecked on the measurement date: $0.50 per million cached-input tokens, $5 per million uncached-input tokens, and $30 per million output tokens. Goal telemetry does not split the 107,144 clean and 80,817 fork tokens among those categories, so the following are sensitivity scenarios—not Codex charges.

| Scenario for all goal tokens | 20 clean trials | 20 fork trials | Fork minus clean |
| --- | ---: | ---: | ---: |
| Cached input at $0.50/M | $0.053572 | $0.040409 | -$0.013164 |
| Uncached input at $5/M | $0.535720 | $0.404085 | -$0.131635 |
| Output at $30/M | $3.214320 | $2.424510 | -$0.789810 |

Negative differences mean the clean trials cost more under that all-one-category assumption. These are API-equivalent sensitivity values, not the user's Codex subscription bill. Prompts above the model's long-context threshold may use different rates. Recheck current pricing before every report: <https://developers.openai.com/api/docs/models/gpt-5.6-sol>.

## Empirical Decision

The clean-task architecture passes the structural goal—removing inherited completed turns—and generally reconstructs the tested state correctly. It does not currently pass a token- or cost-improvement gate. Publication claims must therefore describe context isolation and durable restart behavior, not token savings or lower cost.

The next meaningful evaluation should use realistic multi-step continuation tasks and measure completion correctness, regressions, latency, human intervention, and end-to-end usage-category telemetry when available. More short read-only pairs would not resolve whether clean handoffs improve long-running implementation quality.
