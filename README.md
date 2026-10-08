<p align="center"><img src="https://raw.githubusercontent.com/Femur-0607/handback/main/docs/assets/handback-logo.png" alt="handback" width="420"></p>

https://github.com/user-attachments/assets/b9927ffd-ec73-4980-a128-65842894e269

# handback

Previously developed as **agent-relay**; legacy state is detected with migration guidance.

A local tool for handing work between coding-agent apps and bringing the results back to the conversation where you started.

**Experimental · Windows verified · Python 3.10+ · One computer, one OS user**

[First task](#try-your-first-task) · [Use-your-own-project guide](#use-it-on-your-own-project) · [Detailed setup](https://github.com/Femur-0607/handback/blob/main/docs/quickstart.md) · [한국어 사용 설명서](https://github.com/Femur-0607/handback/blob/main/docs/usage.ko.md)

## What is handback?

handback connects coding agents running on your computer. You work with one main conversation, called the **Lead**, and let it send a defined task to another conversation, called a **worker**. The relay creates the worker conversation, sends the task, and saves its reply in a local inbox for the Lead to review.

For example, you can discuss a change with Claude, have a Codex worker inspect the relevant code, and review its findings back in Claude. The apps perform the reasoning and coding; handback handles task delivery, result collection, and recovery.

It runs as an installed Python package or directly from this repository, using only Python's standard library. You need the supported agent apps installed and signed in. Everything is scoped to one computer and one OS user, and no other project repository is required.

## When to use it / when not to

Use it to hand bounded tasks between **different agent apps** when results must survive interruptions without duplicate submission. Within one app, prefer its built-in subagents. This tool is not for teams, multiple machines, or parallel writes to one checkout.

Delegating heavy reading, investigation, and implementation to workers helps keep the Lead conversation small.

## Why it exists

handback started from a simple wish: let the agent apps already on my computer talk to each other while I can still watch every conversation in the apps themselves. That is why a worker is a real conversation inside its own app, not a hidden subprocess. You can open it at any time and follow the work as it happens.

Working across multiple coding agents creates repeated handoffs: send the task, remember which conversation is working on it, find the answer, and bring it back to the original discussion. If a conversation closes or a wait times out, it can also become unclear whether a task finished or should be sent again.

handback gives each request an identity and keeps its result on disk. The Lead can retrieve an existing request after an interruption and mark a reviewed result as handled. This makes the handoff easier to follow and reduces the risk of submitting the same work twice.

It is intended for developers who already use multiple supported coding-agent apps and want to coordinate bounded tasks in their own projects. You still decide what work is allowed and review the results. A message from an agent is task information, never user authorization.

## How a task moves

1. **You give the Lead a goal and limits**, such as reviewing a module without changing files.
2. **The Lead delegates a task to a worker.** The worker receives its own conversation and a brief describing the work.
3. **The relay collects the reply into an inbox.** The saved result remains available if the Lead closes or collection is interrupted.
4. **The Lead reviews the result and acknowledges it.** An acknowledgement, or **ACK**, marks it as handled so it stops replaying. The original message is retained.

You can also run these steps from a terminal. The first example below uses terminal commands so you can check the complete handoff before relying on automatic reception in an app.

## Supported combinations

| Lead | Workers | Result reception |
|---|---|---|
| Claude | Codex, Antigravity, or both | Monitor and explicit inbox reads; session recovery hooks where available |
| Codex | Antigravity only | External Codex queue, inbox, and ACK |

Start with **Claude Lead → Codex worker**. Claude workers, Codex Lead → Codex worker, Antigravity Lead, and automatic quota fallback are unsupported.

Windows has live integration coverage. macOS and Linux are unverified. App queue, transcript, and hook contracts can change with updates; see the [verification summary](https://github.com/Femur-0607/handback/blob/main/docs/verification/README.md) for tested behavior and remaining gaps.

## Install

After the PyPI release (not published yet), install into an isolated tool environment:

```sh
uv tool install handback
# Or, with Python already available:
pipx install handback
handback install-skills --dry-run
handback install-skills
```

uv can provision Python when needed. From a source checkout today, use `uv tool install .` or `pipx install .`, then the same `handback` commands. No runtime dependencies are installed. The optional `handback-dashboard` GUI requires a Python build with Tk support.

`handback install-skills --dry-run --target-home <absolute-test-home>` previews an isolated installation without consulting host PATH or CODEX_HOME. Existing skills get timestamp backups. Claude is always installed; Codex and Antigravity require app detection, and Antigravity also requires a registered skill directory. Without `--target-home`, detection checks the known Windows app paths and PATH on all platforms.

Packaged skills and hooks use the environment's absolute Python executable with `-m handback`. A direct, uninstalled checkout uses its absolute entry script instead, so hooks and workers also run from other project directories. Keep that environment (or checkout) available, and reinstall skills/hooks if you move it. Existing hook ownership manifests still support removing or upgrading old script-based hooks.

The source-only walkthrough below remains supported; `install.ps1` is a thin wrapper around `python handback.py install-skills`. Package users can replace `python "$relayScript"` in later examples with `handback`.

## Try your first task

Start with **Claude Lead → Codex worker**. Have these ready:

- Windows, PowerShell, and Python 3.10 or newer available as `python`.
- Git if you clone the repository, or an extracted source archive.
- Claude and Codex installed and signed in; start the apps before testing.

After installation, run this from your project in a normal terminal, outside an agent sandbox:

```powershell
handback try
# From an uninstalled source checkout:
python handback.py try
```

`try` checks configuration and Codex detection, creates one read-only worker, sends a reply-only task, verifies its saved `RELAY_OK` result and request identity, then ACKs it. It prints the worker handle, request ID, elapsed time, result, and ACK status. It does not install skills or hooks. App detection alone does not verify login or queue compatibility; the task checks the round trip.

Use `--root <path>` for another project, `--lead claude:<session-id>` when selecting a new topology, `--timeout 300` for the wait limit (default; `0` waits indefinitely), `--keep` to leave the result unread, or `--json` for machine output. An existing topology with a Codex worker is reused; an incompatible one is left untouched with a suggested `use` command. On timeout or unknown delivery, run the printed `wait --request ...` command; never resubmit.

<details>
<summary>What `try` does, step by step (manual setup and recovery)</summary>

The manual route below includes optional skill setup. Keep the same state home and PowerShell session throughout.

### 1. Get the tool and check your setup

```powershell
git clone https://github.com/Femur-0607/handback.git
Set-Location handback
$relayRoot = (Get-Location).Path
$relayScript = Join-Path $relayRoot 'handback.py'
python --version
python "$relayScript" doctor --root "$relayRoot"
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
```

If you downloaded an archive, open its extracted `handback` folder in PowerShell and continue from `$relayRoot = ...`. No `pip install` is required. Review the diagnostic output and planned installation paths. `doctor` checks app availability; it does not prove a task can complete. See [setup and troubleshooting](https://github.com/Femur-0607/handback/blob/main/docs/quickstart.md) if it reports a missing app or an existing-state warning.

### 2. Install the skills and select the Lead inbox

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
$leadAddress = "claude:lead"
python "$relayScript" use --root "$relayRoot" --lead "$leadAddress" --workers codex
python "$relayScript" status --root "$relayRoot"
```

Check that `status` shows the Claude Lead and Codex worker combination. `claude:lead` is an inbox address for this first test; you do not need to discover a real Claude session ID to read its results manually. Session-specific recovery hooks require a real session ID later.

The installer writes agent skills and backs up existing skill files. It does not start the apps, a Lead conversation, or a watcher. Keep the checkout after installation because the installed skills refer to its files. The Windows default state home is `%USERPROFILE%\.handback`; if you already set `HANDBACK_HOME`, keep the same value throughout.

### 3. Send one small task

This command creates a real Codex worker conversation and sends one task:

```powershell
python "$relayScript" new --worker codex --cwd "$relayRoot" --name "relay first task" --sandbox read-only --text "Do not modify files or delegate. Reply with RELAY_OK in this conversation." --no-wait
```

Save the returned JSON, especially `request_id` and `handle`. With `--no-wait`, a successful exit means the task was accepted; its result may still be pending. A detached collector gathers the reply.

### 4. Collect and review the answer

Replace the placeholder with the `request_id` returned above:

```powershell
$requestId = "<request_id from the previous command>"
python "$relayScript" wait --root "$relayRoot" --request "$requestId" --timeout 300
python "$relayScript" inbox list --root "$relayRoot" --for "$leadAddress"
```

Check that the result belongs to this request and contains `RELAY_OK`. If waiting times out, keep this request ID and follow the recovery section below. Do not send the task again.

### 5. Mark the result as handled

Use the same request ID after reviewing its result:

```powershell
python "$relayScript" inbox ack --root "$relayRoot" --for "$leadAddress" --request "$requestId"
python "$relayScript" inbox list --root "$relayRoot" --for "$leadAddress"
```

The handled result should disappear from the pending list while its original file stays on disk. You have now checked task submission, collection, review, and acknowledgement.

</details>

## Use it on your own project

Install the relay once, then select the project you want to work on. **The relay installation folder and your working project are separate paths.** Keep `$relayScript` pointing to the installed tool, and set `$projectRoot` to your existing source folder:

```powershell
$projectRoot = (Resolve-Path "<path-to-your-existing-project>").Path
python "$relayScript" use --root "$projectRoot" --lead "$leadAddress" --workers codex
python "$relayScript" status --root "$projectRoot"
```

Open that project in your Lead app and confirm the installed `handback` skill is available. Here is an example request to give a Claude Lead; replace both paths and the module name:

```text
Use the handback skill for the project at <absolute-project-path>.
The relay script is <absolute-relay-installation>/handback.py.
Use claude:lead as the Lead inbox and Codex as the worker.
Ask one worker to review error handling in <module-path> without changing files.
Collect its result, review the findings, summarize them here, and ACK the result after review.
```

For terminal commands, use `--cwd "$projectRoot"` when creating a worker and `--root "$projectRoot"` for topology, status, waiting, and inbox commands. The worker works on that project. Use one worker conversation per unit of work and run units sequentially when they share a checkout. Send corrections to the same worker only after its previous request finishes.

For automatic results in a Claude conversation, have the Lead run `inbox watch` through its **Monitor** tool. A watcher in an ordinary terminal prints only to that terminal. Started with `--idle-exit 1200`, the watcher stops after 20 idle minutes; re-arm Monitor only while requests are open. If Monitor is unavailable, use explicit `wait` and `inbox list` commands. Fresh skill auto-loading and global recovery hook loading remain incompletely verified; the [detailed setup guide](https://github.com/Femur-0607/handback/blob/main/docs/quickstart.md) covers these limits and the exact watcher command.

For Antigravity, register **your working project** in its app, enable its adapter, and install its relay hooks before selecting it. Follow the [Antigravity setup](https://github.com/Femur-0607/handback/blob/main/docs/quickstart.md#optional-antigravity-setup); the Codex-only test above does not configure that combination.

## Choose models and reasoning for each role

Codex Lead and worker defaults can be set separately for each project. The dashboard exposes the current combination's settings from a project row's context menu → **모델·추론 설정**. From a terminal:

```powershell
handback configure --root "<absolute-project-path>" --agent codex --role lead --model "<model-id>" --reasoning-effort ultra
handback configure --root "<absolute-project-path>" --agent codex --role worker --model "<model-id>" --reasoning-effort high
```

These defaults apply to new relay-created conversations. `new --model ... --reasoning-effort ...` overrides the selected role for one new conversation; Codex `send` accepts the same options and records the selection for subsequent relay turns. The native settings are persisted before queueing; changing the actual next turn of a conversation already loaded in the desktop app remains unverified because the app can retain runtime settings. Existing saved conversation settings take precedence over later role-default changes. Use `--clear-model` or `--clear-reasoning-effort` with `configure` to remove that role's override and inherit the shared agent setting or Codex's native configuration.

Role settings are saved outside the repository in project state. Shared defaults can also be set in `<state-home>/config.json` or project `.handback.json` using `agents.codex.model` and `agents.codex.reasoning_effort`; role overrides use `agents.codex.lead` and `agents.codex.worker` objects with the same fields. Relay command options take precedence over saved conversation settings, followed by role defaults, shared defaults, and native Codex configuration. Recognized effort values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`; availability depends on the selected model and installed Codex build.

Antigravity workers support `--model flash_lite|flash|pro` for new conversations, including `configure --agent antigravity --role worker --model pro`. Its current agentapi interface exposes no reasoning option or model change for an existing conversation. Claude Lead settings remain in the Claude app. Settings for user messages entered directly in an app are controlled by that app; relay options describe relay creation and delivery.

## Delivery and recovery

Submission, completion, Lead delivery, and ACK are separate events. An asynchronous submission returning success means the request was accepted. It does not mean the task finished.

### Result did not come back

Start with `doctor`, then `explain --request` for the saved request ID. Both are read-only; `explain` ends with one next action and never sends, collects, or ACKs:

```powershell
python "$relayScript" doctor --root "$projectRoot"
python "$relayScript" explain --root "$projectRoot" --request "<saved-request-id>"
python "$relayScript" wait --root "$projectRoot" --request "<saved-request-id>" --timeout 300
python "$relayScript" inbox list --root "$projectRoot" --for "$leadAddress"
```

Use the same project, Lead inbox, and state home as the original request. For the first test above, use `$relayRoot` instead of `$projectRoot`. `wait` recovers the existing request without submitting it again. Reading a result does not ACK it.

`doctor` prints OK/WARN/FAIL checks and one next step for each warning/failure (exit 5 for FAIL, 0 otherwise). `doctor --json` preserves the original JSON interface; `explain --json` provides the timeline as JSON. For an issue, copy the redacted block from `doctor --report`. `status --stats --days 30` summarizes local request outcomes, latency, additional delivery attempts, and unACKed results without telemetry. Collection timeouts, recovery via `wait`, and explicit redeliveries are not independently recorded and cannot be counted reliably.

The [detailed guide](https://github.com/Femur-0607/handback/blob/main/docs/quickstart.md) covers timeouts, replay, custom state homes, cleanup, and uninstall; the [Korean manual](https://github.com/Femur-0607/handback/blob/main/docs/usage.ko.md) describes the full command set and adapter limits.

## Optional dashboard

Run `pythonw dashboard.pyw` from the relay installation folder to see project activity, in-progress tasks, and unacknowledged results. The dashboard is optional; it is not required for delegation. `python handback.py dashboard --autostart on` starts the dashboard at Windows login, not the Lead or its Monitor.

<details>
<summary>Dashboard memory measurements</summary>

The optional dashboard was measured on **October 8, 2026**, using Windows, Python 3.13.15, and Tk 8.6.15. These figures describe the dashboard process, excluding the separately running agent apps.

| Check | Observed result | Conditions |
|---|---|---|
| GUI footprint | About **33 MiB working set** and **19 MiB private memory** | A fresh process with a hidden window, 5 projects, no unread results, normal 3-second refresh, and a 15-second observation |
| Context-menu leak fix | Menu widgets, Tcl commands, Windows GUI resources, and handles stayed constant from 100 to 500 menu openings | Real Tk widgets with native popup display suppressed; the previous menu tree and its callbacks are destroyed before replacement |
| Large unread inbox | About **60.3 MiB peak additional Python heap**; **2.7 seconds median per snapshot** | Snapshot only, without Tk: 1,000 unread results with 60 KiB bodies plus 1,000 completed requests; 5 timed runs |

The large-inbox figure measures Python allocations, not total process memory, and timing can vary with concurrent filesystem activity. Refreshes scan stored history and load unread message bodies; showing fewer rows does not cap that work. Usage therefore depends on the amount of stored data and the environment. These short checks do not establish a memory limit or guarantee leak-free operation over hours or days.

[Menu regression tests](https://github.com/Femur-0607/handback/blob/main/tests/test_dashboard_gui.py) cover repeated menu creation, callback cleanup, current settings, and closing the dashboard. They use hidden Tk windows and skip when Tk or a display is unavailable.

</details>

## Limitations

- Long-running Lead and reused worker conversations accumulate context; use bounded units and documented stage handoffs.
- Replacing the Lead preserves old requests' return addresses; explicitly read and ACK the old inbox.
- Windows has live verification; operation is local to one computer and OS user, with restricted agent combinations.
- App updates, uncertain delivery, and Monitor expiry require deliberate diagnostics and recovery; Antigravity cannot enforce read-only access.
- Shared-checkout work should be sequential, and dashboard scan cost grows with history. See [known limitations and workarounds](https://github.com/Femur-0607/handback/blob/main/docs/limitations.md) for details and commands.

## Development

Implementation lives in `handback/`, the CLI entry point is `handback.py`, and tests are in `tests/`.

```powershell
python -m unittest
```

Unit tests use isolated fixtures. They do not establish compatibility with a current app build; live integration results are tracked separately in the [verification summary](https://github.com/Femur-0607/handback/blob/main/docs/verification/README.md). See [Contributing](https://github.com/Femur-0607/handback/blob/main/CONTRIBUTING.md) for test and bug-report requirements.

## License

[MIT](https://github.com/Femur-0607/handback/blob/main/LICENSE). The license covers this repository's code; the agent applications are separately installed and retain their own licenses and terms.

## Migrating a previous local installation

`HANDBACK_HOME` selects the state directory. Defaults are `%USERPROFILE%\.handback`
on Windows, `~/Library/Application Support/handback` on macOS, and
`${XDG_STATE_HOME:-~/.local/state}/handback` on Linux. `AGENT_RELAY_HOME` is never
used as the new state setting. `handback doctor` warns about legacy state and prints
`handback migrate-state --from '<old-state-path>'`. Stop requests, collectors, and
watchers before copying. Migration verifies copied bytes, retains the original files,
and adds a `MOVED.json` receipt to prevent accidental writes to the old store.

Reinstall hooks and skills with `handback install-hooks` and `handback install-skills`;
legacy hooks are recognized and original skill files receive timestamp backups.
Copy a legacy `.agent-relay.json` project policy to `.handback.json`. Until then the
legacy policy is read with a warning; when both exist, `.handback.json` takes precedence.
