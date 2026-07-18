# Fresh Handoff Capsule v2

capsule_version: 2
resume_ready: true
session_id: <session-id>
revision: 4
transfer_id: r4-<nonce-hash>
goal_identity: goal:sha256:<sha256>
transfer_nonce: <one-use-nonce>

## Opening Identity Kernel

### Objective

- Finish and validate the scoped package change without output-quality regression.

### Phase / Status

- verification / Runtime is complete; documentation and release evidence are pending.

### Next Unfinished Action

- Run `bash validate.sh` and resolve the first failing check.

### Completion / Stop Condition

- Targeted tests and the full validation gate pass.
- The live hook definitions are loaded and trusted.
- Release copy makes no unsupported token or cost claim.

### Critical Constraints

- Do not add dependencies.
- Do not include transcripts, secrets, or private runtime paths.
- Do not claim token savings before the paired empirical gate passes.

## Supporting State

### Active Task

- Complete documentation parity and run deterministic validation.

### Completed Work

- Implemented the bounded self-contained session kernel.
- Separated revision refresh from prompt delivery deduplication.

### Remaining Work

- Run the full validation gate.
- Capture the live hook load/trust result.

### Decisions

- Cap capsules at 4096 encoded UTF-8 bytes.
- Cap the one transported prompt at 1024 encoded UTF-8 bytes.
- Treat the `0.30` threshold as experimental and host-dependent.

### Blockers / Risks

- The live `/hooks` trust check requires a Codex CLI session.

### Authoritative Files / Symbols

- `skills/checkpoint-and-continue/SKILL.md`
- `skills/checkpoint-and-continue/scripts/write_handoff.py::build_bounded_capsule`
- `skills/checkpoint-and-continue/scripts/context_handoff.py::official_hook_response`

### Validation

- Targeted unit tests passed after the runtime change.
- Full validation has not run after the documentation update.

## Execution / Ownership Close

- Action now: Run `bash validate.sh` and resolve the first failing check.
- Smallest validation: Run the targeted unit tests.
- Source session: <session-id>
- Transfer ID: r4-<nonce-hash>
- Goal identity: goal:sha256:<sha256>
- Revision: 4
- Capsule SHA-256: compare this file with the exact authoritative transport SHA-256
- Nonce: <one-use-nonce>
- Acknowledge only after exact identity and smallest-validation verification.
- The destination becomes sole writer only after exact acknowledgement; before then the source remains authoritative.

## Overflow Evidence

path: <repo-root>/.omx/state/checkpoint-and-continue/sessions/<session-scope>/overflow/<sha256>.json
sha256: <sha256>

## Notes

- This committed artifact is sanitized. A production clean task must use the exact runtime path/SHA/source/transfer/goal/revision/nonce identity from its delivery result and verify `resume_ready:true`; it must not discover a checkpoint by timestamp or “latest” name.
- The continuation prompt is intentionally absent from the capsule and all pointers.
