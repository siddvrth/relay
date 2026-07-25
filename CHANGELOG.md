# Changelog

## Unreleased

## 0.3.0 - 2026-07-25

- Added acknowledgement-gated single-writer transfer state with exact source/destination/goal/revision/SHA-256/nonce binding, replay-safe ownership publication, and truthful `termination_pending` fallback.
- Added edge-structured capsules with an opening identity kernel, compact supporting middle, and closing execution/ownership block; the bounded prompt carries the exact final capsule SHA-256 because a capsule cannot embed its own digest.
- Added `PreToolUse` authority enforcement across plugin, CLI, and installed adapters and bound `transfer_control.py` into release-runtime digests.
- Added the v2 self-contained, session-scoped handoff kernel with independent 4096-byte capsule and 1024-byte prompt budgets.
- Separated revision refresh from transport delivery deduplication; session-scoped locks, revisions, and delivery ledgers no longer conflate checkpoint freshness with prompt cooldown.
- Added structural readiness guards and fail-closed `resume_ready:false` behavior for missing critical state or critical byte overflow.
- Added content-addressed overflow artifacts for optional evidence and safe critical-overflow metadata.
- Split internal orchestration JSON from event-specific official `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` hook envelopes.
- Clarified that the experimental 30%-used trigger depends on optional host telemetry because official `UserPromptSubmit` does not document a context ratio; `PreCompact` is the deterministic fallback.
- Made canonical skill/hook publication, copy-on-write legacy-state migration, and active legacy-skill archival one locked rollback transaction; the retired legacy writer can no longer bypass canonical byte budgets.
- Replaced ambiguous “latest checkpoint” resume guidance with exact path/SHA/source/transfer/goal/revision/nonce/readiness verification.
- Separated static byte/fidelity gates from the preregistered 20-pair goal-token gate and prohibited token or cost improvement claims until quality, statistical, timestamp, Git ancestry/origin, runtime-digest, and tracked preregistration-artifact requirements pass.

## 0.2.0 - 2026-07-12

- Packaged the workflow as a canonical Codex plugin with bundled skill and lifecycle hooks.
- Moved canonical skill source to `skills/checkpoint-and-continue/` and added distribution validation.
- Added path-with-spaces and plugin-hook portability smoke coverage.
- Replaced inherited-history `fork_thread` handoffs with clean local project tasks created through `create_thread`.
- Added an explicit `handoff_mode: clean_task` orchestration contract and regression coverage.
- Added paired goal-mode telemetry reporting, cost-sensitivity analysis, and an empirical improvement gate.
- Expanded telemetry to 20 valid pairs and documented the negative result: clean tasks shed inherited history but did not reduce goal-token use or sensitivity-estimated cost.
- Added an exact paired sign test and machine-readable sanitized trial data.
- Restricted the package to Codex App and Codex CLI/OMX.
- Removed the Cursor hook, hook configuration, installer paths, lifecycle tests, Node requirement, and Cursor documentation.
- Made the bundled test harness relocatable so it runs correctly from both portable source and `.agents/skills/checkpoint-and-continue` installs.
- Added a fresh-installed test-suite run to `validate.sh`; package-only artifact checks skip when portable source is not present.
- Scoped cooldown and deduplication state per session so continuation generations do not suppress one another.
- Made hook delivery one-shot across threshold, `stop`, `preCompact`, and manual checkpoint events.
- Added concurrency locking and atomic pointer writes for simultaneous hook events.
- Corrected official percent-field parsing so `context_usage_percent: 1` means 1%.
- Added autonomous Codex App, goal-mode, perpetual-loop, and host-boundary acceptance criteria.
- Expanded validation with concurrency and autonomous Codex handoff coverage.
- Added repository management, architecture, and release documentation.
- Added GitHub issue, pull request, and CI validation surfaces.
- Clarified source-of-truth boundaries between portable source, installed surfaces, and runtime state.
- Updated validation baseline to the current 28-test suite.
- Restructured documentation into a standalone project README plus focused `docs/` manuals.
- Added artifact and privacy policy, contribution guidance, and security notes.
- Released the project under the MIT License.
- Kept committed artifacts sanitized and general-purpose.

## 0.1.0

- Added 30% context handoff policy.
- Added Markdown continuation capsules.
- Added active-task seeding.
- Initially included an editor hook gate; removed in the Codex-only product revision.
- Added Codex CLI/OMX hook stub.
- Added validation script and stdlib tests.
