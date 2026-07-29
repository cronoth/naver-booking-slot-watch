# GitHub Actions 운영 설계

## 1. 기본 운영 모델

GitHub Actions cron은 최소 5분 간격이며 지연될 수 있으므로, 단순 cron 1회 실행 방식으로 감시하지 않는다.

대신 하나의 Action job이 약 5시간 30분 동안 살아 있으면서 내부에서 70~90초마다 GraphQL 조회를 반복한다.

```text
Action A 시작
→ 약 5.4시간 동안 반복 조회
→ 상태 저장·커밋
→ 활성 대상이 남아 있으면 Action B 수동 트리거
→ Action B가 같은 동작 반복
```

체인이 끊긴 경우를 복구하기 위해 별도 schedule 이벤트를 둔다.

## 2. 워크플로 이벤트

권장 `monitor.yml` 개념 구조:

```yaml
name: Naver Booking Slot Monitor

on:
  workflow_dispatch:

  push:
    branches: [main]
    paths:
      - "monitors.json"
      - "src/**"
      - "pyproject.toml"
      - ".github/workflows/monitor.yml"

  schedule:
    - cron: "7 */5 * * *"

permissions:
  contents: write
  actions: write

concurrency:
  group: naver-booking-slot-watch
  cancel-in-progress: true
```

### `concurrency`

설정 또는 코드가 변경되면 실행 중인 기존 모니터를 취소하고 새 설정으로 즉시 시작한다.

자체 연결 실행 과정에서 이전 job과 다음 job이 겹치지 않도록 주의한다.

단, `cancel-in-progress: true`를 모든 이벤트에 적용하면 안 된다. cron 주기(5시간)가 `LOOP_HOURS`(5.4시간)보다 짧으므로, 복구용 schedule 실행이 정상 동작 중인 job을 자기 종료 시각 24분 전에 매번 취소한다. 그러면 `Trigger next run` 단계가 실제로는 한 번도 실행되지 않고, 설계한 연결 실행 모델이 아니라 5시간마다 재시작하는 모델이 돌아간다.

schedule은 취소하지 않고 큐에서 기다리게 한다. 체인이 살아 있으면 복구 실행은 그냥 대기하다 교체되고, 체인이 죽었을 때만 실제로 인수한다.

`workflow_dispatch`도 취소 대상에서 빼야 한다. 연결 실행이 스스로 dispatch하는 순간 마무리 중인 부모 job이 취소되기 때문이다. 스텝은 모두 성공하고 상태 커밋과 다음 실행 트리거까지 끝난 뒤에 취소되므로 동작에는 문제가 없지만, 정상 인계가 전부 이력에 `cancelled`로 남아 진짜 취소와 구분할 수 없게 된다.

결국 `push`만 즉시 취소한다.

```yaml
concurrency:
  group: naver-booking-slot-watch
  cancel-in-progress: ${{ github.event_name == 'push' }}
```

### 참고 저장소와의 대조

`DuckOnDesk/naver-booking-monitor`의 실제 워크플로와 비교하면 다음이 다르다.

| 항목 | 참고 저장소 | 이 프로젝트 |
|---|---|---|
| `cancel-in-progress` | `false` (무조건) | `push`일 때만 |
| `push.paths` | `.monitor_restart_request` 센티넬 파일 | `monitors.json`, `src/**` 등 |
| 설정 읽기 | 실행 중 GitHub raw에서 fetch | 시작 시 로컬 체크아웃 1회 |
| 연결 조건 | `if: ${{ !cancelled() }}` | `if: success()` |
| `timeout-minutes` | 360 | 350 |

저쪽이 `cancel-in-progress: false`로 단순할 수 있는 것은 설정을 런타임에 원격에서 가져오기 때문이다. 설정이 바뀌어도 재시작할 이유가 없다. 이 프로젝트는 설정을 시작 시 한 번 읽으므로(PROJECT_SPEC 단계 8) 설정 push에는 재시작이 필요하고, 그래서 `push`만 취소하는 조건이 붙는다. 런타임 원격 fetch는 `REFERENCES.md`가 채택 제외로 지정한 항목이다.

`if: ${{ !cancelled() }}`는 채택하지 않는다. 이 프로젝트는 설정 오류에 exit 2로 즉시 종료하므로, 실패해도 연결하면 수십 초 만에 실패하고 다시 트리거하는 폭주 루프가 된다. `if: success()`가 그 안전장치이며, 대신 일시적 실패는 회차별 예외 가드와 상태 보존으로 흡수하고 복구는 cron에 맡긴다.

`timeout-minutes: 360`은 GitHub-hosted 러너의 job 실행 상한과 같아서 실제로 발동할 수 없다. 350으로 두어 플랫폼이 죽이기 전에 워크플로가 먼저 정리하게 한다.

## 3. job 구조

```yaml
jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 350

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: astral-sh/setup-uv@v9.0.0
        with:
          enable-cache: true

      - name: Monitor
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          NTFY_SERVER_URL: ${{ secrets.NTFY_SERVER_URL }}
          NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}
          NTFY_HEARTBEAT_TOPIC: ${{ secrets.NTFY_HEARTBEAT_TOPIC }}
        # CHECK_INTERVAL_SEC·CHECK_JITTER_SEC·LOOP_HOURS는 여기에 적지 않는다.
        # 코드 기본값(monitor.py)이 유일한 출처다. 두 곳에 적으면 어긋난다.
        run: uv run booking-slot-watch monitor

      - name: Commit state
        if: always()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add state/availability.json
          if git diff --cached --quiet; then
            exit 0
          fi
          git commit -m "chore: update monitor state"
          git pull --rebase origin main
          git push origin HEAD:main

      - name: Trigger next run
        if: ${{ success() }}
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          uv run booking-slot-watch has-active-targets
          gh workflow run monitor.yml --repo "${{ github.repository }}"
```

`uv`가 `.python-version`을 보고 인터프리터를 내려받으므로 워크플로에 파이썬 버전을 적지 않는다.

위 예시는 개념 구조다. 실제 구현에서는 `has-active-targets`가 비활성 상태를 exit code로 표현하도록 정한다.

예시:

```text
exit 0 = 활성 대상 있음
exit 3 = 활성 대상 없음
exit 1 = 설정 오류
```

셸에서 exit 3을 정상적인 “다음 실행 생략”으로 다루는 별도 분기가 필요하다.

## 4. push 재귀 방지

상태 파일 커밋으로 `push` 이벤트가 다시 모니터를 시작하지 않도록 `push.paths`에 `state/**`를 포함하지 않는다.

재시작 대상:

- `monitors.json`
- 소스코드
- 워크플로
- 패키지 설정

## 5. 중복 실행과 연결 안전성

고려할 상황:

- schedule 복구 실행과 기존 장시간 실행이 겹침
- 설정 push로 기존 실행 취소
- 이전 실행의 연결 트리거와 새 push 실행이 동시에 발생
- 상태 커밋 충돌

대응:

1. `concurrency` 그룹 하나 사용
2. `cancel-in-progress: true`
3. 상태 커밋 전 `git pull --rebase`
4. 상태 파일 병합 충돌 시 무리하게 덮어쓰지 않고 로그 후 종료
5. 새 실행이 시작되면 최신 `main` 상태 사용

## 6. Secret 설정

필수:

```powershell
$topic | gh secret set NTFY_TOPIC
```

선택:

```powershell
"https://ntfy.sh" | gh secret set NTFY_SERVER_URL
$token | gh secret set NTFY_TOKEN
$heartbeatTopic | gh secret set NTFY_HEARTBEAT_TOPIC
```

`NTFY_SERVER_URL`이 없으면 코드 기본값으로 `https://ntfy.sh`를 사용한다.

## 7. 최초 실행

```powershell
gh workflow list
gh workflow run monitor.yml
gh run list --workflow monitor.yml --limit 5
gh run watch
```

## 8. 공개 저장소 Actions 사용 주의

- 공개 저장소의 표준 러너 실행 시간은 private 저장소의 포함 분과 별도로 취급된다.
- 작업 하나의 실행 시간 제한은 여전히 존재한다.
- Actions를 무기한 범용 서버로 사용하는 것은 안정적으로 보장되는 운영 모델이 아니다.
- 특정 예약 감시가 끝나면 자동으로 실행을 멈춘다.
- 활성 대상이 없을 때 계속 연결 실행하면 안 된다.
- 장기간 다수 대상을 운영하려면 OCI Docker로 이전할 수 있도록 핵심 로직을 Actions와 분리한다.

## 9. 테스트 워크플로

```yaml
name: Test

on:
  pull_request:
  push:
    branches: [main]
    paths:
      - "src/**"
      - "tests/**"
      - "pyproject.toml"

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v9.0.0
        with:
          enable-cache: true
      - run: uv sync --extra dev
      - run: uv run ruff check .
      - run: uv run pytest
      - run: uv run mypy
```

## 10. 운영 명령

설정 변경:

```powershell
code monitors.json
git add monitors.json
git commit -m "config: update booking targets"
git push
```

중단:

```json
"enabled": false
```

수동 재실행:

```powershell
gh workflow run monitor.yml
```

최근 로그:

```powershell
gh run list --workflow monitor.yml --limit 10
gh run view <RUN_ID> --log
```
