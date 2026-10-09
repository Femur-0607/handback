# Claude Lead → Codex worker live delegation

Checks recorded on **October 9, 2026** ran on Windows 11 (build 10.0.26200) with Python **3.13.15**. The relay used the app-bundled Codex CLI **0.162.0-alpha.2** (the separate `codex` on PATH reported 0.153.4 and was not used). Claude CLI reported **2.1.292**; the Lead ran in the Claude desktop app's Code tab, whose own build was not recorded. These are observations of those installed builds, not a compatibility guarantee for later versions.

This was ordinary development work, not a scripted test: a Claude Lead delegated real fixes for this repository to Codex workers and reviewed the results. Session and thread identifiers, transcripts, and local paths are omitted.

## Flow exercised

The topology was explicitly set to **Claude Lead → Codex worker** with `use`, replacing an earlier Lead session while no request was open. Three units of work were then delegated one after another, each to its own new worker thread:

| Unit | Work delegated | Outcome |
|---|---|---|
| CI-FLAKY-1 | Fix two intermittent Windows CI failures | Result received, reviewed, acknowledged, committed |
| R3 | Make `doctor` require Codex only when the topology uses it | Result received, reviewed, acknowledged, committed |
| R1 | Keep collected results until publication succeeds | Result received, reviewed, acknowledged, committed |

For every unit:

1. `new --worker codex --file <brief> --no-wait` created a new Codex thread that appeared in the app, submitted the brief once, and returned `accepted` with a request ID, marker, and detached collector PID.
2. The Lead armed `inbox watch --idle-exit 1200` in the Claude Monitor. The worker's result arrived there as a `result` envelope with the original request ID and the Lead address as recipient. Long bodies were truncated in the event; the complete body was read from the envelope's `path`.
3. The Lead reviewed the working-tree diff, re-ran the affected test modules, and acknowledged with `inbox ack --request <id>`.

Re-arming the watcher for the next unit stopped the previous watcher with "a newer watcher for this recipient took over", as documented. One watcher expired at the Monitor's 30-minute limit after its result had been delivered; it was not re-armed because no request was open.

## Recovery hook observation

While results were unacknowledged, the installed Claude `UserPromptSubmit` recovery hook added the pending result envelopes to the Lead's context on the next turn, marked as untrusted data. This was observed for all three results and is the first recorded observation of that hook executing inside the desktop app. Hook trust prompts and the `SessionStart` path were not separately examined.

## Results in hosted CI

The delegated changes were committed and pushed. The hosted **Windows tests** workflow then passed three consecutive runs (the fix commit, the 0.2.3 release commit on `main`, and the `v0.2.3` tag). Before the fixes, six of the previous seven commits had failed that workflow. The **Release** workflow for `v0.2.3` passed its tests, build, and Windows/macOS/Ubuntu wheel smoke checks, and published 0.2.3 to PyPI. Three passing runs reduce, but do not rule out, intermittent failures.

## Not exercised

- Interrupting the Lead or a collector mid-request and recovering with `wait`. Every result in this run arrived through the normal watcher path.
- Automatic skill triggering. The Lead invoked the `handback` skill explicitly.
- Per-role model and reasoning settings on a live turn.
- Antigravity workers, Codex Lead, macOS, and Linux.
- `selftest` against Codex 0.162.0-alpha.2. `doctor` still reports that build as untested against the recorded baseline (0.153.4); this record has not been used to update `VERIFIED_VERSIONS`.
