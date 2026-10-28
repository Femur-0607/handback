# 정기 앱 호환성 CI 점검 (2026-10-28)

- **문제**: 현황판의 호환성 점검(`compat.scan`)은 사용자 PC에 새 앱 버전이 깔린 *뒤에만* 돈다. Codex CLI는 npm에 데스크톱 번들보다 먼저 올라오므로(현재 npm latest 0.165.0, 알파 0.166.0-alpha.x, 로컬 번들 0.163.0), 깨지는 변경을 사용자보다 늦게 안다.
- **풀 방향**: GitHub Actions에 주 1회(+수동 실행) 워크플로를 두고, Windows 러너에 npm의 최신 Codex CLI(`latest`, `alpha` 두 채널)와 Claude Code를 깔아 `compat.run_checks`를 그대로 돌린다. 실패하면 `compat.issue_title`/`compat.summary`로 GitHub 이슈를 연다. 같은 제목의 열린 이슈가 있으면 새로 만들지 않는다.
- **하지 않을 것**: 사용자 PC의 AI 앱 자동 업데이트, Antigravity 점검(러너에 설치 불가), 실제 에이전트 턴 실행, `compat.py`·현황판 동작 변경, 검증 버전 기록(`app-compat.json`)을 CI에서 갱신하기.
