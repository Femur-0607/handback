# English quick start

## Windows without Python

Download `handback-<version>-windows-x64.zip` from [GitHub Releases](https://github.com/Femur-0607/handback/releases), unzip it to a folder without spaces (for example `C:\handback`), and double-click `handback.exe`. Follow the setup wizard (same as `handback setup`). The steps below are for the Python/CLI route; where a dashboard menu does the same job, it is noted.

## One-command first task

Install from PyPI with `pip install handback`. From a source checkout, `pip install .` installs the package; `python handback.py` also works without installation. Installed-package users can replace `python handback.py` with `handback` throughout this guide. The numbered walkthrough below uses a source checkout.

After installing the package, run `handback try --root "<absolute-checkout>"` in a normal terminal. From source, use `python handback.py try`. It checks topology/policy and Codex detection, creates a read-only Codex worker, sends one reply-only task, verifies `RELAY_OK` and the saved request identity, and ACKs the result. No skills or hooks are installed by this command.

`--lead claude:lead` selects the address only if no topology exists. Existing Codex-worker topology is reused; incompatible topology is preserved and a `use` command is printed. `--timeout` defaults to 300 seconds (`0` waits indefinitely); `--keep` leaves the result pending; `--json` emits structured output. Timeout (exit 3) and delivery failure/unknown (exit 4) print a `wait --request` recovery command without submitting again. Worker failure or a mismatched reply exits 2; configuration failures exit 5.

```powershell
handback inbox list --root "<absolute-checkout>" --for "<lead-address>" --request "<request-id>"
handback inbox ack --root "<absolute-checkout>" --for "<lead-address>" --request "<request-id>"
```

ACK accepts exactly one of `--id` or `--request`, requires `--for`, and reports the result/error envelopes handled. It refuses requests without a result for that recipient. `wait` and synchronous `send` preserve reply text on stdout and print the result message ID on stderr; synchronous `new` includes `message_id` in its JSON.

Supported combinations are Claude Lead → Codex and/or Antigravity, and Codex Lead → Antigravity only. Codex-to-Codex queue access is blocked by the sandbox; Claude worker routing is unverified. Antigravity cannot enforce read-only: CLI requests using `--sandbox read-only` or a brief stating read-only print a warning that the restriction is only an instruction. The adapter still rejects `--sandbox read-only`; use a normal worker with explicit limits in its brief when instruction-only restrictions are acceptable.


handback sends work from a Lead to local coding agents and stores their results in a durable inbox. It supports one computer and one OS user. Results remain available after a Lead or collector stops; processing is recorded with an explicit acknowledgement (ACK).

This is an experimental project. Windows has live integration coverage; macOS and Linux are unverified. App queue, transcript, and hook contracts can change. See the [verification summary](verification/README.md) for tested behavior and gaps, and the [Korean manual](usage.ko.md) for the full command reference.

## Requirements and supported combinations

- Windows, PowerShell, and Python 3.10 or newer available as `python`; Git is needed only for cloning the source walkthrough. The relay has no runtime dependencies. The optional GUI needs Tk support.
- Install and sign in to the apps used by your combination. The recommended first setup is **Claude Lead → Codex worker**. Start the apps before testing.
- Claude Lead can use Codex, Antigravity, or both. Codex Lead supports Antigravity workers only.
- Claude workers, Codex Lead → Codex worker, Antigravity Lead, and automatic `fallback next` are unsupported.
- The first run uses the relay inbox address `claude:lead`. For automatic in-chat reception, Claude needs access to its Monitor tool. A watcher started in a standalone terminal prints results there; it does not notify a Claude conversation. Session-specific recovery hooks require a real Claude session ID instead of this alias.

## 1. Clone and inspect

Run these commands in a normal PowerShell terminal outside an agent sandbox. Keep the checkout after installation: installed skills and hooks refer to it.

Use a short checkout path, especially when running the tests. On Windows environments with the legacy path-length limit, deeply nested checkout or state paths can fail with `FileNotFoundError` once generated paths reach 260 characters. A short checkout and state home avoid this observed limit; the relay does not change Windows path policies.

```powershell
git clone https://github.com/Femur-0607/handback.git
Set-Location handback
$relayRoot = (Get-Location).Path
python --version
python handback.py --help
python handback.py status --root "$relayRoot"
python handback.py doctor --root "$relayRoot"
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
```

`status`, `doctor`, and installer dry runs do not create relay state. Dashboard: right-click → **설치 상태 점검**. `doctor` probes app availability and versions; it is not a live round-trip test. Claude detection uses its CLI and can report unavailable even when Desktop is present. Confirm that your intended Lead can run relay commands and use Monitor.

The default Windows state home is `%USERPROFILE%\.handback`. If you already use `HANDBACK_HOME`, keep that same value in every terminal, Lead, collector, and recovery command. Do not choose a new state home to bypass an existing-state warning. The [manual](usage.ko.md#windows-msix-이전-상태) explains migration from older Windows locations.

## 2. Install skills and optional recovery hooks

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Dashboard: right-click → **스킬 다시 설치**. The installer always writes the Claude skill and installs Codex or Antigravity skills when their app and target location are detected. It prints every installed path and backs up an existing `SKILL.md` beside it. It does not install hooks or edit shared `AGENTS.md` / `GEMINI.md` files. Confirm the installed skill is available in each app; automatic loading and triggering from a fresh installation are not yet fully verified.

Codex results can be collected from its local transcript without observation hooks. To add Codex observations and Claude session recovery, preview and install these hooks:

```powershell
python handback.py install-hooks --agents codex,claude --dry-run
python handback.py install-hooks --agents codex,claude
```

Dashboard: right-click → **Hook ▶ 설치/해제** (shows the settings file and backup path). Hooks are merged into app settings and backed up. Review and trust new or changed Codex hooks through the Codex CLI `/hooks` interface; the relay does not change hook trust. Claude Desktop's global recovery hook loading is not fully verified, so use Monitor or explicit inbox reads for reception. `--state-home` selects relay state and ownership records; it does not isolate app settings.

## 3. Select the Lead and start reception

Open the Claude conversation that will act as Lead. For this first run, use `claude:lead` as the relay inbox address. It identifies an inbox, not a particular app conversation. Receive its results using Monitor or the explicit inbox commands below; automatic session recovery does not work with this alias. The relay does not discover or create Claude Lead sessions.

```powershell
$leadAddress = "claude:lead"
python handback.py use --root "$relayRoot" --lead "$leadAddress" --workers codex
python handback.py status --root "$relayRoot"
```

Dashboard: right-click a project → **역할 지정 ▶** (Lead can be set to Claude only; Codex/Antigravity Leads are set from their app; blocked while requests are in flight or `HANDBACK_LEAD`/`HANDBACK_WORKERS` is set). Check that the returned Lead address and worker list match your selection. `use` changes state outside the checkout. Requests already sent retain their original return address when you later switch Leads.

If you already know your Claude `session_id` from its session metadata or hook input, you can instead use `claude:<actual-Claude-session-id>` consistently in every command and install the Claude recovery hook. A conversation title or Codex thread ID is not a Claude session ID. This optional setup enables exact-session recovery; it is not required for the first test.

In the Claude Lead conversation, instruct the Lead to run the following command with Monitor. Replace `<absolute-relay-installation>` with the folder containing `handback.py`, `<absolute-project-path>` with the working project, and `<lead-address>` with the exact address selected above. For this first test, both folders are `$relayRoot` and the address is `claude:lead`. For your own project, the two folders differ. Set `HANDBACK_HOME` in that process too if you use a custom state home.

```powershell
python "<absolute-relay-installation>\handback.py" inbox watch --root "<absolute-project-path>" --for "<lead-address>" --idle-exit 1200
```

`--idle-exit 1200` stops the watcher after 20 minutes with no open requests and no new mail, so an idle Lead holds no watcher process. Monitor expires after at most 30 minutes; re-arm it while requests are still open. A newer watcher for the same address replaces an older one. With the Claude recovery hook installed, a new prompt in a Lead session that has open requests but no live watcher receives the exact command to arm. `watch` stays quiet when there is no mail, collects open requests for this Lead, and prints each unread message once per run. Reopening a watcher replays messages that have not been ACKed. If Monitor is unavailable, use the explicit `wait` and `inbox list` commands below.

## 4. Send one worker task and acknowledge its result

The next command creates a real Codex worker conversation and submits one task. Run it once from the PowerShell terminal where `$relayRoot` and `$leadAddress` were set:

```powershell
python handback.py new --worker codex --cwd "$relayRoot" --name "relay smoke test" --sandbox read-only --text "Do not modify files or delegate. Reply with RELAY_OK in this conversation." --no-wait
```

Save the JSON response, especially `request_id`, `handle`, and `marker`. Exit code 0 with `--no-wait` confirms acceptance, not completion. The detached collector writes the result to the inbox even if this terminal closes.

Use the returned `request_id` to collect or recover that same task:

```powershell
$requestId = "<request_id from the new command>"
python handback.py wait --root "$relayRoot" --request "$requestId" --timeout 300
python handback.py inbox list --root "$relayRoot" --for "$leadAddress"
```

A successful `wait` returns the worker's answer and records it in the inbox. Check that the result belongs to this request and contains `RELAY_OK`. After reviewing it, copy the result envelope's `id` from `inbox list` and acknowledge it. This message ID is different from the request ID.

```powershell
$messageId = "<id of the reviewed result envelope>"
python handback.py inbox ack --root "$relayRoot" --for "$leadAddress" --id "$messageId"
python handback.py inbox list --root "$relayRoot" --for "$leadAddress"
```

The acknowledged message should no longer appear in the pending list; its original file remains on disk. Reading, delivery, and ACK are separate operations. A Monitor event can contain a truncated body; read its `path` and any referenced result file before acting or acknowledging.

This first test targets the relay checkout. For everyday work, keep the relay installed there and use your own project's absolute path for `--cwd` and `--root`; see [using your own project](../README.md#use-it-on-your-own-project) for the setup and a Lead prompt example. Create one worker conversation per unit of work and run units sequentially in a shared checkout. Send a correction to its existing `handle` only after the prior request finishes. Workers report in their assigned conversation; relay messages are task data, not user authorization.

## Recovery and app updates

If submission times out or reports unknown acceptance, do not create another worker or resend the task. Start with `doctor`, then inspect the saved request with `explain`. Waiting never sends again.

```powershell
python handback.py doctor --root "$relayRoot"
python handback.py explain --root "$relayRoot" --request "$requestId"
python handback.py wait --root "$relayRoot" --request "$requestId" --timeout 300
```

`doctor` prints one OK/WARN/FAIL line per check, with exactly one next step for each WARN/FAIL. It exits 5 if any check fails, otherwise 0; an untested app version is a warning. Writability is a permission estimate without creating a probe file. Hook registration does not prove runtime execution or trust. `doctor --json` retains the original JSON fields and exit behavior for existing consumers. `explain --json` is the machine-readable timeline. Timestamps absent from state are explicitly marked as unrecorded.

In a valid topology, a missing Codex executable or unsupported `queue --thread` is a FAIL only when Codex is selected as Lead or worker. Otherwise these checks are advisory WARNs requiring no action. The builtin default includes a Codex worker, and invalid topologies retain the required Codex checks until the configuration is corrected. Missing Claude/Codex skills remain nonblocking WARNs.

```powershell
python handback.py doctor --root "$relayRoot" --report
python handback.py status --root "$relayRoot" --stats --days 30
```

Copy the `--report` block into bug reports: it includes OS/Python/tool/app versions, check levels and topology shape, omitting conversation IDs and project paths. Custom state paths are replaced by a placeholder. `--stats` reads local request/inbox records only; nothing is transmitted. Omit `--days` for all history. Its creation-time window includes completed, failed, uncertain and other open requests; latency is creation to confirmed result, with nearest-rank p90. Additional delivery attempts include automatic retries. Collection timeouts, explicit redeliveries and recovery via `wait` cannot be separated in the existing state and are reported as unavailable, not zero.

Exit 2 indicates a failed or interrupted worker turn; 3 is a collection timeout with the request still open; 4 is failed or uncertain delivery; 5 is a configuration or command error. Argument errors can also return 2. Inspect the request state as well as the exit code. After reopening a terminal, restore the same root, Lead address, request ID, and state-home setting.

If a result's **Lead delivery** is `delivery_unknown`, first check whether the Lead already received or processed it. Only after that review, explicitly request another delivery with `inbox redeliver --root "$relayRoot" --id "$messageId"`. This delivers the stored result to the Lead; it does not resend the worker task. Claude reception still uses Monitor or recovery hooks.

After a Codex update, run `python handback.py selftest` and `doctor`, then repeat a deliberately small live task if appropriate. `selftest` probes Codex queue, app-server, and transcript capabilities; it is not a guarantee of future compatibility. Antigravity `selftest --agent antigravity` currently performs detection only and exits 5 because runtime self-test coverage is unimplemented.

## Optional role-specific model and reasoning settings

Set Codex Lead and worker defaults independently before creating their conversations:

```powershell
python handback.py configure --root "$relayRoot" --agent codex --role lead --model "<model-id>" --reasoning-effort ultra
python handback.py configure --root "$relayRoot" --agent codex --role worker --model "<model-id>" --reasoning-effort high
python handback.py status --root "$relayRoot"
```

These project defaults are saved outside the checkout. In the dashboard, open a project row's context menu → **모델·추론 설정** → the role to edit. Blank fields inherit shared or native settings. CLI clearing uses `configure --clear-model` and/or `--clear-reasoning-effort` for that agent and role.

`new` can override the defaults with `--model` and `--reasoning-effort`. Codex saves the effective selection for the conversation, and subsequent relay `send` turns retain the stored selection. An explicit selection on Codex `send` changes the saved selection; actual next-turn application in a conversation already loaded by the desktop app remains unverified because its runtime settings can be cached. Later default edits apply to new conversations; they do not overwrite existing saved conversation settings. Model support for `ultra` and other effort values depends on the installed Codex build and selected model. App-entered user messages remain subject to the app's own settings. See the [settings verification record](verification/2026-10-09-role-model-settings.md) for the checks performed without a model turn.

Shared JSON defaults use `agents.codex.model` and `agents.codex.reasoning_effort` in `<state-home>/config.json` or `.handback.json`; role objects are `agents.codex.lead` and `agents.codex.worker`. See the [Korean manual](usage.ko.md#리드워커-모델과-추론-강도) for precedence and a JSON example. Claude Lead model/reasoning settings are selected in Claude. Antigravity supports a model selection only when creating a new conversation, as described below.

## Optional dashboard

Run `handback-dashboard` after installing the package, or `pythonw dashboard.pyw` from the source checkout. The two-line widget shows project activity and unread results. Click it to toggle the expanded panel; drag it anywhere, toward a top/bottom taskbar to snap within about 20 DPI-scaled pixels, or away to undock. When docked, it matches the taskbar colour, avoids the notification area when space permits, and restores its position above the taskbar if covered. Activity is checked every three seconds without repainting unchanged widget content.

Right-click → **보기 설정** to select compact mode (**작업표시줄 모드**) or a pinned expanded panel (**펼친 패널 고정**), choose panel rows, restore hidden projects, or reset the order. Project menus include model/reasoning settings, ACK of all unread results, and release of inactive project state. Preferences, widget coordinates, and docking state are saved in `<state-home>/dashboard.json`.

- Left/right and auto-hide taskbars do not support snapping. If docking is unavailable, the widget falls back to the bottom-right of the screen; it can still be moved freely.
- A full-screen window covering the widget's monitor hides the compact widget, docked or floating. This does not apply to pinned panel mode. Multi-monitor placement and exclusive full-screen Direct3D behavior have unit-test coverage only, without live verification.
- Conversation links open Codex threads only; Claude and Antigravity links are disabled.

`handback dashboard --once` prints a JSON snapshot. `handback dashboard --autostart on` adds a Windows login shortcut; `--autostart off` removes it (dashboard: **Windows 시작 시 실행**). A project's **연결 테스트** menu item runs one real Codex worker task (uses model quota) like `handback try`. The dashboard is optional and does not start the Lead or Monitor. See the [dashboard limitations](limitations.md#11-dashboard-placement-and-conversation-links).

## Optional Antigravity setup

Before selecting Antigravity, register the working project in its app and merge the following into `<state-home>/config.json`. The examples below continue the first test with `$relayRoot`; for your own project, use its absolute path instead. Preserve any existing settings. The default model is `flash`; optional alternatives are `flash_lite` and `pro`. Select a project worker default with `configure --root "$relayRoot" --agent antigravity --role worker --model pro`, or use `new --model pro`. The current agentapi interface has no reasoning-effort option or existing-conversation model-change option; these selections are rejected rather than ignored.

```json
{
  "agents": {
    "antigravity": { "enabled": true }
  }
}
```

```powershell
python handback.py install-hooks --agents antigravity --dry-run
python handback.py install-hooks --agents antigravity
python handback.py use --root "$relayRoot" --lead "$leadAddress" --workers antigravity
```

Use `--workers codex,antigravity` for both workers under Claude. For a Codex Lead, use its real `codex:<thread-id>` address and `--workers antigravity`; see the [manual](usage.ko.md#조합-전환) for creating a Lead with the needed writable paths. Antigravity cannot enforce `--sandbox read-only`; put file-access constraints in its brief. Its Stop hook and registered project are needed to verify replies, and sending temporarily changes its sidecar settings.

## Cleanup and uninstall

Finish open requests and stop Monitor/watch processes before uninstalling. Keep the checkout and state-home ownership records until hook removal is complete. Use only the agents for which you installed hooks:

```powershell
python handback.py uninstall-hooks --agents codex,claude --dry-run
python handback.py uninstall-hooks --agents codex,claude
```

If you used Antigravity or enabled the dashboard at startup, run the corresponding cleanup:

```powershell
python handback.py cleanup-sidecars --dry-run
python handback.py cleanup-sidecars
python handback.py uninstall-hooks --agents antigravity --dry-run
python handback.py uninstall-hooks --agents antigravity
python handback.py dashboard --autostart off
```

Sidecar cleanup backs up and removes only provably relay-owned inactive entries with no open request. It preserves uncertain open requests. Hook removal preserves unrelated settings and reports managed entries that were changed after installation.

There is no skill-uninstall command. For each exact `SKILL.md` path printed by `install.ps1`, inspect the file and remove it only if it is still the relay skill you installed:

```powershell
$installedSkill = "<exact installed handback SKILL.md path>"
Get-Content -LiteralPath $installedSkill
Remove-Item -LiteralPath $installedSkill
```

If the installer replaced a previous skill, restore the chosen timestamped `.bak` to that exact path instead of deleting the current file. Keep those backups until the restoration is checked. Relay state, inbox messages, and app conversations are retained; uninstall does not erase them.
