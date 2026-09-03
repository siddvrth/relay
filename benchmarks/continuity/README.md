# Continuity benchmark

`benchmark.py` runs the six paired, offline fixture tasks described in the
benchmark brief. It uses headless Codex app-server, a fresh `CODEX_HOME`, and a
fresh fixture repository for every arm. Relay runs install an immutable release
reference into a temporary local marketplace; native runs install no Relay
plugin or hooks. The default reference is `v0.6.0`; set
`RELAY_BENCHMARK_PACKAGE_REF` to the exact tag or commit being evaluated.

Run the ordinary ledger-report verification pair first:

```sh
python3 benchmarks/continuity/benchmark.py --pilot
```

After inspecting the pilot summary and confirming the freeze marker, continue
that session with `--all --session-dir PATH`. The six-pair run is sequential by
design. Results under `benchmarks/continuity/results/` are ignored and include
raw rollout telemetry plus JSONL/Markdown summaries.

The frozen run configuration is `gpt-5.6-luna` with medium effort, a 40,000
token automatic-compaction limit, a 1,000,000-token context window,
`workspace-write`, no approvals, and network disabled. Each pair uses one
fixture snapshot, randomizes arm order, and runs sequentially.

A run is valid when the native arm records at least one automatic compaction,
the Relay arm records at least one fresh-thread handoff, and the external
post-run grader completes. Transition counts are reported as outcomes; only
the packet-pipeline fixture is intended to stress repeated transitions.

The hidden grader wrapper is created with mode 700 under `/private/tmp`, only
after all Codex task processes for the run have terminated, and is never copied
into a task repository. Grader paths and expected outputs are absent from task
prompts, grader material is absent from task repositories and task
`CODEX_HOME` directories, and grader paths/secrets are not passed through the
Codex environment. This is an evaluation-isolation protocol, not an
adversarial security guarantee: the current `workspace-write` mode is not
treated as a hard filesystem read-isolation boundary. No outside-root canary
or sandbox-denial requirement is used.
