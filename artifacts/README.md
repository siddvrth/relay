# Artifacts

This directory contains sanitized evidence for Relay. It should never contain raw project transcripts, secrets, private repository paths, or host-specific runtime state.

## Contents

- `handoffs/sanitized-sample-handoff.md` - a generic v2 capsule showing canonical `next_action`, exact `resume_validation`, historical `validation_evidence`, optional absence, and the bounded self-contained artifact shape.
- `metrics/` - reserved for future sanitized evaluation evidence. No committed pilot or baseline metric files ship in the public tree today.

Raw handoff capsules from unrelated projects are intentionally excluded because they can include repository paths, branch names, dirty working-tree state, and project-specific filenames. Runtime active-task, revision, delivery, pointer, transfer journal, ownership, revocation, latest, lock, and overflow files are also omitted because they describe one live workspace's transient state.

Any capsule/prompt bytes, duplicate-marker counts, or `(bytes+3)//4` proxies associated with these artifacts are storage/transport diagnostics; not evidence of token or cost savings.
