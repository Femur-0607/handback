# Python 최소 버전과 CI 정리 (2026-10-21)

- **문제**: Python 3.10이 2026-10-01에 지원 종료됐는데 `requires-python`, 진단, 문서, CI 매트릭스가 아직 3.10을 기준으로 한다. 빌드 출처 증명 액션도 한 단계 뒤처졌고, 액션 버전을 알려 줄 장치가 없다.
- **방향**: 최소 버전을 3.11로 올리고(패키지 메타데이터, `doctor`/대시보드 검사, 문서), 테스트·스모크 매트릭스를 3.11/3.14로 바꾼다. `attest-build-provenance`를 v4.2.2로 올린다. Dependabot이 `github-actions`와 빌드 도구(`packaging/requirements-build.txt`)를 주 1회, 출시 후 14일 대기를 두고 제안하게 한다.
- **유지할 것**: 배포 exe와 wheel 빌드에 쓰는 Python 3.13, SHA 고정 방식, 이미 최신인 액션.
- **하지 않을 것**: 릴리스·버전 올림, 3.10 전용 우회 코드 삭제나 3.11 문법으로 바꾸는 리팩터링, `gh-action-pypi-publish` v1.14.3(출시 하루째라 Dependabot 대기 후 반영), Inno Setup 7 이전.
