# Security

Fresh Handoff writes local checkpoint artifacts. Treat those artifacts as sensitive until sanitized.

## Supported Surface

- `skills/relay/scripts/write_handoff.py`
- `skills/relay/scripts/context_handoff.py`
- `skills/relay/scripts/transfer_control.py`
- `.codex-plugin/plugin.json`
- `hooks/hooks.json`
- `hooks/relay_hook.sh`
- `codex/relay_hook.sh`
- `install.sh`
- `audit_install.sh`
- `completion_gate.sh`

## Reporting

Use GitHub private vulnerability reporting when it is enabled on the public repository. Until then,
report security concerns privately to the repository owner; do not disclose vulnerabilities in a
public issue.

## Data Handling

Do not place these in committed artifacts:

- API keys or tokens
- cookies or session credentials
- private keys
- full transcripts
- private absolute paths from unrelated repositories
- raw dirty working-tree state from unrelated repositories

The scripts do not intentionally read environment variables beyond documented relay defaults.

## Security Model

Fresh Handoff is a local workflow tool. It should not transmit checkpoint contents to external services. Host tools may display continuation prompts or route them to a new session, but this package treats generated capsules as local sensitive files until explicitly sanitized.

Before a transfer crosses acknowledgement, malformed checkpoint input retains the documented fail-open behavior so it does not block ordinary host work. Once a valid source revocation tombstone or destination ownership record exists, authority ambiguity fails closed for the affected writer. `PreToolUse` and prompt blocking are defense-in-depth only; they are not an operating-system write ACL.

Exact acknowledgement is the ownership boundary. It durably revokes the source and publishes destination ownership before stop work. Delivery, task creation, thread text, visible archive, or a claimed close never substitutes for acknowledgement or proves quiescence. Stop success is accepted only with a supported capability/result pair and durable adapter evidence; otherwise the source remains read-only with `termination_pending`.
