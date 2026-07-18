# Repository Management

Fresh Handoff is managed as a focused runtime package, not as a project-specific export. Keep every file useful to someone installing the package into an unrelated repository.

## Source Of Truth

| Surface | Source of truth | Notes |
| --- | --- | --- |
| Agent skill | `skills/checkpoint-and-continue/` | Edit here first, then run `install.sh .` |
| Codex hook | `codex/` | Keep CLI/OMX shell behavior isolated here |
| Installed runtime copy | `.agents/`, `scripts/workflow/` | Generated locally by install; audit must match source |
| Docs | `README.md`, `docs/`, `skills/checkpoint-and-continue/*.md` | Keep overview, manuals, runtime protocol, and examples separate |
| Evidence | `artifacts/` | Sanitized samples and metrics only |

## Branch And Change Hygiene

- Keep changes small and reviewable.
- Do not mix runtime behavior, docs, and evidence refreshes unless the evidence proves that runtime change.
- Do not commit raw generated capsules, overflow artifacts, session pointers, transfer journals, ownership/revocation records, delivery ledgers, or hook state.
- Do not add repository-specific project names, absolute workspace paths, private branch names, or dirty working-tree dumps.
- Prefer direct scripts and tests over new dependencies.
- Keep install targets auditable with `audit_install.sh`.

## Required Checks

Run these before considering a change ready:

```bash
bash validate.sh
bash audit_install.sh .
bash completion_gate.sh .
rg -n -i '/Users/[^/< ]+|/home/[^/< ]+|api[_-]?key|authorization:|bearer [a-z0-9._-]+' artifacts/ docs/ README.md
```

For documentation-only edits, `bash validate.sh` is still required because docs and artifacts are covered by hygiene tests.

## Pull Request Shape

Every pull request should answer:

- What changed?
- Which Codex surfaces are affected?
- What validation was run?
- Did installed surfaces change and, if so, were they reinstalled?
- Are artifacts sanitized?
- Are goal-mode behavior, independent 4096/1024 byte limits, and the experimental host-dependent `0.30` policy still represented accurately?

Use `.github/PULL_REQUEST_TEMPLATE.md` for the full checklist.

## Issue Triage

Classify incoming work into one of these labels or equivalent buckets:

| Type | Examples |
| --- | --- |
| Runtime behavior | Threshold compatibility, capsule shape, readiness, revision refresh, delivery dedup, resume prompt |
| Codex integration | Codex App thread tools and CLI/OMX hook output |
| Docs | Installation, lifecycle, examples, release notes |
| Validation | Tests, smoke checks, install audit, CI |
| Artifact hygiene | Sanitization, sample capsules, metrics |

## Definition Of Done

A change is done when:

1. The portable source is updated.
2. Installed runtime surfaces match the portable source when relevant.
3. Validation passes.
4. Documentation reflects the behavior.
5. Artifacts are sanitized.
6. Ready examples use a full edge-structured kernel and exact path/SHA/source/transfer/goal/revision/nonce identity; intentionally incomplete examples say `resume_ready:false`.
7. Known limitations are explicit instead of implied.
