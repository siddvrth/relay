# Artifact And Privacy Policy

Fresh Handoff preserves only the minimum durable state needed for safe continuation. Runtime artifacts may expose objectives, file paths, validation results, and repository state, so they remain untracked unless deliberately sanitized.

## Commit These

- canonical source under `skills/`, `hooks/`, and `codex/`
- generated workflow adapter only when distribution parity requires it
- product documentation
- generic v2 sample capsules under `artifacts/handoffs/`
- preregistered, sanitized aggregate evidence under `artifacts/metrics/`

## Do Not Commit These

- `.omx/state/relay/` session directories
- `.active-task.json`, `.revision.json`, `.delivery.json`, `.pointer.json`, `.latest.json`, `.active-transfer.json`, `.ownership.json`, `.revoked.json`, transfer journals, or lock files from a live workspace
- content-addressed overflow objects from private work
- raw capsules, transcripts, hook payloads, prompts, or command logs from unrelated projects
- secrets, credentials, tokens, cookies, API keys, or personal data
- private absolute paths, unrelated branch names, or dirty-tree details

## Pointer And Prompt Policy

Tracked examples may show field names, but must not include a live session ID, hash, path, nonce, or prompt. Runtime pointers contain metadata only: capsule path/hash, source/transfer/goal/revision/nonce identity, delivery state, and metrics. They never duplicate capsule or continuation-prompt bodies.

The repo-latest pointer is convenience metadata and cannot authorize cross-session resume. Sanitized examples must demonstrate exact identity verification rather than “read the latest checkpoint.”

## Sanitizing A V2 Capsule

1. Replace repository paths with `<repo-root>` and session identifiers with `<session-id>`.
2. Replace capsule and overflow digests with `<sha256>` unless the digest covers the committed sanitized artifact itself.
3. Preserve `capsule_version`, `resume_ready`, transfer/goal/revision/nonce shapes, edge-structured section names, budget metrics, and readiness outcome.
4. Remove private objective details, raw logs, full diffs, transcripts, and prompt bodies.
5. Keep only generic files/symbols and validation commands.
6. Confirm the sample is independently readable and does not refer to a predecessor or “latest” file.
7. Scan the staged artifact for private paths and credential patterns.

Example scan:

```bash
rg -n -i '/Users/[^/< ]+|/home/[^/< ]+|api[_-]?key|authorization:|bearer [a-z0-9._-]+' artifacts/
```

Review matches manually; words such as “token” legitimately appear in metrics documentation.

## Historical Evidence

Historical artifacts must state which runtime contract produced them. The 2026-07-11 clean-versus-fork study predates the v2 self-contained kernel and failed the token-efficiency gate. Preserve it as negative baseline evidence; do not relabel it as v2 proof.

## Naming Rules

- Use generic objectives, filenames, branches, and commands.
- Use `<repo-root>`, `<session-id>`, `<revision>`, and `<sha256>` placeholders.
- Keep dates only when they identify package evidence.
- Never publish a real user's goal text or runtime overflow merely because it is content-addressed.
