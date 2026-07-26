# Documentation

Fresh Handoff documentation is organized by operator job:

| Document | Use it when |
| --- | --- |
| [Installation](installation.md) | You need to install the kit into this repo or another repo |
| [Architecture](architecture.md) | You need the component model and runtime data flow |
| [Acceptance criteria](acceptance-criteria.md) | You need the continuity promises mapped to Codex evidence and boundaries |
| [Handoff lifecycle](lifecycle.md) | You need the seed, checkpoint, handoff, and resume process |
| [Integrations](integrations.md) | You are wiring Codex App or Codex CLI/OMX |
| [Repository management](repository-management.md) | You are changing repo structure, docs, CI, artifacts, or ownership |
| [Release process](release.md) | You are preparing a tagged package revision |
| [Artifact and privacy policy](artifacts.md) | You are deciding what can be committed |
| [Validation and metrics](metrics.md) | You need proof commands or the current evidence baseline |

The runtime skill protocol lives in [`../skills/relay/SKILL.md`](../skills/relay/SKILL.md). Low-level flags and JSON contracts live in [`../skills/relay/reference.md`](../skills/relay/reference.md). Copy-paste examples live in [`../skills/relay/examples.md`](../skills/relay/examples.md).

## Documentation Standard

- Put user-facing setup and operations in `docs/`.
- Put agent runtime instructions in `skills/relay/`.
- Put sanitized proof in `artifacts/`.
- Keep README short enough to scan, but complete enough to orient a new operator.
- Update validation evidence when behavior or test counts change.
