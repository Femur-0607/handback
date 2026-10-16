# Changelog

All notable changes to this project are documented here. This file follows the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

### Added

- The dashboard notices Codex, Claude and Antigravity updates. Every 10 minutes
  it reads each installed app's version; a version that differs from the last
  checked one runs a read-only compatibility check (`--help`/`--version`, the
  Antigravity sidecar registry and manifests, project file format, hook
  settings format; nothing is sent). A tray notification says
  "Antigravity 2.23.1 감지 — 호환성 점검 통과/실패". A pass records the version
  as verified; a failure adds a **⚠ … 진단 요약** menu item with a copyable
  summary (app version, failed checks, relevant log lines with paths shortened)
  and a button that opens a new GitHub issue page. Nothing is filed
  automatically. **앱 호환성 점검** in the menu runs the check on demand.
- `handback doctor` reports the Antigravity version (from the installed app,
  or its logs) instead of "unknown", and notes a passed compatibility check.

### Changed

- The update check is available in every run, including a source checkout,
  and moved out of **보기 설정** into the right-click menu itself, next to
  **Windows 시작 시 실행**: **최신 버전 확인** reports "최신 버전입니다" or adds an
  **업그레이드 (version)** item at the top of the menu. The installer build
  downloads, verifies and installs as before; a source checkout opens the
  GitHub release page instead. The menu items were renamed from
  **지금 업데이트 확인** and **업데이트 설치**.
- **보기 설정** now offers two views only, **작게 보기** and **펼쳐 보기**. The
  **작업표시줄에 넣기** item is gone: either view docks by dragging it onto a
  top/bottom taskbar and undocks by dragging it away. The expanded panel docks
  flush against the taskbar and keeps its position across restarts.

### Fixed

- Antigravity workers deliver again with Antigravity 2.23+, which runs each
  sidecar in a copy of the target project named after the sidecar. The sidecar
  now accepts that copy when it covers the same checkout, the copy no longer
  counts as a second registration of the folder, and handback removes it
  (with a backup) after delivery; `cleanup-sidecars` also removes copies left
  behind by earlier runs. A sidecar that stops before calling agentapi now
  reports `send_failed` instead of `delivery_unknown`. (#4)
- `send` to a Codex conversation that is open in the Codex app no longer fails
  with "active writer" because handback's saved reasoning effort differed from
  the conversation's own. The conversation's settings now win unless the send
  passes `--model`/`--reasoning-effort`; a warning names both values. An
  explicit change on a conversation the app holds is refused with nothing sent
  and a message saying what to do. See
  [the verification record](docs/verification/2026-10-15-codex-native-settings-precedence.md).
- The widget docked in the taskbar no longer flickers when a taskbar button is
  clicked: a foreground-change hook puts it back above the taskbar at once
  instead of on the next 750 ms check. It also hides within about 0.1 s when a
  full-screen app (such as a game) opens, and follows a taskbar colour change
  within a second instead of up to 30 seconds; colour sampling reads the screen
  once per check (about 5 ms instead of 90 ms).
- `doctor` kept warning "untested version" for a Codex build after `selftest`
  had passed against it, and its next step was that same `selftest`
  ([#1](https://github.com/Femur-0607/handback/issues/1)). A passing `selftest`
  now records the Codex version in `<state home>/selftest.json`; `doctor` reports
  such a version as `OK … (local selftest ok <date>; release-verified=<version>)`.
  The release-verified table is unchanged.
- `doctor` reported "installation reference differs or is unverified" for the
  skills and a bare WARN for the recovery hooks when a different handback
  installation (a second checkout, a non-editable pip copy, a frozen build) had
  installed them, and its next step would have moved them to the running copy
  ([#2](https://github.com/Femur-0607/handback/issues/2)). Both checks now name
  the entry point that owns the file and this run's entry point, and suggest
  choosing one installation (or `pip install -e <checkout>`) instead.
- Agents that loaded the handback skill often switched to English once the
  English skill text and relay output filled the conversation, even when the
  user wrote in another language. The skill now tells agents to reply in the
  user's language. Run `install-skills` to update installed copies.
- Docking placed the widget mid-screen after the display scale changed while
  the dashboard was running (for example 150% → 100%): the taskbar rectangle
  came from a shell query in physical pixels while the window used Windows'
  DPI-virtualized coordinates. Both now share one coordinate space.
- A second Claude session in the same project root could send a request whose
  result silently went to the configured Lead's inbox instead of its own
  ([#3](https://github.com/Femur-0607/handback/issues/3)). `new`/`send` now
  refuse, before creating or sending anything, when the calling Claude session
  (`CLAUDE_CODE_SESSION_ID`) is not the configured Claude Lead, and name both
  ways out: the new `--return-to <agent:id>` option, which sends the result to
  that address without changing the topology, or `use --lead`. `use` also warns
  when the Lead it replaces still has open requests or a live inbox watcher.

## [0.3.5] - 2026-10-12

### Added

- In-app update for the Windows installer build: the dashboard checks GitHub
  Releases once a day (toggle under 보기 설정, or check now from there). When a
  newer release exists, right-click › **업데이트 설치** downloads the installer,
  verifies it against the release's `SHA256SUMS.txt`, runs it silently, and the
  installer reopens the dashboard.

### Changed

- Setup now installs the Claude/Codex recovery hooks by default, alongside the
  skills (existing settings are backed up). This covers the installer, the
  `handback setup` wizard and the dashboard's 설정 window.
- The 설정 window is the single place to install or repair skills and hooks: it
  shows whether each is installed, and its **훅 해제** button removes hooks. The
  separate **스킬 다시 설치** and **Hook ▶** menu items are gone.

## [0.3.4] - 2026-10-12

### Fixed

- Setup and the dashboard now recognise Claude when only the Claude desktop
  app is installed (Microsoft Store/MSIX or the older installer) and no
  `claude` CLI is on PATH. Previously setup stopped with "Claude가 필요합니다"
  even though the desktop app's Code tab can act as the Lead. First reported by
  an early outside user installing 0.3.3.

## [0.3.3] - 2026-10-12

### Added

- The compact widget and the expanded panel show a small dot before each
  project name, coloured by the project's lead agent (dim when unknown); on a
  light docked taskbar the dot uses darker agent colours.
- Codex is now shown in a cool slate colour (`#9fb3c8`) instead of the normal
  text colour, so it is distinguishable in the panel and the lead dot.

### Fixed

- Launching the dashboard again (for example from the Start menu) while it is
  hidden in the notification area now brings the running dashboard to the
  front instead of exiting silently.

## [0.3.2] - 2026-10-12

### Added

- Notification-area (tray) icon: left-click shows or hides the dashboard,
  right-click opens its menu (with **숨기기** and **종료**), and the tooltip shows
  the current state. **닫기** now hides the dashboard to the tray.

### Removed

- The 0.3.1 taskbar button; handback now lives in the notification area only.

### Changed

- Dashboard window icons are loaded at 16, 20, 24, 32, 40, 48, 64, 128 and
  256 px for sharper rendering at every display scale.
- The Windows download is now a single installer, `handback-<ver>-setup.exe`. It
  installs the agent skills in one step, adds a Start menu entry and an optional
  desktop shortcut, offers optional autostart, upgrades in place, and uninstalls
  from Windows Settings. The zip is no longer published.
- Dashboard menu item renamed from "설정 다시 실행" to "설정".
- The installer adds its folder to the per-user PATH, so `handback` works in
  new terminals; uninstall removes exactly that entry.
- CI now builds and smoke-tests the Windows installer on every push and manual
  run, and the release workflow's installer job can be run manually (tag check
  and GitHub Release upload stay tag-only).

### Fixed

- Uninstalling now also removes handback's Antigravity hooks and its own
  Claude, Codex and Antigravity skills (new `handback uninstall-skills`, which
  deletes only skill files that point at this installation and keeps backups),
  so nothing is left pointing at the deleted exe. `~/.handback` data is kept.
- The windowed dashboard no longer flashes console windows: its `git` and
  PowerShell child processes run with `CREATE_NO_WINDOW`. Toggling
  **Windows 시작 시 실행** no longer freezes the dashboard while PowerShell
  runs, and the startup-shortcut check reads non-ASCII paths as UTF-8.
- The setup window no longer warns "move to a folder without spaces" for the
  installer location (`%LOCALAPPDATA%\Programs`) when the Windows user name
  contains a space. Its update hint now points at the new setup.exe (or at
  `pip install -U` for source installs) instead of overwriting files.
- **모두 확인 처리** in the unread-results viewer now acknowledges only the
  results that viewer showed; results that arrive while it is open stay
  pending and still reach the Lead.

## [0.3.1] - 2026-10-11

### Added

- Setup window on the first dashboard launch (exe) and a **설정 다시 실행**
  menu item to reopen it.
- Dashboard taskbar button with the handback logo.

### Changed

- README: start from `handback-dashboard.exe`.

### Fixed

- The release job now creates a missing GitHub release (PowerShell 5.1 stderr).
- Dashboard menu tests no longer depend on the real Startup folder.

## [0.3.0] - 2026-10-10

### Added

- Windows download: `handback-<version>-windows-x64.zip` on GitHub Releases with
  `SHA256SUMS.txt` and a build provenance attestation. It contains
  `handback.exe` and `handback-dashboard.exe` and needs no Python.
- Setup wizard: double-click `handback.exe`, or run `handback setup` (`--yes`,
  `--dry-run`). It checks apps, installs skills, offers Windows autostart, opens
  the dashboard, and prints the next step.
- Dashboard right-click menu: Windows startup toggle, install check, skill
  reinstall, Hook install/remove, per-project role selection, and a connection
  test that runs one real Codex worker task. The dashboard can set only Claude
  as Lead; Codex and Antigravity Leads are set from their own app.
- `handback --version`.

### Changed

- Running `handback` with no arguments prints help from source. The Windows exe
  runs the setup wizard instead.
- The Windows startup shortcut repairs itself if the folder was moved.
- Hook-scan checkpoints are deleted when a scan completes. Cursors untouched for
  7 days are pruned (at most 16 per write). `limits.log` is kept.

### Fixed

- Windows sharing violations (`PermissionError`) in state locks and atomic file
  replaces could crash concurrent routers. They are now retried.

## [0.2.4] - 2026-10-09

### Changed

- The compact dashboard widget drags freely over the taskbar and to the monitor
  edges, from anywhere including status counts. It docks only when released
  inside a top or bottom taskbar with at least half of it overlapping. Floating
  widgets have rounded corners and an outline on Windows.
- The dashboard now starts as a small widget (`docked` default changed from
  true to false). It docks only when released inside the taskbar.
- Long expanded panels scroll, including pinned panels. `+N` turns yellow when
  the remaining projects have unread results. Unchanged tooltips and scroll
  positions survive refresh.

### Fixed

- The dashboard shows state read errors instead of an empty project list.
- The recovery hook resumes bounded directory scans from a per-scope checkpoint
  instead of restarting at the 4096-entry cap, so later entries are no longer
  skipped on every call. Incomplete scans are reported in the hook context and
  `hook-scans/limits.log`.

## [0.2.3] - 2026-10-09

### Fixed

- A collected result is kept until it is published. A transient failure while
  saving the result mail or request state no longer stops the watcher from
  completing the request, and the worker is never asked again.
- `doctor` reports a missing Codex executable or `queue --thread` support as a
  failure only when Codex is the selected Lead or a worker. Otherwise these
  checks are advisory warnings.
- On Windows, reading relay state retries briefly when another process is
  replacing the same file, instead of failing the router or collector.

## [0.2.2] - 2026-10-09

### Fixed

- An Antigravity Stop that fires before its observation file exists is now
  collected once instead of being missed.
- Codex install discovery is shared by the adapter and `install-skills`, so
  version-specific install paths are found by both.
- Releasing a project from the dashboard re-checks open requests under the
  state lock, so a request created meanwhile is never archived.
- Open menus, submenus, and focus survive the periodic dashboard refresh.
- The dashboard redraws when its error state changes, and the model/reasoning
  dialog resizes after showing or clearing errors.

### Changed

- Common dashboard errors are worded in Korean, the compact font is slightly
  larger, and the panel responds to Enter/Space/Esc.
- The release workflow publishes only after the Windows test matrix passes.

## [0.2.1] - 2026-10-09

### Added

- Per-project model and reasoning defaults for Codex Lead and worker roles, with
  CLI overrides, inheritance, and a dashboard settings dialog. Antigravity worker
  models can be selected before their first conversation is created.
- Free movement of the compact dashboard widget, snapping to top/bottom taskbars
  within about 20 DPI-scaled pixels, and persistent horizontal/vertical position
  and docking state. Off-screen positions are clamped back onto the screen.
- Automatic hiding of the docked or floating compact widget while a full-screen
  window covers its monitor.

### Fixed

- Pulling the widget away from the taskbar now undocks reliably at scaled DPI.
- Unchanged widget content no longer triggers periodic repaint flicker.
- Fast dragging follows the current pointer position without lagging behind
  queued motion events.

## [0.2.0] - 2026-10-09

### Added

- First public PyPI release of the local relay, using Python's standard library,
  with `handback` and optional `handback-dashboard` entry points.
- Supported Claude Lead → Codex and/or Antigravity worker combinations and
  Codex Lead → Antigravity, with durable requests, result collection, inbox ACK,
  recovery commands, diagnostics, and local usage statistics.
- A compact two-line taskbar widget, expanded project panel, and project/view
  context menus, including unread-result acknowledgement and project release.

### Changed

- Renamed agent-relay to handback, including the package, commands, and installed
  skills. The Windows state default is `%USERPROFILE%\.handback`, selected with
  `HANDBACK_HOME`; project policy uses `.handback.json`. Legacy state is detected
  with migration guidance, legacy policy remains readable with a warning, and
  relay markers and state formats are preserved.
- README links use absolute URLs so they work on PyPI.

### Fixed

- The docked widget restores its position above the Windows taskbar when covered.
- Legacy-state checks recognize chained `MOVED.json` receipts, including stores
  migrated through `.agent-relay` to `.handback`.
- Windows extended-length paths no longer cause valid project-state paths to be
  rejected during concurrent delivery checks.
- The release version check imports the checkout when run from a temporary
  directory.

[0.3.5]: https://github.com/Femur-0607/handback/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/Femur-0607/handback/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/Femur-0607/handback/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/Femur-0607/handback/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Femur-0607/handback/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Femur-0607/handback/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/Femur-0607/handback/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/Femur-0607/handback/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Femur-0607/handback/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Femur-0607/handback/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Femur-0607/handback/releases/tag/v0.2.0
