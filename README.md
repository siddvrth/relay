# Relay

Relay is a Codex plugin for long-running work. Its `relay` skill writes bounded, evidence-backed session revisions so a clean task can resume without the old transcript or a chain of earlier checkpoints.

The plugin and bundled skill are both named `relay`.

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

The `0.30` policy is a conservative configurable safety margin, not a proven optimum for GPT-5.6 or Codex. It is motivated by published Qwen2.5-7B evidence that reports a 40–50% long-context degradation region ([arXiv:2601.15300](https://arxiv.org/abs/2601.15300)). Relay first accepts compatible hook telemetry; when it is absent, it reads only a bounded tail of the documented `transcript_path` and uses the latest `last_token_usage.input_tokens` divided by that record's `model_context_window`. Because transcript JSONL is explicitly not a stable hook interface, missing or changed fields fail open.

Timing evaluation uses six separate non-claim conditions: no proactive handoff, `0.30`, `0.50`, `0.70`, `PreCompact`-only, and milestone. No threshold is proven optimal, and missing telemetry cannot support a threshold claim. The 4096/1024-byte storage and transport budgets do not determine timing.

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

During `PreToolUse`, a ready handoff adds a short model-visible launch instruction. Codex App then records launch intent, creates exactly one clean local task with the existing bounded prompt, binds the returned task id, and waits for destination verification and acknowledgement before the source becomes read-only. Production handoffs do not use `fork_thread`, because forks inherit completed conversation history. `PreCompact` remains a last-resort checkpoint and does not claim to launch a task.

## Repository Contents

- `.codex-plugin/plugin.json`: frozen plugin identity and install metadata
- `.codex-plugin/release-policy.json`: exact machine-readable experimental non-claim policy
- `skills/relay/`: canonical skill, scripts, reference, and examples
- `hooks/`: default plugin lifecycle hooks
- `codex/`: portable Codex CLI/OMX hook adapter
- `install.sh`: compatibility installer for repo-local `.agents` and workflow copies
- `docs/`: installation, lifecycle, architecture, integration, evidence, and release guidance
- `artifacts/`: sanitized samples and historical evidence only

Per [Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins), the default `hooks/hooks.json` is discovered without an explicit manifest field. Plugin command hooks require a one-time trust review. Use `/hooks` for that review in Codex CLI; Codex App uses its own plugin-hook trust prompt. After trust, ordinary Goal Mode operation is automatic.

## Quickstart

```bash
git clone https://github.com/siddvrth/fresh-handoff.git
cd fresh-handoff
bash validate.sh
```

Install into a target repository:

```bash
bash install.sh /absolute/path/to/target-repository
bash audit_install.sh /absolute/path/to/target-repository
```

The installer creates `.agents/skills/relay/` and `scripts/workflow/relay_hook.sh`. Runtime state stays untracked under `.omx/state/relay/`.

When upgrading from the legacy `session-continuity` skill, canonical skill and hook surfaces are fully staged and verified first. One lock-scoped transaction then swaps both canonical surfaces, publishes any verified copy-on-write state import, and archives the active legacy skill. Any failure restores every prior live surface. Legacy runtime bytes and archived skill bytes remain unchanged, but the legacy writer is no longer active, so installed callers cannot bypass the canonical 4096/1024-byte budgets.

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

Static byte/fidelity checks and token-efficiency evidence are separate. Capsule bytes, prompt bytes, and `(bytes+3)//4` proxies are storage/transport diagnostics; not evidence of token or cost savings. A historical 20-pair pre-v2 clean-versus-fork study showed context isolation but did not show lower goal-token use or cost. It is retained as negative baseline evidence, not proof about v2.

Do not claim token or cost improvement until a preregistered v2 study has at least 20 paired runs with one unique task ID per pair, every candidate passed and ready, task-level output-quality non-inferiority, positive median goal-token savings, and the exact sign-test result. Release validation also binds four distinct preregistration paths and hashes, post-freeze run timestamps, repository origin, real control-to-candidate-to-release ancestry, and a deterministic digest of the frozen shipped runtime contract with no candidate-to-release drift. Evidence may be committed after the candidate when that later commit changes evidence only. The public release remains governed by the exact four-field `.codex-plugin/release-policy.json` `experimental_non_claim` contract.

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
