# Installation

## Requirements

- Python 3.10 or newer
- Bash
- Git
- Codex App or Codex CLI/OMX

Relay is Codex-only. Clean-task automation requires a Codex surface that exposes thread tools.

## Validate The Package

```bash
git clone https://github.com/siddvrth/fresh-handoff.git
cd fresh-handoff
bash validate.sh
```

The clone is a plugin root with `.codex-plugin/plugin.json`, canonical `skills/`, and default `hooks/hooks.json`.

## Install Into A Repository

```bash
bash /path/to/fresh-handoff/install.sh /absolute/path/to/target-repository
bash /path/to/fresh-handoff/audit_install.sh /absolute/path/to/target-repository
```

Target paths may contain spaces. The installer creates:

| Path | Purpose |
| --- | --- |
| `.agents/skills/relay/` | Generated canonical skill copy |
| `scripts/workflow/relay_hook.sh` | Generated Codex CLI/OMX adapter |
| `.omx/state/relay/` | Untracked, session-scoped runtime state as needed |

Canonical source remains `skills/relay/`; generated `.agents` and workflow copies should be repaired by reinstalling, not edited independently. The complete skill copy includes the V2/V3 telemetry reporters and their tests; installation adds no telemetry dependency.

After installation, seed a complete critical kernel using [the ready-session example](../skills/relay/examples.md#seed-a-ready-session). A partial objective/next-action seed will correctly remain non-ready.

## Review And Trust Hooks

Plugin command hooks are not trusted merely because the plugin is installed or enabled. Current [Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins) says the default `hooks/hooks.json` is discovered automatically and that bundled hooks require review/trust.

Before relying on automatic events:

1. Install or enable the Relay plugin.
2. In Codex CLI only, open `/hooks`. In Codex App, use the plugin-hook trust prompt presented by the host.
3. Confirm the Relay hook source and definitions are loaded.
4. Review and trust the current hook hashes once.
5. Recheck after a hook change, because trust is tied to the definition hash.
6. Exercise `UserPromptSubmit`, `PreToolUse`, `PreCompact`, and `Stop` with a fully seeded transfer and retain sanitized release evidence.

After this one-time setup, normal `/goal` work requires no manual handoff.

Release evidence in `artifacts/metrics/live-hooks-trust.json` is valid only for 24 hours and must bind `schema_version:1`, `evidence_type:"codex_live_hooks_trust"`, `checked_via:"/hooks"`, all four hook events, the current plugin version, the SHA-256 of `hooks/hooks.json`, and SHA-256 values for the plugin, Codex, and workflow adapters.

An unavailable live `/hooks` check is a release-blocking validation gap, not a silent pass.

## Upgrade From Legacy Skills

The installer treats `.agents/skills/session-continuity`, `.agents/skills/checkpoint-and-continue`, and their matching `.omx/state/*` trees as legacy state:

- the active legacy skill, including its writer, is moved to `.agents/archived-skills/`, outside the active namespace
- the legacy runtime tree is left byte-identical
- the newest valid legacy checkpoint is imported copy-on-write only when a newer canonical checkpoint is absent
- imported bytes are verified and accompanied by checksum/provenance
- a newer canonical checkpoint always wins
- the complete canonical skill, including V3 telemetry validation/reporting, and hook copies are staged, byte-compared, compiled, and syntax-checked before any live change
- one install lock covers both canonical swaps, verified migration publication, and legacy-skill archival
- failure at any transaction step restores the prior canonical skill, hook, imported state, and active legacy skill
- repeated install is locked and idempotent; after success, only the canonical writer remains active and enforces the 4096-byte capsule and 1024-byte prompt budgets

`audit_install.sh` fails while `.agents/skills/session-continuity` or `.agents/skills/checkpoint-and-continue` remains active.

Older releases also created an unsupported editor hook. The installer removes the generated hook file; if user-owned `.cursor/hooks.json` still references `relay-gate.mjs`, remove that stale entry separately.

## Repair And Re-Audit

```bash
bash /path/to/fresh-handoff/repair_active_install.sh /absolute/path/to/target-repository
bash /path/to/fresh-handoff/audit_install.sh /absolute/path/to/target-repository
```

Repair reinstalls canonical surfaces, applies the idempotent migration path, audits drift/conflicts, and runs package validation.

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

This proves portable install and repair behavior, including installed V2/V3 telemetry parser parity. It does not create empirical evidence and does not replace the live `/hooks` load/trust check.

## Uninstall

After preserving any required runtime evidence:

```bash
rm -rf .agents/skills/relay
rm -f scripts/workflow/relay_hook.sh
rm -rf .omx/state/relay
```

Uninstall does not delete archived legacy skill copies or the untouched `.omx/state/session-continuity` source.
