# Changelog

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
