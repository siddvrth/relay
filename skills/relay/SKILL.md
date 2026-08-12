---
name: relay
description: Keep long-running Codex Goals moving by starting a fresh app-server thread at 30% context usage and continuing from a compact live-repository handoff.
---

# Relay

Relay is a Codex-only plugin for long-running Goal Mode work. Its bundled hooks
check context before prompts and tools. The default threshold is `0.30` and can
be changed for tests or advanced local use with `RELAY_THRESHOLD`.

## Automatic flow

When current occupancy reaches the threshold, Relay:

1. Reads the source Goal with `thread/goal/get` when the app-server exposes it.
2. Builds a short continuation from the Goal objective, current request, live
   changed-file hint, repository path, and next action.
3. Starts `codex app-server --stdio`, calls `thread/start` with the same
   repository `cwd`, restores the objective with `thread/goal/set`, and calls
   `turn/start`.
4. Returns the destination thread and turn IDs only after the fresh destination
   exists. The source prompt is blocked and source tool calls are denied after
   that point, so the source becomes quiescent through the supported hook API.

The production path never calls `thread/fork`. A fork retains completed history;
Relay needs a genuinely fresh context. The destination receives the same plugin
hooks when the plugin is installed and trusted by Codex.

## Continuation state

Each source thread gets one small atomic record under:

```text
.omx/state/relay/<source-id-hash>.json
```

It contains the source and destination IDs, repository path, Goal objective,
threshold observation, changed-file hint, and next action. A sibling lock file
serializes duplicate hooks. There is no transcript copy, revision chain,
distributed journal, manual seed, or persistent handoff document. Failed
launches remain fail-open and can retry on the next eligible event.

## Telemetry

The preferred signal is the current app-server
`thread/tokenUsage/updated` notification: `tokenUsage.last.totalTokens` divided
by `modelContextWindow`, adjusted for the current TUI's 12,000-token baseline.
For hooks that only provide `transcript_path`, Relay reads the latest exact
`event_msg` / `token_count` record and uses
`last_token_usage.total_tokens`. Missing, malformed, or unfamiliar telemetry
returns control to Codex without starting a thread.

## `/compact` comparison

`/compact` summarizes earlier turns inside the same thread. Relay pays the
small cost of a new app-server thread and a compact continuation prompt to
restore a fresh context window and the Goal objective. Relay preserves live
repository state rather than shipping a transcript; `/compact` preserves more
conversation context but remains subject to the same active window. Neither
mechanism guarantees final correctness: the destination must inspect, implement,
and validate the live work.

## Install and verify

Install Relay from the marketplace that contains this plugin:

```bash
codex plugin add relay@<marketplace-name>
```

Then review and trust the bundled hooks in `/hooks`. A quick local check is:

```bash
python3 skills/relay/scripts/test_context_usage.py
python3 skills/relay/scripts/test_relay.py
```

For a real local app-server smoke, run
`python3 skills/relay/scripts/smoke_codex_app_transport.py` in an authenticated
Codex environment. The smoke reports the destination IDs, same `cwd`, and
restored Goal objective.
