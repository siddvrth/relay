# Changelog

## 0.3.1 - 2026-08-03

- Refreshed the published plugin and Codex App client identity after the context-trigger delivery and distribution cleanup work.
- Prevented existing cached `0.3.0` installations from being mistaken for the current transport implementation.

## 0.3.0 - 2026-07-25

- First public Relay release for Codex App and Codex CLI/OMX.
- Added automatic context-aware clean-task rotation for persistent Goal Mode work, using compatible telemetry first, a bounded transcript fallback second, and fail-open behavior if neither is trustworthy.
- Added bounded, self-contained capsules with exact identity, next-action, resume-validation, and readiness checks.
- Added acknowledgement-gated single-writer ownership, truthful `termination_pending`, and repeatable Relay chaining.
- Added atomic, idempotent install and repair with source/installed parity checks.
- Added deterministic distribution, release verification, integration coverage, and adversarial safety tests.
