# Antigravity 다른 도구의 깨진 훅 — 방향 (2026-11-17, 이슈 #8)

- **문제**: 다른 도구(ai-memory)가 남긴 Antigravity 훅의 실행 파일이 지워져 도구 호출마다 훅이 실패했다. 한국어 Windows의 CP949 stderr가 대화 단계에 들어가 `proto: ... invalid UTF-8`로 멈췄고, 그 대화는 이후 보낼 때마다 같은 오류로 멈춘다. relay 결과에는 Stop 훅의 마지막 오류만 실려 원인(훅 실패 단계)이 보이지 않았다.
- **풀 방향**: (1) `doctor`가 전역·`.agents/`·플러그인 hooks.json에서 handback 외 훅의 실행 파일이 없으면 `WARN`으로 파일·그룹·이벤트·경로를 알린다. (2) 수집 실패 오류에 `invalid UTF-8`이 있으면 transcript의 마지막 ERROR 단계(이번 요청 이전 단계 포함)를 붙이고, 훅 점검과 `new`로 다시 보내라는 안내를 덧붙인다. (3) `docs/limitations.md`에 한 항목을 적는다.
- **하지 않을 것**: 다른 도구의 훅을 고치거나 지우기, 훅을 실행해 보는 점검, 멈춘 대화 복구, Codex와 같이 쓰는 transcript 리더의 디코딩 방식 변경(실측 transcript는 이미 유효한 UTF-8이고 깨진 바이트는 대체 문자로 들어 있다).
- **실측 (Antigravity 2.25.0)**: 실패한 대화의 `transcript_full.jsonl`에 `type=GENERIC, status=ERROR` 단계가 있고, `error` 필드가 `JSON hook "jsonhook__ai-memory_PreToolUse_0_0" failed: command failed: exit status 1, stderr: ����…`이다. 같은 대화로 다시 보낸 요청은 새 ERROR 단계 없이 Stop만 `invalid UTF-8`로 끝났다.
