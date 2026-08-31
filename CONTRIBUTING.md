# Contributing

Relay is small on purpose. Contributions should preserve that shape.

## Principles

- Keep the package portable across repositories.
- Prefer standard library Python and small Bash wrappers.
- Keep the core scripts focused on the documented Codex CLI contract.
- Keep raw runtime state out of git.
- Validate before claiming completion.

## Local Workflow

```bash
bash validate.sh
rg -n -i 'private-project-name|/Users/.+/(private-path|private-project)|secret|token|api[_-]?key' .
```

## Documentation Rules

- README is the front door.
- `skills/relay/SKILL.md` contains the concise runtime contract.
- Focused tests live beside the standard-library runtime scripts.
- `.github/` contains contribution intake and validation wiring.

Do not commit raw transcripts, continuation state, or private paths from unrelated projects.

## Review Checklist

- Does only `PreCompact(auto)` launch while manual `/compact` remains native?
- Does a successful destination use `thread/start`, restore the Goal, and avoid `thread/fork`?
- Does one source thread deduplicate while A-to-B-to-C remains repeatable?
- Do malformed hook data, unavailable state, and failed launches fail open?
- Does `bash validate.sh` exercise the clean plugin install path?
