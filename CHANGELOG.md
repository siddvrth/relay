# Changelog

## 0.4.0 - 2026-08-12

- Rebuilt Relay around current Codex app-server Goal and token-usage APIs.
- Added automatic 30% threshold handoff to a genuinely fresh `thread/start` destination.
- Restored the source Goal objective with `thread/goal/get` and `thread/goal/set`.
- Replaced layered persistence with one atomic continuation record per source thread.
- Added repeated A-to-B-to-C, concurrency, retry, telemetry, clean-install, and real app-server smoke coverage.
- Removed compatibility installers, duplicate adapters, and obsolete release tooling.

## 0.3.1 - 2026-08-03

- Previous development snapshot; superseded by the current plugin-only runtime.
