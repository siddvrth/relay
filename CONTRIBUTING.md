# Contributing

Fresh Handoff is small on purpose. Contributions should preserve that shape.

## Principles

- Keep the package portable across repositories.
- Prefer standard library Python and small Bash wrappers.
- Do not add host-specific assumptions to the core scripts.
- Keep raw runtime state out of git.
- Validate before claiming completion.

## Local Workflow

```bash
bash validate.sh
bash audit_install.sh .
bash completion_gate.sh .
rg -n -i 'private-project-name|/Users/.+/Desktop|secret|token|api[_-]?key' .
```

When changing installed surfaces, edit the portable source first, then run:

```bash
bash install.sh .
bash audit_install.sh .
bash validate.sh
bash completion_gate.sh .
```

## Documentation Rules

- README is the front door.
- `docs/` contains user-facing manuals.
- `skills/relay/` contains the runtime skill protocol, references, and scripts.
- `artifacts/` contains sanitized evidence only.
- `.github/` contains contribution intake and validation wiring.

Do not commit raw handoff capsules from unrelated projects.

## Review Checklist

- Does the change preserve 30% threshold behavior?
- Does `preCompact` still force a checkpoint?
- Does goal-mode text remain preserved without promising goal object transfer?
- Does one session deduplicate without suppressing the next session's handoff?
- Do threshold, manual, and `preCompact` paths emit no more than one follow-up?
- Are installed surfaces updated when portable source changes?
- Are docs and examples aligned with the new behavior?
- Are all artifacts sanitized?
