# Relay

Relay is a Codex plugin designed as an alternative to [`/compact`](https://learn.chatgpt.com/docs/developer-commands#keep-transcripts-lean-with-compact) for long-running goals. It writes the current work into a small, self-contained capsule so a clean task can continue without copying the old conversation.

## What it does

Relay preserves the facts a new task needs:

- the objective, current phase, status, and completion condition;
- remaining work, constraints, and authoritative files;
- one exact next action and one exact validation command;
- the source, destination, goal, revision, nonce, and capsule digest.

A destination must inspect the live repository and goal, verify the exact capsule identity, and acknowledge before it becomes the sole writer. If the capsule or transport prompt cannot fit its byte limit, Relay stays non-ready and does not switch tasks.

The default context trigger is `0.30` when the host provides compatible usage data. If usage data is missing or malformed, Relay fails open and keeps `PreCompact` as a checkpoint fallback. These are safety rules, not claims about model quality or cost.

## Install and validate

```bash
git clone https://github.com/siddvrth/relay.git
cd relay
bash validate.sh
```

To install the compatibility surfaces into another repository:

```bash
bash install.sh /absolute/path/to/target-repository
bash audit_install.sh /absolute/path/to/target-repository
```

The installer creates `.agents/skills/relay/` and `scripts/workflow/relay_hook.sh` in the target. Runtime state remains untracked under `.omx/state/relay/`. Reinstalling is safe and idempotent; edit the canonical files in this repository, not generated target copies.

## Limits and host behavior

- Capsule: 4096 encoded UTF-8 bytes.
- Transport prompt: 1024 encoded UTF-8 bytes.
- Hosts: Codex App and Codex CLI/OMX.
- Dependencies: Python standard library and Bash.

The plugin hooks are discovered from `hooks/hooks.json`. Review and trust bundled hooks in the host before relying on automatic events. Relay does not use `thread/fork` for production handoffs because a fork keeps the old conversation history.

## Repository layout

- `.codex-plugin/` — plugin identity and release allowlist.
- `hooks/` — Codex plugin lifecycle hooks.
- `codex/` — portable CLI/OMX hook adapter source.
- `skills/relay/` — skill instructions, examples, reference, runtime, and tests.
- `install.sh` and audit/repair scripts — compatibility installation support.

## Documentation

- [Installation](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Lifecycle](docs/lifecycle.md)
- [Codex integrations](docs/integrations.md)
- [Skill protocol](skills/relay/SKILL.md)
- [CLI and hook reference](skills/relay/reference.md)
- [Examples](skills/relay/examples.md)

## License

Relay is available under the [MIT License](LICENSE).
