# Codex native settings precedence and conversations open in the app

Checks recorded on **October 15, 2026** used Windows, the app-bundled Codex CLI **0.162.0-alpha.17.2**, and desktop package **26.1007.9446.0**. They describe those installed builds, not later versions.

No model turn was executed. App-server checks used a temporary `CODEX_HOME` and a temporary project directory. The `codex queue` subprocess was intercepted, so nothing reached the desktop app. The installed desktop bundle was inspected statically. No user conversation log was read and no existing user conversation setting was changed. The triggering conversation was examined earlier with read-only `thread/read` only.

## Incident

A `send` to an existing Codex worker failed with `thread/resume … already has an active writer`. Handback's saved record for that conversation said `reasoning_effort: ultra`; the conversation's own metadata said `high`. The saved record was passed to the adapter as if it were a request, so `deliver()` saw a difference and tried to resume the conversation to restore `ultra`, while the desktop app had it open. The earlier hypothesis that missing `thread/read` fields fell back to config defaults was wrong: both fields were present.

## Observations

| Check (isolated state) | Result |
|---|---|
| Create with `ultra`, then read from a new process | `ultra` persisted; `gpt-6-astra` lists `ultra` among supported efforts. |
| Plain `thread/resume` without overrides, then read again | `ultra` unchanged. Resuming alone does not lower effort. |
| Process A resumes (holds) the thread and updates effort to `high`; process B reads | B sees `high` immediately. B's `thread/read` status is `notLoaded` although A holds the thread. |
| Process B calls `thread/resume` while A holds it | `-32600 … already has an active writer`, the reported error. |
| Process B calls `thread/settings/update` without resuming | `thread not found`; settings cannot be changed without becoming the writer. |

`thread/read` status is per app-server process. It cannot tell a separate process that the desktop app has the conversation open. The resume error is the only signal, and its JSON-RPC code is generic, so the adapter matches the error message.

Static inspection of the desktop bundle (`app-initial` and `app-shared` webview assets) found that the composer's effort resolution discards `ultra` unless a feature gate is on. When `ultra` is not one of the model's slider positions, it falls back to the last non-`ultra` slider value. The remembered "last started" model setting also excludes `ultra` by schema. The "show Ultra in model picker slider" setting defaults to off.

**Most likely path:** the desktop app, as the conversation's writer, resolved the effort to the highest offered non-`ultra` value (`high`) and stored it. The isolated checks confirm that such a write lands in native metadata at once. Whether the write happened on load or on turn start, and the state of the feature gate on this account, were **not** confirmed; that would need the real app on a user conversation.

## Decision

1. **Native settings are canonical for an existing conversation.** The app can change them and can refuse `ultra`, so handback's saved record goes stale. The saved record and role defaults now only fill in missing native metadata. A send without flags never resumes a conversation to "restore" a saved value. It queues with a warning naming both values, and the saved record is then updated to the native value.
2. **Only flags on this send (`--model`, `--reasoning-effort`) are requests.** The CLI passes them separately as `overrides`. If they differ from native settings, the adapter resumes and updates as before.
3. **Explicit request while the app holds the conversation: fail clearly and send nothing.** Queueing anyway would run the turn with settings the caller explicitly rejected. The error says that nothing was sent, so a retry is safe. It suggests changing the setting in the app, or closing the conversation there and sending again.
4. **Missing native metadata while the app holds the conversation: queue with a warning.** Nothing was explicitly requested, and the app's current selection will run the turn.
5. **Any other resume or settings failure still blocks queueing**, as before.

A `status`-based check (skip resume when loaded) was rejected because `status` cannot see the desktop app's hold.

## Verification

- Against the real bundled app-server in isolated state, with one process holding the thread at `high`, both new paths behaved as designed. With saved `ultra` and no flags, the message was queued (intercepted) with the native-settings warning. With `overrides={"reasoning_effort": "ultra"}`, the send was refused with the explicit message and nothing was queued.
- New regression tests in `tests/test_codex.py` cover saved `ultra` against native lower effort, whether the conversation is held or not. They also cover: no warning when defaults match; an explicit override refused while the app holds the conversation; an explicit override equal to native needing no resume; missing metadata queued with a warning; other resume failures still blocking; `rpc_error` detection from `AppServer.call`; and unknown override keys. `tests/test_cli.py` checks that only send flags become overrides and that delivery warnings are printed.
- Full `python -m unittest`: 701 tests, OK, one skip.

## Remaining limits

- The exact trigger and gate state behind the app's `ultra` → `high` write are unconfirmed (see above).
- Whether a native settings update made while the app does **not** hold the conversation affects the app's next turn after it reopens the conversation remains as in the [October 9 record](2026-10-09-role-model-settings.md).
- Detecting the app's hold relies on the error text `active writer`. A future wording change would make such sends fail with the raw error instead of the explanation; nothing would be queued.
