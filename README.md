# Relay

Relay keeps long-running Codex Goals moving when context gets crowded. At the
default 30% occupancy threshold, its plugin hooks start a genuinely fresh
Codex app-server thread in the same repository, restore the current Goal
objective, and continue with a compact live-repository prompt.

Relay preserves the effective model, personality, approval reviewer/policy,
and equivalent sandbox behavior exposed at the source hook boundary. It also
requests the source effort and summary on the explicit
continuation turn. Provider identity and active permission-profile provenance
are not exposed by the hook payload, so Relay does not claim to preserve them.
It only relays an active Goal; unavailable Goal or settings reads fail open for
a later retry.

The source thread is quiesced through the supported hook responses: its next
prompt is blocked and its tool calls are denied after the destination is real.
The destination is created with `thread/start`, never `thread/fork`, so it does
not inherit the predecessor conversation.

## Install

Relay is a current Codex plugin release candidate. This source checkout does
not publish a marketplace selector. Install it using the real selector from a
marketplace that has packaged this repository; do not substitute a placeholder
name. Development installs are exercised from a temporary local marketplace by
`bash validate.sh`.

Review and trust the bundled hooks in `/hooks` after installation. Hook trust is
controlled by Codex; installing a plugin does not silently grant hook execution.

## Verify

```bash
python3 skills/relay/scripts/test_context_usage.py
python3 skills/relay/scripts/test_relay.py
bash validate.sh
```

The authenticated real smoke installs Relay into an isolated Codex home,
trusts the exact installed hook hashes, and proves automatic A → B → C relay,
real work in B, Goal/settings preservation, and duplicate suppression:

```bash
python3 skills/relay/scripts/smoke_codex_app_transport.py
```

## Behavior

Installed Relay hooks read the latest exact Codex transcript token-count
record exposed by `transcript_path`. Direct `thread/tokenUsage/updated`
notifications are accepted only by the parser's diagnostic/test surface.
Unknown or missing telemetry fails open. One small atomic JSON record plus a
worker outcome record per source thread provide duplicate suppression and
post-launch recovery; no transcript is copied and no manual state seed is
required.

`/compact` keeps the same thread and summarizes earlier conversation. Relay
adds fresh-thread startup overhead in exchange for a new context window and a
short live-repository continuation. Both approaches still require the
destination/current thread to inspect the work and validate the final result.

## Development

Relay uses only the Python standard library and Bash. The package surface is
`hooks/`, `.codex-plugin/`, and `skills/relay/`. Run `bash validate.sh` before
publishing a change. The release policy intentionally makes no token-efficiency
or cost-savings claim.
