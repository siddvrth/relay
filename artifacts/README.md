# Artifacts

This directory contains sanitized evidence for the Fresh Handoff project. It should never contain raw project transcripts, secrets, private repository paths, or host-specific runtime state.

## Contents

- `metrics/20260711-codex-only.md` - historical pre-v2 evidence for the Codex-only package boundary.
- `metrics/20260711-fork-vs-clean.md` - historical pre-v2 evidence comparing inherited fork history with a clean task; it failed the token-efficiency gate.
- `metrics/fresh-handoff-v2-five-pair-pilot-preregistration.json` - frozen five-pair pilot structure with unpopulated results; it is directional, claim-ineligible, and not a completed V2/V3 study.
- `metrics/fresh-handoff-v2-pilot-environment-limitations.json` - literal capability probes showing why the five-pair pilot is not executable in the current environment and cannot unlock token or cost claims.
- `handoffs/sanitized-sample-handoff.md` - a generic v2 capsule showing the bounded, self-contained artifact shape.

Raw handoff capsules from unrelated projects are intentionally excluded because they can include repository paths, branch names, dirty working-tree state, and project-specific filenames. Runtime active-task, revision, delivery, pointer, transfer journal, ownership, revocation, latest, lock, and overflow files are also omitted because they describe one live workspace's transient state.
