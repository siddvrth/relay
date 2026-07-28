# Changelog

## 0.3.0 - 2026-07-25

- First public Relay release for Codex App and Codex CLI/OMX.
- Added acknowledgement-gated single-writer transfer state with exact source/destination/goal/revision/SHA-256/nonce binding, replay-safe ownership publication, and truthful `termination_pending` fallback.
- Added edge-structured capsules with an opening identity kernel, compact supporting middle, and closing execution/ownership block; the bounded prompt carries the exact final capsule SHA-256 because a capsule cannot embed its own digest.
- Added the Codex App launch envelope that records one stable create intent, starts exactly one clean `create_thread` task with the existing bounded prompt, and leaves the configurable threshold host-limited when context telemetry is absent.
- Added `PreToolUse` authority enforcement across plugin, CLI, and installed adapters and bound `transfer_control.py` into release-runtime digests.
- Added the v2 self-contained, session-scoped handoff kernel with independent 4096-byte capsule and 1024-byte prompt budgets.
- Separated revision refresh from transport delivery deduplication; session-scoped locks, revisions, and delivery ledgers no longer conflate checkpoint freshness with prompt cooldown.
- Added structural readiness guards and fail-closed `resume_ready:false` behavior for missing critical state or critical byte overflow.
- Added content-addressed overflow artifacts for optional evidence and safe critical-overflow metadata.
- Split internal orchestration JSON from event-specific official `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` hook envelopes.
- Clarified that the experimental 30%-used trigger prefers compatible host telemetry and otherwise uses a bounded `transcript_path` token-count fallback; `PreCompact` remains the deterministic last-resort checkpoint.
- Made canonical skill/hook publication one locked rollback transaction so reinstall is atomic and idempotent.
- Replaced ambiguous “latest checkpoint” resume guidance with exact path/SHA/source/transfer/goal/revision/nonce/readiness verification.
- Separated static byte/fidelity gates from the preregistered paired goal-token gate and prohibited token or cost improvement claims until quality, statistical, timestamp, Git ancestry/origin, runtime-digest, and tracked preregistration-artifact requirements pass.
- Packaged the workflow as a canonical Codex plugin with bundled skill, lifecycle hooks, documentation, GitHub contribution surfaces, and MIT licensing.
