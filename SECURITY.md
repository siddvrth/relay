# Security

Relay writes one local continuation record per source thread. Treat it as sensitive until removed.

## Supported Surface

- `skills/relay/scripts/relay.py`
- `skills/relay/scripts/codex_app_transport.py`
- `skills/relay/scripts/codex_app_protocol.py`
- `.codex-plugin/plugin.json`
- `hooks/hooks.json`
- `hooks/relay_hook.sh`

## Reporting

Use GitHub private vulnerability reporting when it is enabled on the public repository. Until then,
report security concerns privately to the repository owner; do not disclose vulnerabilities in a
public issue.

## Data Handling

Do not place these in committed artifacts:

- API keys or tokens
- cookies or session credentials
- private keys
- full transcripts or continuation prompts
- private absolute paths from unrelated repositories
- raw dirty working-tree state from unrelated repositories

The scripts read only documented Relay settings and the normal Codex environment needed by
`codex app-server`.

## Security Model

Relay passes the compact continuation to the local Codex app-server process selected by the user's
normal Codex configuration. It does not copy a transcript or send state to an unrelated service.
Hook-state and filesystem inspection fail open. Source blocking and tool denial are defense-in-depth
hook responses, not operating-system write ACLs; users should still review hook trust and repository
permissions in Codex.
