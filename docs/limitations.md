# Known limitations and workarounds

These limits describe the current implementation and recorded verification. **Current feature** means implemented behavior; **Operating tip** means a suggested workflow, not an enforced safeguard; **Future improvement** means unavailable functionality, not a release commitment. Commands below use the installed `handback` entry point; from a source checkout, replace it with `python "<absolute-relay-installation>/handback.py"`.

## 1. Lead conversation context grows

**Symptom:** A long-running Lead conversation accumulates instructions, investigations, and results. If its host app automatically compacts the conversation, the summary can lose details. Reusing a worker conversation can produce the same problem.

**Cause:** Relay delivers work and records results; it does not control an app's context window or compaction. Sending another turn to an existing conversation does not reset its history. The exact compaction behavior depends on the host app and is not a relay guarantee.

**Fix or workaround**, in this order:

### Size work before starting

- **Operating tip:** Small work: handle it in the Lead. Medium work: delegate to a worker and review only the result. Large work: write a plan document first, open a new Lead session for each stage, and hand off between stages through documents. Record decisions, remaining work, and verification in those documents instead of relying on chat history.

### Delegate the heavy work

- **Current feature:** `new --worker auto --cwd "<absolute-project-path>" --name "<unit> <goal>" --file "<absolute-brief-path>" --no-wait` creates a fresh worker for a bounded unit. Workers can perform heavy reading, investigation, and implementation and return a result for Lead review. The relay retains requests and collected replies outside the conversation; it does not automatically summarize them.

### Replace the Lead session explicitly

- **Current feature:** After opening a new Claude session, `use --lead` changes the configured Lead for subsequent requests. Already-open requests keep their original `return_to`, and their results go to the old address. Changing topology neither moves existing requests nor transfers conversation history. Use the same project and the exact `HANDBACK_HOME` that holds those requests. Environment overrides `HANDBACK_LEAD` and `HANDBACK_WORKERS`, if set, take precedence over saved topology; check `status` after switching.

```powershell
handback use --root "<absolute-project-path>" --lead "claude:<new-session-id>" --workers codex
handback status --root "<absolute-project-path>"
handback wait --root "<absolute-project-path>" --request "<old-request-id>" --timeout 300
handback inbox list --root "<absolute-project-path>" --for "claude:<old-session-id>" --request "<old-request-id>"
# Review the result and any referenced full result file before ACK.
handback inbox ack --root "<absolute-project-path>" --for "claude:<old-session-id>" --request "<old-request-id>"
```

- **Current feature:** A new session can explicitly list and ACK the old address's results with these commands. `--for` selects the stored recipient; it is not authentication of the running session. `inbox list --request` filters pending messages; `inbox ack --request` acknowledges that recipient's result/error messages. `--id "<message-id>"` remains an alternative ACK selector. Reading does not ACK or move the result.
- **Current feature:** A watch for the new address neither collects old-address requests nor displays old-address mail. To receive the old backlog explicitly, run `handback inbox watch --root "<absolute-project-path>" --for "claude:<old-session-id>" --timeout 60`. It can still collect and display old results after the switch. A detached collector already following an old request also keeps its original destination.
- **Current feature:** Recovery hooks require both the configured Lead and the mail recipient to match the session. The new Lead's hook does not inherit old mail; after the switch, the old Lead's recovery hook also no longer matches that project's topology. Use explicit old-address reads/watch. Claude global recovery hook loading and Codex recovery-hook trust retain the live-verification gaps described in the [verification summary](verification/README.md).
- **Operating tip:** Before leaving the old session, record its address, the state home, open request IDs, pending results, decisions, and next actions in a handoff document. Review and ACK old results deliberately from the new session. This example preserves the Claude → Codex combination; keep any replacement topology within the [supported combinations](../README.md#supported-combinations).
- **Future improvement:** Reassigning already-open requests or unACKed results to a replacement Lead is not supported by a dedicated command. Do not treat `use` or `inbox redeliver` as reassignment; redelivery uses the saved recipient.

### Keep briefs and returned results focused

- **Operating tip:** Keep each brief short and narrowly scoped. Ask the worker for a concise result containing changes, evidence, unresolved issues, and paths to detailed reports. This is an instruction to the worker, not an enforced response limit.
- **Current feature:** `envelope.MAX_BYTES` is `64 * 1024`: the limit applies to the entire canonical UTF-8 JSON envelope, including metadata and JSON escaping, not 64 KiB of body text. Oversized request envelopes are rejected; `--file` sends a brief's absolute path for the worker to read.
- **Current feature:** During collection, result text (or the error/empty-response fallback) whose `json.dumps(text, ensure_ascii=False)` UTF-8 representation exceeds `48 * 1024` bytes is replaced in inbox mail by a path to `<project-state>/results/<request-id>.json`. That file contains the complete result object. This is a separate threshold below the envelope limit, not a summary.
- **Current feature:** `inbox watch` emits the first 1,800 body characters; inbox and recovery hooks emit the first 600 per message. Both expose `body_truncated` and `path`, where `path` points to the full inbox envelope. That envelope may itself point to the full result file. Recovery hooks include at most 10 messages per invocation. These previews do not ACK mail. `inbox list` returns full stored envelope bodies; synchronous `send`/`wait` can still print the full result. Codex Lead delivery also substitutes an inbox-file path when its body exceeds 12,000 UTF-8 bytes.
- **Future improvement:** A dedicated worker-result summary field and a CLI option enforcing result length are not implemented. Preview truncation and `--return-file` do not provide these features.

### Make session handoff easier

- **Future improvement:** A `handoff` command could write one note containing topology, open requests, unACKed results with previews, and next actions so a new Lead could start from that file alone. There is currently no such command; write and maintain the note manually.

### Give each worker a bounded lifetime

- **Current feature:** `new` creates a fresh worker thread/handle; Antigravity creates its actual conversation on first send. Corrections through `send --to "<agent:id>"` reuse the existing conversation. Relay does not reset that conversation's context.
- **Operating tip:** Use one fresh worker per minor unit, and send corrections for that unit to its existing worker after the previous request finishes. Do not accumulate an entire large project in one worker conversation.

## 2. Platform and local-account scope

**Symptom:** A supported-looking setup on another OS, computer, or account may not reproduce recorded results.

**Cause:** Live integration was verified on Windows, on one computer under one OS user. macOS/Linux execution is unverified; state and app access are local. Deep paths also exceeded the legacy Windows path limit in a recorded source-extraction check.

**Fix or workaround:**

- **Operating tip:** Keep the Lead, workers, project, and relay state on one computer under one OS user. Treat macOS/Linux operation as unverified, and use short checkout/state paths on Windows environments with legacy path limits. The relay does not change OS path policies.
- **Current feature:** `doctor` reports detected apps and local configuration; detection is not live round-trip proof. See the [verification summary](verification/README.md) for the tested scope.

## 3. Agent combinations are restricted

**Symptom:** A requested Lead/worker combination or automatic fallback is unavailable.

**Cause:** Claude worker routing is unverified; Codex Lead → Codex worker is unsupported because sandbox queue DB access is unavailable. Antigravity Lead is deferred despite retained implementation. Automatic quota fallback (`fallback next`) is not implemented.

**Fix or workaround:**

- **Current feature:** Claude Lead supports Codex, Antigravity, or both as workers. Codex Lead supports Antigravity workers only, with external queue delivery and the worker's idle Stop hook supporting external recovery.
- **Operating tip:** Select an operational combination explicitly. Do not treat retained Antigravity Lead code, app detection, or `--worker auto` as support for another combination or automatic failover; `auto` selects the configured first worker.

## 4. Antigravity cannot enforce read-only access

**Symptom:** Creating an Antigravity worker with `--sandbox read-only` fails.

**Cause:** Its adapter cannot enforce a read-only sandbox and rejects that option.

**Fix or workaround:**

- **Operating tip:** State allowed files and actions in the brief, understanding that these are instructions rather than enforced isolation.
- **Current feature:** Codex worker creation supports `--sandbox read-only` when the supported topology permits a Codex worker.

## 5. Application updates can break integration

**Symptom:** Delivery, collection, or hooks stop behaving as previously verified.

**Cause:** Relay depends on internal queue, transcript, and hook contracts. Recorded CLI versions are historical observations, not guarantees for future app or desktop UI builds.

**Fix or workaround:**

- **Current feature:** `doctor --root "<absolute-project-path>"` warns on different or unknown app versions; a version mismatch alone is a WARN, not a FAIL. `doctor --report` produces a redacted diagnostic block.
- **Operating tip:** After a Codex update, run `handback selftest` and retain exact failures. Antigravity `selftest --agent antigravity` currently performs detection only and exits 5; it is not a successful runtime transport check. Consult the [verification summary](verification/README.md) for recovery-hook and skill-loading gaps.

## 6. Timeout or unknown delivery does not mean nothing happened

**Symptom:** A wait times out or delivery reports `delivery_unknown`, with no immediate answer.

**Cause:** Submission, task completion, collection, Lead delivery, and ACK are separate events. Acceptance can be uncertain, or collection can finish after a wait ends.

**Fix or workaround:**

- **Operating tip:** Never recreate or resend an uncertain worker request. Keep its request ID and original state home.
- **Current feature:** Inspect `status`, `doctor`, and `explain --root "<absolute-project-path>" --request "<request-id>"`; recover with `wait --root "<absolute-project-path>" --request "<request-id>" --timeout 300`. `wait` does not resend. An unknown Lead-result delivery is not retried automatically; after checking whether it was already received, `inbox redeliver --root "<absolute-project-path>" --id "<message-id>"` explicitly retries the saved result, not the worker task.

## 7. Claude Monitor reception expires

**Symptom:** Automatic result notifications stop, or unread messages repeat when watching resumes.

**Cause:** Claude Monitor expires after at most 30 minutes, and `--idle-exit` stops an idle watcher on purpose. Each watch run emits unread messages once; reading is not ACK. A terminal watcher prints to that terminal, not to the Claude conversation.

**Fix or workaround:**

- **Operating tip:** Re-arm Monitor after expiry while requests remain open, using `inbox watch --idle-exit 1200` for the exact Lead address. Results that arrive while no watcher runs stay in the inbox, and the Claude recovery hook injects them on the next prompt and suggests re-arming. Without Monitor, use explicit `wait` and `inbox list`.
- **Current feature:** ACK processed results with `inbox ack --root "<absolute-project-path>" --for "<lead-address>" --request "<request-id>"`; the original message remains on disk.

## 8. Shared-checkout work must be serialized

**Symptom:** A second task for a busy worker is rejected, or simultaneous workers could edit the same files.

**Cause:** Relay allows one open request per worker conversation within the project state. That guard does not prevent different workers from conflicting over shared files; Git worktrees share relay state.

**Fix or workaround:**

- **Current feature:** The open-request guard rejects a second submission to the same busy conversation.
- **Operating tip:** Run minor units sequentially in a shared checkout. Wait for completion before sending corrections to the same worker, and use a fresh worker for each next unit. A timeout does not release file ownership. The [project rules snippet](project-rules-snippet.md) provides pasteable guidance.

## 9. Dashboard scan cost grows with history

**Symptom:** Dashboard refreshes take longer or allocate more memory as stored history and unread results grow.

**Cause:** Snapshots scan stored request/message history and load unread bodies. Limiting visible rows does not cap that work; ACK retains original messages.

**Fix or workaround:**

- **Operating tip:** ACK results after review to reduce unread bodies retained in the snapshot, and close the optional dashboard when its refresh cost is undesirable; relay delegation does not require it. The inbox scan still reads stored envelopes before filtering ACKed messages, so ACK does not remove historical read/scan cost.
- **Current feature:** `dashboard --once` produces one snapshot instead of a continuously refreshing GUI. The [README measurements](../README.md#optional-dashboard) describe specific short checks, not a memory cap or long-duration stability guarantee.

## 10. Model and reasoning controls depend on the agent

**Symptom:** A role default does not change an existing conversation, or an agent rejects a model/reasoning choice.

**Cause:** Conversation selections are retained ahead of role defaults. Codex native settings are stored before queueing, but a desktop session that already loaded the conversation can retain its runtime configuration. The available controls also differ between agents: Antigravity's current agentapi supports a model only for `new-conversation`. Claude Lead settings are owned by Claude.

**Fix or workaround:**

- **Current feature:** Configure new Codex Lead defaults using `configure --root "<absolute-project-path>" --agent codex --role lead --model "<model-id>" --reasoning-effort ultra`; select `--role worker` for worker defaults. `--clear-model` and `--clear-reasoning-effort` restore inheritance for that role. Existing saved conversation selections take precedence; an explicit Codex `send` selection updates the stored native and relay settings.
- **Operating tip:** Choose a model and effort supported by the installed Codex build. Recognizing `ultra` as a setting does not guarantee every model supports it. Relay settings cover relay creation and delivery; user turns entered directly in the app follow the app's own settings.
- **Current feature:** Select `flash_lite`, `flash`, or `pro` before creating an Antigravity worker. Reasoning selection and model changes on an existing Antigravity conversation are rejected because the current delivery interface cannot apply them. Change Claude Lead settings in Claude.
- **Verification limit:** Automated tests cover selection, precedence, persistence, and transport parameters. Isolated app-server checks confirmed native settings restoration across processes without executing a model turn. Actual next-turn application after changing settings on a conversation already loaded by the desktop app remains unverified; the checks also do not prove live model effort or direct app-entered turn behavior. The [settings verification record](verification/2026-10-09-role-model-settings.md) documents this scope; the earlier live-integration record predates these controls.

## 11. Dashboard placement and conversation links

**Symptom:** The compact widget does not snap to a taskbar, or a conversation link is disabled.

**Cause:** Snapping is implemented for top/bottom Windows taskbars with auto-hide turned off. Conversation links are implemented only for Codex thread handles. Multi-monitor placement and exclusive full-screen Direct3D behavior have unit-test coverage only; they have not been verified live.

**Fix or workaround:**

- **Current feature:** Drag the compact widget freely, within about 20 DPI-scaled pixels of a supported taskbar to dock, or away to undock. The widget saves its coordinates and docking state, clamps off-screen positions, and keeps clear of the notification area when enough space is available. Left/right and auto-hide taskbars do not support snapping; unavailable docking falls back to the bottom-right of the screen.
- **Current feature:** When docked, the widget matches the taskbar colour and restores its position above Explorer when covered. Unchanged content is not periodically repainted, although activity is still checked every three seconds. A full-screen foreground window covering the widget's monitor hides the compact widget whether docked or floating; it returns when the window no longer covers it. Pinned expanded-panel mode does not use this automatic hiding.
- **Operating tip:** For unsupported taskbar layouts, place the floating widget where it is useful or select **보기 설정 → 펼친 패널 고정**. Treat multi-monitor and exclusive full-screen behavior as unverified in a live environment.
- **Current feature:** Codex conversation links open the selected thread. Claude and Antigravity links are disabled; open those conversations in their apps. Stored result bodies remain readable in the dashboard.
