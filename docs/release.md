# Release Process

Fresh Handoff remains experimental, but every public release must be reproducible and must separate static correctness from empirical efficiency claims.

## Frozen Identities

- plugin and repository: `fresh-handoff`
- bundled skill: `checkpoint-and-continue`

Do not rename these during routine contract or documentation work.

## Versioning

- patch: documentation, tests, hygiene, or behavior-preserving fixes
- minor: capsule contract, hook behavior, integration, or CLI surface changes
- major: future stable compatibility break

Use `0.x.y` while Codex hook and clean-task behavior are experimental.

## Release Checklist

1. Update canonical runtime source under `skills/`, `hooks/`, or `codex/`.
2. Update docs, examples, sanitized samples, and `CHANGELOG.md` without adding raw runtime state.
3. Confirm every ready example supplies the full critical kernel; label intentionally incomplete examples non-ready.
4. Confirm every resume instruction uses exact path/SHA/source/transfer/goal/revision/nonce/readiness identity, never a “latest” scan.
5. Reinstall generated surfaces when runtime source changed:

   ```bash
   bash install.sh .
   bash audit_install.sh .
   ```

6. Run deterministic gates:

   ```bash
   python3 skills/checkpoint-and-continue/scripts/test_write_handoff.py
   python3 skills/checkpoint-and-continue/scripts/test_transfer_control.py
   python3 skills/checkpoint-and-continue/scripts/test_transfer_integration.py
   python3 scripts/test_release_readiness.py
   python3 scripts/validate_distribution.py
   bash validate.sh
   ```

7. Prove a fresh consumer lifecycle:

   ```bash
   tmp_repo="$(mktemp -d)"
   trap 'rm -rf "$tmp_repo"' EXIT
   git -C "$tmp_repo" init -q
   bash install.sh "$tmp_repo"
   bash audit_install.sh "$tmp_repo"
   bash repair_active_install.sh "$tmp_repo"
   bash audit_install.sh "$tmp_repo"
   bash completion_gate.sh "$tmp_repo"
   ```

8. In Codex CLI, open `/hooks`; prove `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` are loaded and the current hashes are reviewed/trusted. Exercise delivery plus pre/post-ack write denial with a complete session kernel. Any future live evidence must use a fresh parseable timestamp and bind the plugin version, `hooks/hooks.json` SHA-256, and all three adapter SHA-256 values. Missing, stale, or unbound live evidence blocks release acceptance.
9. Run `python3 scripts/check_release_readiness.py` from the intended clean, committed release checkout. This is the only listed gate that requires clean git/release metadata.
10. Confirm one locked install transaction swaps both canonical surfaces, imports legacy state only when appropriate, and archives the active legacy writer; inject a failure and prove every prior live surface is restored. Re-audit cleanly and verify the installed canonical writer enforces 4096/1024-byte budgets.
11. Confirm official event outputs use distinct allowed shapes and `PreToolUse` default-denies unknown/write-capable tools for revoked or control-only actors.
12. Confirm `transfer_control.py` is present in canonical and freshly installed runtime copies and participates in the frozen runtime digest.
13. Confirm release copy describes the `0.30` threshold as experimental and host-dependent.
14. Refresh empirical artifacts only when the preregistered study changed. Preserve failures, blockers, non-ready runs, termination-pending observations, and intervention rows. Never fabricate a V3 artifact.
15. Preserve the exact machine-readable experimental non-claim policy in `.codex-plugin/release-policy.json`; a positive token/cost claim on any public release surface blocks the release even if a disclaimer is also present.
16. Do not claim token or cost improvement unless a bound V3 study passes the separate >=20-unique-task acknowledgement-gated paired gate. V2 remains reproducible prior-schema evidence only. V3 must verify four distinct tracked preregistration artifacts, post-freeze non-future starts, matching origin, real control-to-candidate-to-HEAD ancestry, framed runtime digests with no release drift, exact four-component chain totals, zero passing-candidate post-acknowledgement source activity, lifecycle outcomes/latencies, all seven continuation checks, quality non-inferiority, a candidate-favoring paired median, and the exact sign-test rule. A later evidence-only commit is permitted only while those bindings remain unchanged.
17. Commit, push, and confirm CI from a clean clone before tagging.

## Evidence Policy

Separate the release report into:

- static 4096/1024 byte and critical-field fidelity evidence
- hook load/trust evidence
- session/revision/delivery and migration evidence
- V2 prior-schema evidence and strict V3 aggregate-chain evidence, when available
- output quality and readiness/failure rates
- acknowledgement/source-stop outcomes and latencies, post-acknowledgement activity, duplicate/conflict/retry/pending/intervention observability, and continuation-quality checks
- byte metrics, which are not token proxies

The historical 20-pair clean-versus-fork study is pre-V2 negative baseline evidence. It may support the structural observation that clean tasks shed inherited completed turns, but it does not satisfy the V2 or V3 token/cost claim gates.

Keep only sanitized evidence under `artifacts/`. Do not commit private capsules, raw hook payloads, transcripts, secrets, or workspace-specific paths.

## Rollback

```bash
git revert <release-commit>
bash install.sh .
bash audit_install.sh .
bash completion_gate.sh .
```

Hook failures should fail open so Codex remains usable. Readiness, audit, and release claims remain fail-closed.
