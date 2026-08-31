# Changelog

## 0.7.0 - Unreleased

- Narrowed Relay's supported product surface to interactive Codex CLI and
  `codex exec`; unsupported roots now fail open generically.
- Removed the source/presentation compatibility path and retained local
  app-server stdio only as the internal continuation mechanism.
- Added coverage for `exec` roots, generic unsupported roots, and Relay-owned
  successors whose internal source metadata is not a user-originated source.

## 0.6.1 - 2026-08-30

- Prevented Relay-generated continuation prompts from recursively becoming the
  next current request, while preserving the original request across a chain.
- Made no-progress detection follow repository state instead of volatile
  assistant prose, so tight compaction thresholds fail open instead of looping.
- Added regression coverage for recursive prompt and repeated-transition safety.

## 0.6.0 - 2026-08-27

- Added explicit Python 3.10+ hook detection with an actionable fail-open diagnostic,
  documented the validated Codex surface and local-state removal, and made the
  real smoke model configurable.
- Replaced token-threshold triggering with the supported auto-only `PreCompact` boundary.
- Stopped source compaction only after a fresh destination Goal and explicit turn are verified; failures now defer to native compaction.
- Preserved manual `/compact` and converted prompt/tool hooks to post-handoff ownership guards.
- Removed token telemetry and the unsupported Desktop presenter/deep-link bridge; Desktop now fails open without creating an invisible successor.
- Restored Goals paused before the explicit continuation turn, then reactivated them to avoid racing host-owned Goal continuation.
- Corrected named permission-profile preservation, stale-state validation, and circuit-breaker behavior so Relay never blocks the Goal.

## 0.5.0 - 2026-08-23

- Fixed worker completion after a destination acknowledges its successor, without completing the predecessor Goal or interrupting its turn.
- Added control-operation bypasses, terminal worker cleanup, and a repeated-no-progress circuit breaker.
- Added deterministic Relay chain identity, sequence names, and original-title preservation.
- Added optional exact Desktop presentation proof.

## 0.4.1 - 2026-08-14

- Bumped the plugin and app-server client identity after the post-0.4.0 runtime fixes so Codex installs a fresh cached package instead of reusing stale 0.4.0 hooks.
- Added a release guard so future runtime changes cannot land without a plugin version bump.

## 0.4.0 - 2026-08-12

- Rebuilt Relay around current Codex app-server Goal and token-usage APIs.
- Added automatic 30% threshold handoff to a genuinely fresh `thread/start` destination.
- Restored the source Goal objective with `thread/goal/get` and `thread/goal/set`.
- Replaced layered persistence with one atomic continuation record per source thread.
- Added repeated A-to-B-to-C, concurrency, retry, telemetry, clean-install, and real app-server smoke coverage.
- Removed compatibility installers, duplicate adapters, and obsolete release tooling.

## 0.3.1 - 2026-08-03

- Previous development snapshot; superseded by the current plugin-only runtime.
