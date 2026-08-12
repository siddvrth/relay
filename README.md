# Relay

Relay keeps long-running Codex Goals moving when context gets crowded. At the
default 30% occupancy threshold, its plugin hooks start a genuinely fresh
Codex app-server thread in the same repository, restore the current Goal
objective, and continue with a compact live-repository prompt.

The source thread is quiesced through the supported hook responses: its next
prompt is blocked and its tool calls are denied after the destination is real.
The destination is created with `thread/start`, never `thread/fork`, so it does
not inherit the predecessor conversation.

## Install

Relay is a current Codex plugin. Install it from the marketplace that publishes
this repository:

```bash
codex plugin add relay@<marketplace-name>
```

Review and trust the bundled hooks in `/hooks` after installation. Hook trust is
controlled by Codex; installing a plugin does not silently grant hook execution.

## Verify

```bash
python3 skills/relay/scripts/test_context_usage.py
python3 skills/relay/scripts/test_relay.py
bash validate.sh
```

The real local smoke uses the installed `codex` binary and reports the fresh
destination thread ID, turn ID, repository `cwd`, and restored Goal objective:

```bash
python3 skills/relay/scripts/smoke_codex_app_transport.py
```

## Behavior

Relay prefers the current `thread/tokenUsage/updated` signal. Hook-only hosts
fall back to the latest exact Codex transcript token-count record. Unknown or
missing telemetry fails open. One small atomic JSON record per source thread
is used for duplicate suppression and retry; no transcript is copied and no
manual state seed is required.

`/compact` keeps the same thread and summarizes earlier conversation. Relay
adds fresh-thread startup overhead in exchange for a new context window and a
short live-repository continuation. Both approaches still require the
destination/current thread to inspect the work and validate the final result.

## Development

Relay uses only the Python standard library and Bash. The package surface is
`hooks/`, `.codex-plugin/`, and `skills/relay/`. Run `bash validate.sh` before
publishing a change. The release policy intentionally makes no token-efficiency
or cost-savings claim.
