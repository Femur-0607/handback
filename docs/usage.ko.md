# agent-relay 한국어 사용 설명서

## 만든 이유

처음 바람은 단순했습니다. 내 컴퓨터에 있는 에이전트 앱들이 서로 통신하되, 그 대화를 각 앱에서 직접 눈으로 확인하고 싶었습니다. 그래서 워커는 숨은 하위 프로세스가 아니라 해당 앱 안의 실제 대화로 만들어지며, 언제든 열어서 진행 과정을 볼 수 있습니다.

쓰다 보니 반복되는 인계가 문제였습니다. 작업을 보내고, 어느 대화가 맡았는지 기억하고, 답을 찾아 원래 대화로 가져와야 했습니다. 대화가 닫히거나 대기가 끊기면 작업이 끝났는지, 다시 보내야 하는지도 불분명해졌습니다. agent-relay는 요청마다 식별자를 붙이고 결과를 디스크에 보관합니다. 중단 뒤에도 같은 요청을 다시 찾을 수 있고, 확인한 결과는 ACK로 처리 완료를 표시하므로 같은 작업을 두 번 보낼 위험이 줄어듭니다.

## 한 명령으로 첫 작업 확인

설치 후 일반 터미널에서 `agent-relay try --root "<프로젝트 절대 경로>"`를 실행합니다. 소스 실행은 `python agent_relay.py try`입니다. 설정·Codex 탐지를 확인하고 읽기 전용 워커 하나를 만들어 답변만 요청합니다. 저장된 결과의 요청 식별자와 `RELAY_OK`를 검증한 뒤 ACK합니다. 스킬이나 훅은 설치하지 않습니다.

- `--lead`: 저장된 조합이 없을 때 사용할 주소(기본 `claude:lead`). 기존 Codex 워커 조합은 그대로 재사용하고 다른 조합은 변경 없이 `use` 명령을 안내합니다.
- `--timeout`: 기본 300초, `0`은 무기한. 시간 초과·접수 불명에는 출력된 `wait --request` 명령으로 회수하며 다시 보내지 않습니다.
- `--keep`: 결과를 ACK하지 않고 남깁니다. `--json`: 구조화된 결과를 출력합니다.

요청 ID 하나로 결과를 찾고 처리할 수 있습니다.

```powershell
agent-relay inbox list --root "<프로젝트 절대 경로>" --for "<Lead 주소>" --request "<요청 ID>"
agent-relay inbox ack --root "<프로젝트 절대 경로>" --for "<Lead 주소>" --request "<요청 ID>"
```

`ack`는 `--id`와 `--request` 중 하나만 받으며 `--for`가 필수입니다. 해당 수신자의 결과·오류 메시지만 ACK하고 처리한 메시지 ID를 출력합니다. 아직 결과가 없으면 거부합니다. `wait`와 동기 `send`는 표준 출력의 답변을 유지하며 표준 오류에 결과 메시지 ID를 출력합니다. 동기 `new`의 JSON에는 `message_id`가 추가됩니다.

지원 조합은 Claude Lead → Codex 및/또는 Antigravity, Codex Lead → Antigravity입니다. Codex → Codex는 샌드박스의 queue DB 접근 제한 때문에 지원하지 않으며 Claude 워커 라우팅은 검증되지 않았습니다. Antigravity 대상으로 `--sandbox read-only` 또는 읽기 전용을 명시한 브리프를 보내면 강제 권한이 아닌 지시뿐이라는 경고가 출력됩니다. 기존 어댑터의 `--sandbox read-only` 거부 동작은 유지합니다.


[프로젝트 소개](../README.md) · [English quick start](quickstart.md) · [기여 안내](../CONTRIBUTING.md) · [검증 범위](verification/README.md)

**실험적 프로젝트 · Windows 검증 · Python 3.10 이상.** 앱 업데이트에 따라 내부 연동 방식이 달라질 수 있습니다. macOS·Linux는 미검증이며 지원을 보장하지 않습니다.

Lead가 Codex·Antigravity 워커에게 작업을 보내고 결과를 영속 수신함으로 받는 로컬 중계 도구입니다. Lead는 작업을 나누고 결과를 처리하며, 워커는 지정된 브리프를 수행해 같은 대화에 답합니다. 범위는 **PC 한 대·사용자 한 명**입니다. 에이전트가 보낸 메시지는 작업 정보이며 사용자 승인으로 취급하지 않습니다.

## 지원 조합

| Lead | 워커 | 결과 수신 |
|---|---|---|
| Claude | Codex | Monitor와 복구 훅 |
| Claude | Antigravity | Monitor와 복구 훅 |
| Claude | Codex + Antigravity | 워커별 수신함 결과 |
| Codex | Antigravity만 | 샌드박스 밖 `codex queue`, 수신함·ACK |

**미지원:** Claude 워커, Codex Lead → Codex 워커, `fallback next` 자동 전환.

**보류:** Antigravity Lead. 관련 내부 코드는 남아 있지만 운영 지원 조합이 아닙니다.

Windows 실제 왕복·동시 수신·Lead 전환·ACK의 확인 범위와 남은 한계는 [검증 요약](verification/README.md)에 정리했습니다. Claude Desktop Monitor는 검증했으며, Desktop의 전역 복구 훅 로딩은 완전히 검증하지 않았습니다.

## 설치

Python 3.10 이상과 표준 라이브러리를 사용합니다. 선택한 앱을 설치하고 로그인해야 합니다.

PyPI 배포 후(현재 미배포)에는 `uv tool install agent-relay` 또는 `pipx install agent-relay`로 설치합니다. uv는 필요한 Python도 준비할 수 있습니다. 현재 소스에서는 저장소 폴더에서 `uv tool install .` 또는 `pipx install .`을 실행합니다. 설치 후에는 `agent-relay` 명령을 사용합니다. 선택적인 `agent-relay-dashboard` GUI에는 Tk를 포함한 Python이 필요합니다.

패키지를 설치하지 않고 `python agent_relay.py`로 직접 실행하는 방식도 유지됩니다. 패키지 스킬·훅은 해당 환경의 절대 Python 경로와 `-m agent_relay`를 사용하며, 미설치 체크아웃은 절대 스크립트 경로를 사용합니다. 환경이나 체크아웃을 이동했다면 스킬·훅을 다시 설치합니다. 아래 예시의 `python agent_relay.py`는 패키지 사용 시 `agent-relay`로 바꿀 수 있습니다.

### 스킬 설치

```powershell
agent-relay install-skills --dry-run
agent-relay install-skills
# 별도 시험 홈의 앱·설정만 탐지하는 예
agent-relay install-skills --target-home "<absolute-test-home>" --dry-run
```

`install-skills`는 패키지의 공통 스킬을 바탕으로 세 앱의 설치 대상을 처리합니다. 실행 파일 탐지는 알려진 사용자 설치 경로와 PATH를 사용합니다. Claude는 탐지 여부와 무관하게 기존 설치 방식을 유지합니다.

| 대상 | 설치 위치·조건 |
|---|---|
| Claude | `~/.claude/skills/agent-relay/SKILL.md`에 항상 설치 |
| Codex | 앱 탐지 시 `CODEX_HOME/skills/agent-relay/SKILL.md`, 미지정이면 `~/.codex/skills/agent-relay/SKILL.md` |
| Antigravity | 앱 탐지 후 `~/.gemini/config/skills.json`의 해석 가능한 첫 등록 경로 아래 `agent-relay/SKILL.md`. 등록 위치를 확인할 수 없으면 건너뜀 |

기존 스킬 파일은 타임스탬프 `.bak`으로 백업합니다. `--dry-run`은 예정 경로만 출력하며 파일을 만들지 않습니다. `--target-home`을 명시하면 호스트 PATH·CODEX_HOME을 참조하지 않고 대상 홈 안의 앱·설정만 사용하며, 홈 밖의 Antigravity 등록 경로는 건너뜁니다. 공유 AGENTS.md·GEMINI.md, 훅, topology, 검색 경로 등록 파일은 수정하지 않습니다. 임시 홈 설치·백업은 검증했으며 새 설치본의 실제 앱 로딩·자동 트리거는 [검증 요약](verification/README.md)의 미검증 항목입니다.

`install.ps1`은 같은 Python 명령을 호출하는 호환 래퍼입니다. 소스 직접 실행 시 `python agent_relay.py install-skills`를 사용합니다.

### 훅과 Antigravity 준비

1. Antigravity를 쓴다면 체크아웃을 앱에서 프로젝트로 등록합니다. relay는 프로젝트를 만들지 않습니다.
2. 선택한 상태 홈의 `config.json`에서 `agents.antigravity.enabled`를 `true`로 설정합니다. 기존 설정에 병합합니다. 선택적 `model`은 `flash_lite`, `flash`, `pro`이며 기본값은 `flash`입니다.
3. 사용할 에이전트의 훅을 미리 확인한 뒤 설치합니다. 아래 예의 `--agents`는 사용하는 앱 목록으로 바꿉니다. Claude 복구 훅도 필요하면 `claude`를 포함합니다.

```powershell
python agent_relay.py install-hooks --agents codex,antigravity --state-home "<absolute-state-home>" --dry-run
python agent_relay.py install-hooks --agents codex,antigravity --state-home "<absolute-state-home>"
python agent_relay.py uninstall-hooks --agents codex,antigravity --state-home "<absolute-state-home>" --dry-run
# 임시 설치를 끝낼 때 같은 상태 홈의 소유 기록으로 제거
python agent_relay.py uninstall-hooks --agents codex,antigravity --state-home "<absolute-state-home>"
```

훅 설치는 기존 설정을 백업·병합하고 제거는 relay 소유 항목만 대상으로 합니다. `--state-home`은 relay 상태·소유 기록 위치이며 앱 설정을 격리하는 옵션이 아닙니다. Codex 훅은 `CODEX_HOME` 또는 `~/.codex`의 `hooks.json`, Antigravity 훅은 `~/.gemini/config/hooks.json`에 둡니다. **Codex의 새 훅·변경된 훅은 사용자가 CLI `/hooks`에서 검토하고 신뢰 승인해야 합니다.** 설치기는 신뢰 설정을 바꾸지 않습니다.

Antigravity는 따옴표가 든 훅 명령을 실행하지 않으므로 공백 경로에는 Windows 8.3 짧은 경로를 사용하고, 그래도 공백이 남으면 설치를 거부합니다. Claude 복구에는 실제 세션 ID를 포함한 Lead 주소가 필요합니다.

## 조합 전환

```powershell
python agent_relay.py status --root "<absolute-checkout>"
python agent_relay.py doctor --root "<absolute-checkout>"
python agent_relay.py use --root "<absolute-checkout>" --lead "claude:<session-id>" --workers codex,antigravity
python agent_relay.py use --root "<absolute-checkout>" --lead "codex:<thread-id>" --workers antigravity
python agent_relay.py new --role lead --worker codex --cwd "<absolute-checkout>" --name "relay Lead"
```

`status`는 조합·설정 출처·열린 요청과 전달 상태를, `doctor`는 앱 탐지·제약·상태 경고를 읽기 전용으로 보여 줍니다. Claude의 `--workers`는 `codex`, `antigravity`, `codex,antigravity` 중에서 고릅니다. `use`는 저장소 밖 전환 상태만 바꾸므로 **조합 전환에 프로젝트 규칙 수정이 필요 없습니다.** 이미 열린 요청의 `return_to`는 발송 당시 Lead를 유지합니다.

`new --role lead`는 만든 핸들을 Lead로 등록합니다. Codex Lead의 기본 워커는 Antigravity이며 `--workers antigravity`로 명시할 수 있습니다. 새 Codex Lead에는 relay 상태 홈과 Antigravity 설정 폴더를 추가 쓰기 허용하고 기존 Lead의 권한은 바꾸지 않습니다. `codex:lead` 같은 Lead 별칭과 동일 Lead·워커 핸들은 거부합니다.

Lead 초기 지시는 사용자가 그 대화에 입력합니다. `send --to <Lead>` 또는 `new --role lead --text/--file`로 보내지 않습니다. 워커는 다른 워커에게 재위임하지 않고 할당받은 대화에 결과를 답합니다.

### 현황판

```powershell
pythonw dashboard.pyw
python agent_relay.py dashboard --once
python agent_relay.py dashboard --autostart on
```

`dashboard.pyw`는 기본으로 작업표시줄 알림 영역 왼쪽에 두 줄 위젯을 띄웁니다. 프로젝트 이름, 진행 수, 미확인 결과 수를 3초마다 갱신하며 남은 프로젝트는 `+N`으로 표시합니다. 작업표시줄이 아래쪽이 아니거나 자동 숨김 상태이거나 위치를 감지하지 못하면 작업 영역 오른쪽 아래에 배치합니다. 위젯을 클릭하면 리더 → 워커 조합과 마지막 활동 시각을 포함한 패널이 위로 열립니다. 미확인 결과는 리더에게 온 결과·오류 중 ACK하지 않은 것이며, 리더가 보낸 요청 사본은 세지 않습니다. 목록은 상태 홈 `projects/`에 기록이 있는 프로젝트 전체이며 기본은 최근 활동순입니다. 루트 폴더가 없거나 임시 폴더 아래에 있는 시험 프로젝트는 빼고 보여 줍니다.

- 위젯 클릭: 패널 열기·닫기. 패널 밖 클릭 또는 Esc로 닫기. 진행·미확인 숫자는 워커 대화를 열거나 선택 메뉴를 표시
- 패널의 프로젝트 이름 클릭: 진행 중인 워커 대화와 최근 미확인 결과 5개를 펼침. 결과 줄을 클릭하면 전체 본문 창이 열림
- 프로젝트 줄 우클릭: 위로 올리기·아래로 내리기·숨기기, 미확인 결과 모두 확인 처리, relay 등록 해제
- 빈 곳 우클릭 → 보기 설정: 작업표시줄 모드·펼친 패널 고정 선택, 패널 표시 줄 수(2줄·3줄·전부, 기본 3줄), 숨긴 프로젝트 다시 표시, 순서 초기화
- 위젯 드래그: 작업표시줄을 따라 가로로 이동하고 위치 저장. 고정 패널은 자유롭게 이동. 패널의 `+N개 더 보기`로 나머지 프로젝트 표시

순서·숨김·패널 줄 수·모드·위젯의 가로 위치는 상태 홈의 `dashboard.json`에 저장합니다. relay 상태를 바꾸는 동작은 확인 창을 거친 두 가지뿐입니다.

- **모두 확인 처리**: `inbox ack`와 같이 그 프로젝트의 결과·오류를 ACK합니다. 내용은 수신함에 남고, 리더의 Monitor·복구 훅이 다시 전달하지 않습니다.
- **relay 등록 해제**: 진행 중 요청이 없을 때만 그 프로젝트 상태 폴더를 상태 홈 `released/`로 옮깁니다(삭제 아님). 그 프로젝트에서 relay를 다시 쓰면 새로 등록됩니다.

`--autostart on`은 Windows 시작프로그램 폴더에 바로가기를 만들고 `off`는 지웁니다. 띠는 사용자 세션당 하나만 뜹니다. `--once`는 현황을 JSON으로 출력합니다.

## 위임과 수신

### 생성·전송·회수

```powershell
python agent_relay.py new --worker auto --cwd "<absolute-checkout>" --name "<소분류 ID> <짧은 목표>" --file "<absolute-brief>" --no-wait
# 짧은 지시를 보내는 별도 생성 예
python agent_relay.py new --worker auto --cwd "<absolute-checkout>" --name "<소분류 ID> <짧은 목표>" --text "작업 지시" --timeout 300
# 같은 소분류의 이전 요청이 끝난 뒤 정정
python agent_relay.py send --root "<absolute-checkout>" --to "<agent>:<worker-id>" --text "정정 지시" --no-wait
python agent_relay.py wait --root "<absolute-checkout>" --request "<request_id>" --timeout 300
```

소분류마다 새 워커 스레드 하나를 만들고 같은 체크아웃의 소분류는 순차 실행합니다. 같은 소분류의 정정은 같은 스레드로 보냅니다. 같은 프로젝트 상태에서 워커 대화당 열린 요청은 하나만 허용합니다. `--worker auto`는 현재 조합의 첫 워커를 고르며, `--worker`를 생략하면 기존 호환 동작대로 Codex를 고릅니다.

`new`에 지시가 없으면 빈 스레드/임시 핸들을 만들고, `--worker` 생략 시 원본 ID, 명시 시 `<agent>:<id>` 한 줄을 출력합니다. Codex 스레드는 앱에서 열며 `--no-open`으로 생략할 수 있습니다. Antigravity는 첫 전송 때 실제 대화를 만듭니다. `--text`와 `--file`은 상호 배타이며 `--file`은 파일 내용 대신 워커가 읽을 절대 경로를 전달합니다.

지시가 있는 `new`는 생성·첫 전송을 한 번 수행하고 JSON을 출력합니다. `created_handle`은 최초 핸들, `handle`·`id`는 전송 뒤 실제 대상이며 `request_id`, `marker`, `to`, `status`도 포함합니다. 비동기에는 `collector_pid`, 동기에는 `result`가 들어갑니다. Antigravity의 `pending-...` 핸들은 첫 전송에서 실제 대화에 바인딩되며 이후 같은 임시 핸들도 그 대화를 가리킵니다.

`send`는 기본적으로 답을 기다립니다. `--no-wait`는 접수 뒤 요청 ID·마커·대상·상태·수집기 PID를 JSON으로 출력합니다. 분리 수집기는 Lead가 닫혀도 결과를 수신함에 기록합니다. 기본 수집 제한은 24시간이고 `send --collect-timeout`으로 바꾸거나 `--no-collect`로 기동을 생략합니다. Codex Lead의 샌드박스에서는 외부 수집을 위해 기동을 생략할 수 있어 PID가 `null`일 수 있습니다.

`--timeout 0`은 무기한 대기입니다. `new`·`send`·`wait`의 `--return-file`은 성공한 답을 UTF-8 파일에도 씁니다. 수집기가 종료됐거나 시간이 초과돼도 `wait --request` 또는 `inbox watch`로 같은 요청을 회수합니다. 같은 요청의 결과 메시지 ID는 일정하여 수신함 기록이 중복되지 않습니다.

**접수 불명·시간 초과에서는 재생성·재전송하지 않습니다.** `status`로 열린 요청을 확인한 뒤 요청 ID로 `wait`합니다. `wait`는 다시 보내지 않습니다.

| 종료 코드 | 의미 |
|---|---|
| `0` | 동기 전송·대기의 답 수집 성공. `--no-wait`에서는 접수 성공만 의미하며 다른 명령에서는 해당 작업 성공 |
| `2` | 워커 턴 실패·중단. 명령행 인자 오류에도 사용 |
| `3` | 수집 시간 초과. 요청은 열린 상태로 유지 |
| `4` | 전송 실패·접수 불명, `wait`에서 확인한 미전송 요청. `send`의 설정·실행 예외도 포함 |
| `5` | 그 밖의 명령의 설정·기능 사용 불가·실행 오류. Antigravity selftest의 런타임 검사 미구현도 포함 |

Codex `selftest` 점검 실패는 `1`입니다. 종료 코드만으로 접수 여부를 추정하지 말고 요청 상태를 함께 확인합니다. 기존 `send --thread <id>`와 `wait --thread <id> --marker "[relay ...]"`도 유지하며, 기존 `send --thread ... --no-wait`는 JSON 대신 마커 한 줄을 출력합니다.

### 수신함과 ACK

```powershell
python agent_relay.py inbox watch --root "<absolute-checkout>" --for "<lead-address>" --timeout 60
python agent_relay.py inbox list --root "<absolute-checkout>" --for "<lead-address>"
python agent_relay.py inbox ack --root "<absolute-checkout>" --for "<lead-address>" --id "<message-id>"
# 결과의 접수 불명을 확인하고 중복 처리 가능성을 검토한 뒤에만 실행
python agent_relay.py inbox redeliver --root "<absolute-checkout>" --id "<message-id>"
```

`<lead-address>`는 발송 때의 `claude:<session-id>` 또는 `codex:<thread-id>`입니다. 기본 설정의 주소는 `claude:lead`입니다. 수신·ACK에는 결과가 기록된 정확한 `AGENT_RELAY_HOME`을 사용합니다.

`watch`는 실행 중 같은 메시지를 한 번 출력하고 빈 수신함에서는 조용히 기다립니다. 매 확인 전에 해당 Lead로 돌아올 열린 요청도 수집합니다(`--no-collect`로 생략). Claude Lead는 요청을 보낸 직후 `--idle-exit 1200`을 붙여 Monitor로 실행합니다. 열린 요청도 새 메일도 없이 20분이 지나면 watch가 스스로 종료해 대기 중 메모리를 쓰지 않습니다. 열린 요청이 남은 채 Monitor가 최대 30분으로 만료되면 다시 겁니다. 같은 주소의 새 watch가 시작되면 이전 watch는 종료됩니다. 설치한 Claude 복구 훅은 열린 요청이 있는데 살아 있는 watch가 없을 때 대화를 시작하거나 메시지를 보낼 때 실행 명령을 안내하므로, 매번 따로 지시하지 않아도 됩니다. 수집 예외는 요청별 2~300초 지수 간격으로 재시도하며 프로젝트 `log/watch-collector.log`에 기록합니다. 읽기·출력은 ACK가 아닙니다. 처리한 메시지를 `ack`해야 다음 watch·복구에서 반복되지 않으며 원문은 삭제하지 않습니다. 큰 답은 결과 파일로 보존하고 수신함에 경로를 기록합니다.

결과의 Lead 전달 상태는 `inbox/delivered/<id>.json`에 기록됩니다. 워커 요청 상태와 별개입니다.

| 전달 상태 | 의미·복구 |
|---|---|
| `pending` | 샌드박스 등으로 미전달. 외부 라우터가 재시도 가능 |
| `delivered` | Lead 측 접수. 처리·ACK 완료를 의미하지 않음 |
| `failed` | 확정 전달 실패. 자동 재시도 가능 |
| `delivery_unknown` | 접수 불명. 자동 재시도하지 않음. 확인 후 명시적 `inbox redeliver`만 허용 |

`redeliver`는 저장된 결과를 Lead에게 다시 전달하는 명령이며 워커 작업을 재전송하는 명령이 아닙니다.

## 알려진 한계

- Lead와 재사용하는 워커 대화는 문맥이 누적됩니다. 작은 작업은 Lead에서 처리하고, 중간 작업은 워커에 위임해 결과만 검토하며, 큰 작업은 계획 문서와 단계별 새 Lead 세션으로 나누는 운영 방식을 권합니다. 단계 사이에는 문서로 인계합니다.
- Lead를 바꿔도 기존 요청의 반환 주소는 유지됩니다. 새 Lead의 훅·watch는 이전 주소의 결과를 자동으로 받지 않습니다. 같은 상태 홈에서 `inbox list --for "<old-lead-address>" --request "<request-id>"`로 읽고, 검토 후 같은 주소와 요청 ID로 `inbox ack`합니다. 명령에 `--root "<absolute-checkout>"`을 지정합니다.
- 검증 범위는 Windows의 한 컴퓨터·한 OS 사용자이며, 지원 조합에 제한이 있습니다. Antigravity는 읽기 전용을 강제하지 못합니다. 앱 업데이트 뒤에는 진단이 필요하고, 접수 불명·시간 초과에서는 재전송 없이 기존 요청을 회수해야 합니다.
- Claude Monitor는 열린 요청이 남은 채 만료되면 다시 걸고, 같은 체크아웃의 작업은 순차 실행합니다. 현황판의 스캔 비용은 저장된 이력에 따라 커집니다.

[알려진 한계와 우회 방법](limitations.md)은 현재 기능·운영 팁·미구현 개선안을 구분하며, Lead 교체 명령과 결과 크기·미리보기의 정확한 동작을 설명합니다. 자동 인계 명령이나 결과 요약·길이 강제 옵션은 현재 기능이 아닙니다.

## 어댑터별 한계

### Codex

- 샌드박스 안에서는 queue 상태 DB에 접근할 수 없어 Lead 반환을 `pending`으로 둡니다. relay가 연 Antigravity 워커의 유휴 Stop 훅은 외부 라우터를 기동해 누락된 수집기와 대기·확정 실패 전달을 복구합니다. Codex Lead는 Antigravity 워커만 지원합니다.
- 워커 완료의 근거는 rollout의 `task_complete`입니다. 관찰 훅의 Stop만으로 완료 처리하지 않습니다. Codex Lead 복구는 정확한 Lead의 `UserPromptSubmit`에 `additionalContext`를 넣으며 SessionStart는 사용하지 않습니다.
- 새 워커의 `--sandbox`는 기본 `workspace-write`, 선택적으로 `read-only`입니다. `--add-dir`는 Codex용 추가 쓰기 경로입니다. 모델·effort 설정은 아직 실제 스레드 생성에 적용하지 않습니다.
- 일부 Windows 샌드박스에서 상대 경로 resolve 오류를 확인했습니다. `--root`·`--cwd`·브리프는 절대 경로를 사용합니다. 실행 파일은 `AGENT_RELAY_CODEX`, 사용자 `agents.codex.executable`, 앱 번들, PATH 순서로 탐색하고 기록은 `CODEX_HOME` 또는 `~/.codex`에서 읽습니다. `python agent_relay.py codex`로 실행 파일 경로를 확인합니다.

### Antigravity

- agentapi는 사이드카 안에서 실행합니다. 전송마다 relay 소유 사이드카를 `~/.gemini/config/config.json`에 잠깐 켜고 agentapi를 한 번 호출합니다. 설정을 백업하고 동시 변경을 거부합니다. 성공 확인 뒤 항목·폴더를 제거하지만 앱 종료 등으로 제한시간(기본 60초)을 넘기면 `delivery_unknown`과 비활성 사이드카를 남깁니다. 다시 보내지 않습니다.
- `--sandbox read-only`는 거부합니다. 읽기 전용을 강제할 수 없으므로 제한을 브리프에 씁니다. 앱의 명령 자동 실행 설정이 켜져 있으면 워커가 명령을 바로 실행할 수 있습니다.
- 반환은 마커가 있는 `transcript_full.jsonl` 입력 이후의 답과 `fullyIdle` Stop 관찰로 확정합니다. 다른 Stop 훅의 최대 제한시간도 기다립니다. 마커 없는 앱 직접 입력은 relay 답으로 수집하지 않습니다.
- `cleanup-sidecars`는 이름·명령·소유 설명을 확인한 비활성 항목 중 열린 요청이 없는 것만 백업·제거합니다. 다른 상태 홈 항목과 열린 `delivery_unknown`은 보존합니다. `doctor --json`의 `sidecar_cleanup`에서 대상 개수를 확인할 수 있습니다.

```powershell
python agent_relay.py cleanup-sidecars --dry-run
python agent_relay.py cleanup-sidecars
```

Antigravity `selftest --agent antigravity`는 **설치 탐지만 하고 종료 5**를 반환합니다. 런타임 자체 시험은 구현되지 않았으며 실제 왕복 실패 판정이나 전체 전송 검사 통과로 해석하면 안 됩니다.

## 상태 폴더와 잠금

`AGENT_RELAY_HOME`이 지정되면 그 경로를 사용합니다. 기본 위치는 다음과 같습니다.

| 운영체제 | 기본 상태 홈 |
|---|---|
| Windows | `%USERPROFILE%\.agent-relay` |
| macOS | `~/Library/Application Support/agent-relay` |
| Linux | `${XDG_STATE_HOME:-~/.local/state}/agent-relay` |

`<state>/projects/<key>/`에 `topology.json`, `threads.json`, `requests/`, `inbox/`, `log/`를 둡니다. 같은 Git 저장소의 워크트리는 Git 공통 디렉터리 기준으로 상태를 공유하고, Git이 아닌 폴더는 루트별로 분리합니다.

설정은 내장 기본값 → 사용자 `<state>/config.json` → 프로젝트 `.agent-relay.json` → `use`의 전환 상태 → 환경변수 `AGENT_RELAY_LEAD`·`AGENT_RELAY_WORKERS`와 명시적 선택값 순서로 적용합니다. 기본 조합은 Claude Lead·Codex 워커·`fallback ask`입니다. 사용자 비활성 설정과 프로젝트 `allowed_agents`는 이후 계층으로 우회할 수 없습니다.

잠금은 `<state>/.locks/`에 두고 구 버전과의 배타성을 위해 부모의 `.agent-relay-lock-<hash>.lock`도 읽기 전용으로 열어 함께 잡습니다. 잠금 파일은 삭제하지 않습니다. 처음 쓰는 상태 홈의 호환 잠금은 샌드박스 밖에서 생성해야 하며, 생성이 차단되면 내부 잠금만으로 진행하지 않고 명령을 거부합니다. 이후에는 상태 폴더 쓰기 권한을 사용합니다. `status`·`doctor`·`--dry-run`은 잠금을 만들지 않습니다.

### Windows MSIX 이전 상태

Claude Desktop의 MSIX 가상화로 이전 `%LOCALAPPDATA%\agent-relay`가 앱 밖의 폴더와 달라질 수 있습니다. 이전 폴더가 남아 있으면 새 기본 경로의 쓰기 명령은 상태 분리를 막기 위해 거부합니다. 열린 작업은 `AGENT_RELAY_HOME`을 **실제 이전 폴더**로 지정해 마치며, relay가 이전 위치를 추측해 자동 이전하지 않습니다.

1. 열린 요청·기존 Lead의 대기 명령·Monitor·수집기를 모두 끝냅니다. 열린 요청 또는 기록된 살아 있는 수집기 PID가 있으면 이전이 거부되며, 구버전은 PID를 기록하지 않았을 수 있습니다.
2. 대상 상태 홈을 확인합니다. 기본 새 위치를 쓸 때는 이전 경로를 가리키던 `AGENT_RELAY_HOME`을 해제하고, 별도 목적지는 이 변수로 명시합니다. 원본은 `--from`으로 지정합니다.
3. 미리보기를 확인한 뒤 명시적으로 이전합니다. 운영 상태 이전은 별도 승인 작업입니다.

```powershell
python agent_relay.py migrate-state --from "<실제 이전 상태 폴더>" --dry-run
python agent_relay.py migrate-state --from "<실제 이전 상태 폴더>"
```

`--dry-run`은 파일을 만들지 않습니다. 실제 이전은 쓰기 작업과 같은 잠금을 잡고 원본을 보존하며 복사본 SHA-256을 확인합니다. 대상 상태 폴더와 `.locks`는 교체하지 않고 데이터만 게시합니다. 대상 파일과 내용이 충돌하면 덮어쓰지 않습니다. 원본에는 `MOVED.json`을 기록해 이후 읽기는 새 위치를 안내하고 쓰기는 거부합니다.

이전 뒤에는 **새 상태 폴더에 복사된 소유 기록**을 사용해 설치돼 있던 에이전트 훅만 `uninstall-hooks --agents <목록> --state-home <새 상태 홈>` → `install-hooks --agents <목록> --state-home <새 상태 홈>` 순서로 다시 설치합니다. 기존 백업을 보존하고 Codex 훅의 신뢰 상태도 확인합니다. 상태 이전의 확인 범위는 [검증 요약](verification/README.md)을 참고하세요.

## 앱 업데이트 후 점검

```powershell
python agent_relay.py selftest
python agent_relay.py selftest --agent antigravity
python agent_relay.py doctor --root "<absolute-checkout>"
```

Codex 업데이트 후 `selftest`로 queue·app-server·기록 이벤트를 점검합니다. Antigravity selftest는 앞서 설명한 탐지 한계가 있으므로 종료 5와 진단 내용을 구분해서 읽습니다. `doctor`로 설치 버전·전달 경로·상태 경고를 확인합니다. 실패하면 정확한 오류를 보존합니다. 앱 내부 queue 계약·rollout·훅 형식의 장기 안정성은 보장하지 않습니다.

## 개발과 검증 기록

`agent_relay.py`는 호환 진입점이고 구현은 `agent_relay/`, 단위 시험은 `tests/`입니다.

```powershell
python -m unittest
```

공개 문서에는 개인 경로·대화 ID·원본 대화 기록을 제외한 [검증 요약](verification/README.md)을 제공합니다. 단위 시험과 실제 앱 왕복 검증은 별도이며, 단위 시험 통과만으로 설치된 앱 버전과의 호환성을 보장하지 않습니다. 변경 제안과 검증 방법은 [기여 안내](../CONTRIBUTING.md)를 참고하세요.

## 결과가 돌아오지 않을 때: 진단과 로컬 통계

먼저 `doctor`, 이어서 저장된 요청 ID의 `explain`을 실행합니다. `explain`은 생성 → 발송 → 수집 프로세스 → inbox 결과 → Lead 전달 → ACK 순서와 다음 조치 하나를 표시합니다. 기록되지 않은 시각은 추정하지 않습니다. 두 명령은 발송·수집·ACK를 수행하지 않습니다.

```powershell
python agent_relay.py doctor --root "<absolute-checkout>"
python agent_relay.py explain --root "<absolute-checkout>" --request "<request-id>"
python agent_relay.py doctor --root "<absolute-checkout>" --report
python agent_relay.py status --root "<absolute-checkout>" --stats --days 30
```

`doctor`의 기본 출력은 OK/WARN/FAIL 체크리스트이며 WARN/FAIL마다 다음 조치 하나가 붙습니다. FAIL이 있으면 종료 5, 경고만 있으면 종료 0입니다. 앱 버전 불일치는 미검증 WARN입니다. 쓰기 권한은 파일 생성 없이 추정하며, 훅 등록 확인은 실제 실행이나 신뢰 설정 검증을 뜻하지 않습니다. 기존 JSON 소비자는 `doctor --json`을 사용해야 하며 기존 필드와 종료 동작이 유지됩니다. 타임라인은 `explain --json`으로도 읽을 수 있습니다.

`doctor --report`는 OS·Python·도구·앱 버전, 검사 등급, Lead/worker 종류만 포함하는 버그 보고용 블록입니다. 홈 경로는 `~`로, 사용자 지정 상태 경로는 자리표시자로 표시하고 프로젝트 경로와 세션·스레드 ID를 제외합니다.

`status --stats`는 기존 로컬 상태만 읽으며 외부로 전송하지 않습니다. `--days N`은 요청 생성 시각 기준 최근 N일이며 생략하면 전체입니다. 완료·실패·불확실·기타 열린 요청 수, 생성부터 결과 확정까지의 중앙값과 p90(nearest-rank), 추가 Lead 전달 시도, 미ACK 결과 수를 보여 줍니다. 추가 시도에는 자동 재시도도 포함됩니다. 수집 시간초과 이력, `wait` 복구 횟수, 명시적 redeliver 횟수는 기존 상태에서 구분할 수 없어 0으로 표시하지 않고 산출 불가로 설명합니다.
