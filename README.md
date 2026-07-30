# Relay

Relay is a Codex plugin designed as an alternative to [`/compact`](https://learn.chatgpt.com/docs/developer-commands#keep-transcripts-lean-with-compact) for long-running Goal Mode work. Its `relay` skill serializes the current working state into a bounded, self-contained capsule so a clean task can resume without the old transcript.

Generic handoff tools transfer one session into another. Relay is automatic, verified, repeatable clean-context rotation for persistent Codex goals.


### `/relay` vs `/compact`

Official Codex documentation describes [`/compact`](https://learn.chatgpt.com/docs/developer-commands#keep-transcripts-lean-with-compact) as summarizing the visible chat to free tokens while retaining key points. Relay instead carries verified working state into a clean context.

The CLI `/compact` command is manual, and Codex can also compact chats automatically. Separately, the [Responses API supports automatic server-side compaction](https://developers.openai.com/api/docs/guides/deployment-checklist#leverage-compaction). Neither makes `/compact` wrong; Relay solves a different problem.

Relay:

- captures explicit current working state rather than trying to preserve the conversation as conversation
- uses a bounded self-contained capsule
- creates a genuinely clean task instead of remaining in the existing chat context
- rechecks the live repository and goal
- verifies exact capsule identity/SHA/revision/transfer
- carries one exact next action and exact resume validation
- transfers write ownership only after acknowledgement
- automatically repeats during long Goal Mode work

| | `/compact` | `/relay` |
| --- | --- | --- |
| Primary strategy | Summarize existing chat | Serialize current working state |
| Continues in | Compacted current chat | New clean task |
| Carries | Concise summary of earlier chat | Bounded explicit task state |
| Repository revalidation | Not Relay's explicit contract | Required |
| Exact next action/validation | Not guaranteed as typed fields | Required |
| Goal Mode rotation | Same-chat continuation | Designed to repeat across clean tasks |
| Best fit | Keep a conversation going | Long autonomous implementation goals |

Relay does not claim empirically higher quality, lower token use, or lower cost than `/compact`. Those comparisons have not been proven.

### Why Relay rotates early

Long-context quality does not have to decline smoothly. Published work on Qwen2.5-7B reports a cliff-like transition rather than a gentle slope ([Intelligence Degradation in Long-Context LLMs](https://arxiv.org/abs/2601.15300), arXiv:2601.15300):

- performance stays comparatively strong before a critical region
- that region appears around 40–50% of the model's maximum context
- reported F1 drops from about 0.55–0.56 to ~0.30 there (~45.5% degradation)

Conceptual sketch based on that Qwen2.5-7B result — not a measured Codex or GPT-5.6 curve:

```text
quality
  ^
  |  ──────────────────┐
  |                    │
  |                    └────────
  |
  +--------------------------------> context used
       30%        40%   50%
        ^
     Relay default
                  <--->
             observed cliff region
             in Qwen2.5-7B study
```

Relay's default `0.30` threshold is intentionally before that published 40–50% region, leaving safety margin before a potentially nonlinear degradation regime.

Important limits:

- `0.30` is configurable
- `0.30` is not proven optimal for GPT-5.6 or Codex
- the Qwen paper motivates conservative early rotation; it does not establish Codex's exact threshold
- “cliff-like” / “jagged” describes the observed nonlinear transition, not a universal law for every model
- the paper's “30% performance degradation” criterion is a different quantity from Relay's “30% context used” trigger

Host measurement uses `last_token_usage.input_tokens / model_context_window`. Compatible hook telemetry is preferred; when it is absent, Relay reads only a bounded tail of the documented `transcript_path`. Missing or changed transcript fields fail open.

## Contract At A Glance

| Property | Default or invariant |
| --- | --- |
| Capsule budget | 4096 encoded UTF-8 bytes |
| Continuation prompt budget | 1024 encoded UTF-8 bytes, one copy per transport |
| Resume shape | Self-contained, session-scoped revision |
| Automatic threshold | Configurable default `0.30`, measured against the current host's effective context window on `PreToolUse` |
| Deterministic fallback | `PreCompact` refreshes the checkpoint when proactive usage telemetry is unavailable |
| Safety on critical overflow | `resume_ready:false`; no autonomous task switch |
| Ownership transfer | Destination becomes sole writer only after exact acknowledgement |
| Dependencies | Python standard library plus small Bash wrappers |
| Hosts | Codex App and Codex CLI/OMX |

Timing evaluation uses six separate non-claim conditions: no proactive handoff, `0.30`, `0.50`, `0.70`, `PreCompact`-only, and milestone. No threshold is proven optimal. The 4096/1024-byte storage and transport budgets do not determine timing.

## What A Ready Capsule Preserves

- objective, active task, phase, status, and completion criteria
- remaining work, constraints/non-goals, and one exact `next_action`
- authoritative files or symbols plus exact `resume_validation.command` and `.expected`
- optional known-state arrays for completed work, decisions, blockers, and historical `validation_evidence` (empty without filler when absent)
- source/transfer/goal identity, revision, nonce, and the authoritative transport-SHA comparison rule

Each ready capsule resumes independently. The capsule contains no continuation prompt. A metadata-only pointer records the capsule path/hash, source/transfer/goal/revision/nonce identity, delivery state, and metrics; it does not copy capsule or prompt text.

The low-level structural writer rejects missing or placeholder fields, circular next actions, session mismatch, revisions that are not exact positive integers, and completed/remaining overlap.
The orchestrator owns authoritative stale/current/new monotonicity against durable revision state while holding the per-session `.handoff.lock`. If critical data cannot fit, Relay writes safe `resume_ready:false` metadata and a content-addressed overflow reference. Optional verbose evidence normally moves to content-addressed overflow while the capsule stays ready; if even that reference cannot fit beside the kernel, Relay emits compact non-ready metadata instead.

The mandatory prompt contains the exact capsule path, session, revision, SHA-256, `next_action`, both exact `resume_validation` fields, and transfer ownership instructions. It deliberately omits full goal prose. If those mandatory lines do not fit the configured prompt budget, the capsule can remain `resume_ready:true`, but `prompt_guard.fits` is false and delivery is blocked.

## Revision And Delivery

Revision refresh and prompt delivery are intentionally separate. State changes can create a newer session revision while a session-scoped delivery cooldown suppresses a duplicate prompt. Every `PreCompact` advances the revision even when durable state is unchanged, but it still does not duplicate delivery inside cooldown. Before any old pointer is reused, the runtime verifies its session/scope/revision/readiness, contained path, and capsule SHA-256; a failure creates a new revision. That means one delivery per cooldown, not one revision per cooldown.

During `PreToolUse` or `UserPromptSubmit`, a ready handoff invokes Relay's host-side `codex app-server --stdio` launcher. The launcher records intent before spawning, performs `initialize` and `initialized`, creates one fresh persisted conversation with `thread/start` at the source repository's exact `cwd`, and starts the bounded continuation with `turn/start`. It persists the returned thread and turn IDs atomically, keeps consuming events through `turn/completed`, and only then records completion. The source model is never responsible for opening the destination. `thread/fork` is excluded because copied history defeats context shedding. The hook reports only a small success notice after both IDs are acknowledged, or the exact manual continuation fallback on failure. Protocol waits are bounded; a pre-acknowledgement timeout stops Relay's worker process group and records an unknown outcome so it cannot trigger a blind duplicate. Relay inherits Codex configuration but cannot render interactive approval UI from the detached worker, so server approval requests are declined without granting additional permissions. Creating/running the destination is automatic; forcing Codex Desktop to visually focus it is not currently claimed. `PreCompact` remains a last-resort checkpoint and does not launch a task.

The clean-task loop is designed to repeat:

```text
session A -> ready revision and one delivery -> session B
session B -> newer ready revision and one delivery -> session C
session C -> complete, or repeat
```

## Repository Contents

- `.codex-plugin/plugin.json`: frozen plugin identity and install metadata
- `.codex-plugin/release-policy.json`: exact machine-readable experimental non-claim policy
- `skills/relay/`: canonical skill, scripts, reference, and examples
- `hooks/`: default plugin lifecycle hooks
- `codex/`: portable Codex CLI/OMX hook adapter
- `install.sh`: repo-local `.agents` and workflow installer
- `docs/`: installation, lifecycle, architecture, integration, evidence, and release guidance
- `artifacts/`: sanitized samples and optional future evaluation evidence

Per [Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins), the default `hooks/hooks.json` is discovered without an explicit manifest field. Plugin command hooks require a one-time trust review. Use `/hooks` for that review in Codex CLI; Codex App uses its own plugin-hook trust prompt. After trust, ordinary Goal Mode operation is automatic.

## Quickstart

```bash
git clone https://github.com/siddvrth/relay.git
cd relay
bash validate.sh
```

Install into a target repository:

```bash
bash install.sh /absolute/path/to/target-repository
bash audit_install.sh /absolute/path/to/target-repository
```

The installer creates `.agents/skills/relay/` and `scripts/workflow/relay_hook.sh`. Runtime state stays untracked under `.omx/state/relay/`. Canonical skill and hook surfaces are fully staged and verified first. One lock-scoped transaction then swaps both surfaces; any failure restores every prior live surface. Reinstall is idempotent and preserves source/installed parity.

See [examples](skills/relay/examples.md) for a full ready-kernel seed. Incomplete seed commands are intentionally non-ready.

## Validation And Evidence

```bash
python3 skills/relay/scripts/test_write_handoff.py
python3 skills/relay/scripts/test_transfer_control.py
python3 skills/relay/scripts/test_transfer_integration.py
python3 scripts/test_release_readiness.py
python3 scripts/validate_distribution.py
bash validate.sh
```

The release process also builds and extracts the exact archive into a temporary consumer repository, audits the install, runs the completion gate, and verifies the Codex App handoff contract. `scripts/check_release_readiness.py` separately requires a clean committed release checkout.

Static byte/fidelity checks and token-efficiency evidence are separate. Capsule bytes, prompt bytes, and `(bytes+3)//4` proxies are storage/transport diagnostics; not evidence of token or cost savings.

Do not claim token or cost improvement until a preregistered study has at least 20 paired runs with one unique task ID per pair, every candidate passed and ready, task-level output-quality non-inferiority, positive median goal-token savings, and the exact sign-test result. Release validation also binds four distinct preregistration paths and hashes, post-freeze run timestamps, repository origin, real control-to-candidate-to-release ancestry, and a deterministic digest of the frozen shipped runtime contract with no candidate-to-release drift. Evidence may be committed after the candidate when that later commit changes evidence only. The public release remains governed by the exact four-field `.codex-plugin/release-policy.json` `experimental_non_claim` contract.

## Documentation

- [Installation](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Handoff lifecycle](docs/lifecycle.md)
- [Codex integrations](docs/integrations.md)
- [Acceptance criteria](docs/acceptance-criteria.md)
- [Validation and metrics](docs/metrics.md)
- [Release process](docs/release.md)
- [Artifact and privacy policy](docs/artifacts.md)
- [Skill protocol](skills/relay/SKILL.md)
- [CLI and hook reference](skills/relay/reference.md)

## License

Relay is available under the [MIT License](LICENSE).
