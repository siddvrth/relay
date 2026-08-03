# Installation

## Requirements

- Python 3.10 or newer
- Bash
- Git
- Codex App or Codex CLI/OMX

Relay is Codex-only. Clean-task automation requires a Codex surface that exposes thread tools.

## Validate The Package

```bash
git clone https://github.com/siddvrth/relay.git
cd relay
bash validate.sh
```

The clone is a plugin root with `.codex-plugin/plugin.json`, canonical `skills/`, and default `hooks/hooks.json`.

## Install Into A Repository

```bash
bash /path/to/relay/install.sh /absolute/path/to/target-repository
bash /path/to/relay/audit_install.sh /absolute/path/to/target-repository
```

Target paths may contain spaces. The installer creates:

| Path | Purpose |
| --- | --- |
| `.agents/skills/relay/` | Generated canonical skill copy |
| `scripts/workflow/relay_hook.sh` | Generated Codex CLI/OMX adapter |
| `.omx/state/relay/` | Untracked, session-scoped runtime state as needed |

Canonical source remains `skills/relay/`; generated `.agents` and workflow copies should be repaired by reinstalling, not edited independently.

Canonical skill and hook surfaces are fully staged, byte-compared, compiled, and syntax-checked before any live change. One install lock covers both canonical swaps. Failure at any transaction step restores the prior skill and hook. Repeated install is idempotent and enforces the 4096-byte capsule and 1024-byte prompt budgets on the installed writer.

After installation, seed a complete critical kernel using [the ready-session example](../skills/relay/examples.md#seed-a-ready-session). A partial objective/next-action seed will correctly remain non-ready.

## Review And Trust Hooks

Plugin command hooks are not trusted merely because the plugin is installed or enabled. Current [Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins) says the default `hooks/hooks.json` is discovered automatically and that bundled hooks require review/trust.

Before relying on automatic events:

1. Install or enable the Relay plugin.
2. In Codex CLI only, open `/hooks`. In Codex App, use the plugin-hook trust prompt presented by the host.
3. Confirm the Relay hook source and definitions are loaded.
4. Review and trust the current hook hashes once.
5. Recheck after a hook change, because trust is tied to the definition hash.
6. Exercise `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` with a fully seeded transfer.

After this one-time setup, normal `/goal` work requires no manual handoff.

## Repair And Re-Audit

```bash
bash /path/to/relay/repair_active_install.sh /absolute/path/to/target-repository
bash /path/to/relay/audit_install.sh /absolute/path/to/target-repository
```

Repair reinstalls canonical surfaces, audits drift, and runs package validation.

## Temporary Consumer Proof

From the package root:

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

This proves portable install and repair behavior. It does not replace the live `/hooks` load/trust check.

## Uninstall

After saving any runtime state you need:

```bash
rm -rf .agents/skills/relay
rm -f scripts/workflow/relay_hook.sh
rm -rf .omx/state/relay
```
