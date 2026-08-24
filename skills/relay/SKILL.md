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
2. Builds a bounded continuation from the Goal objective, recent predecessor
   progress/decisions/constraints/validation, live changed-file hint,
   repository path, and next action.
3. Starts `codex app-server --stdio`, calls `thread/start` with the same
   repository `cwd` and directly exposed thread settings, names the destination
   `Relay II: <original title>` (or the next deterministic sequence), restores
   and verifies the Goal with `thread/goal/set`, then calls `turn/start`.
4. Returns the destination thread and turn IDs only after the fresh destination
   exists and any required presentation proof has arrived. Goal-control,
   cancellation, and shutdown paths bypass quiescence; `SessionEnd` cleans
   detached workers.
   Ordinary repeated no-progress observations stop at the circuit-breaker
   limit instead of creating an unbounded chain.

The production path never calls `thread/fork`. A fork retains completed history;
Relay needs a genuinely fresh context. The destination receives the same plugin
hooks when the plugin is installed and trusted by Codex.

Relay preserves the hook-visible model, personality, approval reviewer/policy,
and equivalent sandbox behavior. It requests effort and summary on the explicit
continuation turn. Hook payloads do not expose provider identity or active
permission-profile provenance, so Relay makes no preservation claim for those
fields.

## Continuation state

Each source thread gets one small atomic record under:

```text
.omx/state/relay/<source-id-hash>.json
```

It contains the source and destination IDs, repository path, Goal objective,
threshold observation, changed-file hint, next action, stable chain ID, Relay
sequence, original title, progress fingerprint, and presentation status. A
sibling lock file serializes duplicate hooks. Goal/settings read failures and
pre-ack launch or presentation failures fail open. The worker writes a completed
outcome and closes its app-server when the destination Goal becomes terminal or
the destination acknowledges its own successor; `SessionEnd` remains a fallback
that kills detached worker process groups and persists cancellation outcomes.

Desktop success requires an external exact presentation proof. A persisted
thread, `thread/read`, or `thread/loaded/list` is not proof that the Desktop
window selected the conversation. The proof is bound to the source and
destination threads, destination turn, chain, and Relay sequence.

## Telemetry

Installed hooks use `transcript_path`: Relay reads the latest exact `event_msg`
/ `token_count` record and uses
`last_token_usage.total_tokens`. Missing, malformed, or unfamiliar telemetry
returns control to Codex without starting a thread.

The parser also accepts a direct `thread/tokenUsage/updated` notification for
diagnostics and focused tests. That notification is not the installed hook's
production input path.

## `/compact` comparison

`/compact` summarizes earlier turns inside the same thread. Relay pays the
small cost of a new app-server thread and a compact continuation prompt to
restore a fresh context window and the Goal objective. Relay preserves live
repository state rather than shipping a transcript; `/compact` preserves more
conversation context but remains subject to the same active window. Neither
mechanism guarantees final correctness: the destination must inspect, implement,
and validate the live work.

## Install and verify

This source checkout is a release candidate and does not advertise a fabricated
marketplace selector. Use the actual selector from the marketplace that
packages it. `bash validate.sh` verifies a clean temporary marketplace install.

Then review and trust the bundled hooks in `/hooks`. A quick local check is:

```bash
python3 skills/relay/scripts/test_context_usage.py
python3 skills/relay/scripts/test_relay.py
```

For an authenticated installed A → B → C smoke, run
`python3 skills/relay/scripts/smoke_codex_app_transport.py` in an authenticated
Codex environment. The smoke proves real work in B, B relaying to C, preserved
Goal/settings, and exactly three isolated app-server threads.
