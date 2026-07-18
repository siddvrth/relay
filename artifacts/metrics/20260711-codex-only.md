# Codex-Only Validation Evidence

Date: 2026-07-11

> Historical scope: this snapshot predates the v2 self-contained, session-scoped kernel. It is retained as boundary evidence, not v2 validation.

## Scope

The package supports Codex App and Codex CLI/OMX. Editor-specific hook code, configuration, installation steps, tests, and runtime state were removed because they could not complete the required autonomous fresh-task handoff.

## Package Contract

- Context use at or above 30% writes a continuation capsule.
- `preCompact` forces a capsule even below the threshold.
- Codex App resolves the current project, creates a clean local task with the continuation prompt as its initial prompt, awaits acknowledgement, and stops old-task writes.
- Production handoffs exclude `fork_thread` because forks inherit completed conversation history.
- Codex CLI/OMX can emit capsule and continuation data for Codex orchestration.
- The installed package contains only the Codex skill and Codex hook stub.

## Verification

The release gate is:

```bash
bash validate.sh
bash audit_install.sh .
bash completion_gate.sh .
```

Current command output should be treated as authoritative over this snapshot.
