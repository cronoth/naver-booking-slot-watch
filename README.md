# naver-booking-slot-watch

네이버 예약 상품의 **특정 날짜·특정 시간 회차** 잔여석을 감시하고, 매진이 풀리면 [`ntfy`](https://ntfy.sh)로 폰에 알림을 보낸다.

```
[8월 29일 예약] 예약 가능 — 2026-08-29 14:30
잔여 수량: 1
이전 상태: 매진
확인 시각: 2026-07-28 14:30:12 KST
```

## 지원하는 것

- 여러 예약 URL, URL마다 여러 날짜, 날짜마다 여러 시간 회차
- 회차별 잔여 수량 확인과 회차별 독립 상태 관리
- `매진 → 예약 가능` 전환 알림
- 예약 가능 상태에서 잔여 수량이 늘 때 재알림(선택)
- 중복 알림 방지
- 만료 시각이 지나면 자동 중단
- 일일 Heartbeat로 살아 있음 확인
- GitHub Actions에서 5시간 30분 단위 연결 실행

## 하지 않는 것

- **자동 예약을 하지 않는다.** 알림만 보낸다.
- 네이버 로그인·쿠키·CAPTCHA를 다루지 않는다.
- Playwright로 페이지를 렌더링하지 않는다. GraphQL API를 직접 호출한다.

## 동작 방식

```
monitors.json ─┬─ 상품+날짜로 묶어 GraphQL hourlySchedule 1회 호출
               └─ 응답에서 회차별 unitStock - unitBookingCount 계산
                     ├─ 잔여 >= 기준 → available → 이전 상태와 비교해 알림 판단
                     ├─ 잔여 <  기준 → sold_out
                     └─ 오류·응답 구조 변경·회차 없음 → unknown (이전 상태 보존)
                          ↓
                  state/availability.json (회차별 키, 원자적 저장)
```

**조회 실패를 매진으로 취급하지 않는다.** API 오류와 `0석`은 다른 사건이므로 `unknown`으로 두고 직전 정상 상태를 유지한다. 이게 이 도구의 핵심 계약이다.

## 빠른 시작

[`uv`](https://docs.astral.sh/uv/)만 설치하면 된다. 인터프리터와 가상환경은 `uv`가 `.python-version`과 `pyproject.toml`을 보고 맞춘다.

```powershell
uv run --extra dev ruff check .
uv run --extra dev mypy
uv run --extra dev pytest
uv run booking-slot-watch --help
```

**모든 실행은 `uv run`을 쓴다.** 시스템 `python`을 직접 호출하지 않는다. 파이썬 버전의 단일 출처는 `.python-version`과 `pyproject.toml`의 `requires-python`이며, 문서에는 버전을 적지 않는다.

## 감시 대상 설정

[`monitors.json`](monitors.json)이 실제 감시 설정이다. 코드를 고치지 않고 이 파일만 바꿔서 대상을 추가·변경·중단한다. GitHub 웹 편집기로 수정해도 된다.

```json
{
  "version": 1,
  "defaults": {
    "notify_when_remaining_at_least": 1,
    "notify_on_initial_available": true,
    "notify_on_increase": true,
    "error_alert_threshold": 3
  },
  "monitors": [
    {
      "id": "event-20260829",
      "name": "8월 29일 예약",
      "enabled": true,
      "url": "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183",
      "targets": [{ "date": "2026-08-29", "times": ["10:30", "12:30", "14:30"] }],
      "expires_at": "2026-08-29T17:00:00+09:00"
    }
  ]
}
```

지켜야 할 것:

- `id`는 저장소 전체에서 유일해야 한다. 상태 키가 `{id}:{date}:{time}`이다.
- `expires_at`에는 timezone offset을 반드시 넣는다(`+09:00`). 없으면 설정 오류다.
- `times`는 `HH:MM` 형식이다. `9:00`이나 `09:00:00`은 거부된다.
- 중단은 `"enabled": false` 또는 `expires_at`을 과거로 두면 된다.
- 같은 URL·같은 날짜의 여러 시간은 GraphQL 호출 한 번으로 묶인다.

형식 예시는 [`monitors.example.json`](monitors.example.json), 전체 필드 정의는 [`docs/CONFIG_AND_STATE.md`](docs/CONFIG_AND_STATE.md)에 있다.

### 대상 시간을 정확히 알아내기

**추측하면 안 된다.** 판매자가 회차 시각을 바꾸거나 상품을 갈아끼우면 존재하지 않는 시간을 감시하게 되고, 그 회차는 영구히 `unknown`이 되어 알림이 오지 않는다.

`check-once`를 한 번 돌려 로그를 확인한다. 지정한 시간이 응답에 없으면 `error=slot_not_found`가 찍힌다.

```powershell
$env:NTFY_TOPIC = "본인-토픽"
uv run booking-slot-watch check-once
```

## ntfy 앱 설정

1. [ntfy 앱](https://ntfy.sh/app)을 설치한다(Android / iOS / 웹).
2. 추측하기 어려운 무작위 토픽 이름을 정한다. **ntfy.sh는 공개 서버이므로 토픽 이름 자체가 비밀이다.** 이름을 아는 사람은 누구나 알림을 읽고 보낼 수 있다.
3. 앱에서 `+` → 토픽 이름 입력 → `Subscribe`. 서버는 기본값(`ntfy.sh`) 그대로 둔다.
4. 전송이 되는지 확인한다.

```powershell
$env:NTFY_TOPIC = "본인-토픽"
uv run booking-slot-watch send-test-notification
```

자체 ntfy 서버를 쓰면 `NTFY_SERVER_URL`을, 인증이 필요하면 `NTFY_TOKEN`을 설정한다. Heartbeat를 별도 토픽으로 받으려면 `NTFY_HEARTBEAT_TOPIC`을 설정한다(없으면 일반 토픽으로 간다).

## CLI

| 명령 | 설명 | ntfy 필요 |
|---|---|:---:|
| `validate-config` | `monitors.json` 검증 | |
| `has-active-targets` | 활성 대상 존재 여부를 exit code로 반환 | |
| `check-once` | 활성 대상을 한 번 조회하고 필요하면 알림 | O |
| `monitor` | 장시간 반복 감시 루프 | O |
| `send-test-notification` | ntfy 테스트 알림 전송 | O |

공통 옵션: `--config`(기본 `monitors.json`), `--state`(기본 `state/availability.json`)

exit code:

| 코드 | 의미 |
|---:|---|
| `0` | 성공 |
| `1` | 실행 오류 |
| `2` | 설정 오류 |
| `3` | 활성 대상 없음 |
| `4` | 모든 조회 실패 |

### 환경변수

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `NTFY_TOPIC` | 없음(필수) | 알림 토픽 |
| `NTFY_SERVER_URL` | `https://ntfy.sh` | ntfy 서버 |
| `NTFY_TOKEN` | 없음 | Bearer 토큰 |
| `NTFY_HEARTBEAT_TOPIC` | `NTFY_TOPIC` | Heartbeat 전용 토픽 |
| `CHECK_INTERVAL_SEC` | `70` | 조회 간격. `30` 미만은 거부한다 |
| `CHECK_JITTER_SEC` | `20` | 간격에 더할 무작위 지터 |
| — | — | 이 세 값의 기본값은 `monitor.py`가 유일한 출처다. 워크플로는 다시 적지 않는다 |
| `LOOP_MINUTES` | `330` | 한 프로세스가 사는 시간(분). 5시간 30분. `10`~`350` 범위만 허용 |

## GitHub Actions 운영

한 job이 약 5시간 30분 살아 있으면서 70~90초마다 조회하고, 끝날 때 활성 대상이 남아 있으면 다음 실행을 트리거한다. 체인이 끊기면 5시간 cron이 복구한다. 모든 대상이 만료·비활성이면 연결 실행을 만들지 않고 멈춘다.

### Secret 등록

```powershell
gh secret set NTFY_TOPIC --body "본인-토픽"
gh secret list
```

선택 항목:

```powershell
gh secret set NTFY_SERVER_URL --body "https://ntfy.example.com"
gh secret set NTFY_TOKEN --body "tk_..."
gh secret set NTFY_HEARTBEAT_TOPIC --body "별도-토픽"
```

### 최초 실행과 로그

```powershell
gh workflow list
gh workflow run monitor.yml
gh run list --workflow monitor.yml --limit 5
gh run watch
gh run view <RUN_ID> --log
```

### 감시 대상 변경·중단

`monitors.json`을 고쳐서 push하면 실행 중인 job이 취소되고 새 설정으로 즉시 다시 시작한다(`concurrency` + `cancel-in-progress`).

```powershell
git add monitors.json
git commit -m "config: update booking targets"
git push
```

상태 파일 커밋은 `push` 트리거 대상이 아니다(`state/**`가 `paths`에 없음). 그래서 상태 커밋이 워크플로를 다시 켜는 무한 루프가 생기지 않는다.

중단은 `"enabled": false` 또는 `expires_at`을 과거로 두면 된다. 지난 날짜의 `targets`는 자동으로 제외되므로, 대상 날짜가 전부 지나간 모니터는 그것만으로 비활성이 된다.

감시가 완전히 끝났으면 복구용 cron이 5시간마다 빈 job을 띄우지 않도록 워크플로를 끈다.

```powershell
gh workflow disable monitor.yml
```

### `concurrency` 동작

`push`만 실행 중인 job을 즉시 취소하고 새 설정으로 다시 시작한다. `schedule`과 `workflow_dispatch`는 취소하지 않고 큐에서 기다린다.

- `schedule`: cron 주기(5시간)가 루프 길이(5시간 30분)보다 짧아서, 취소하게 두면 복구용 cron이 매번 살아 있는 job을 죽여 자기 연결 실행이 영원히 일어나지 않는다.
- `workflow_dispatch`: 연결 실행이 스스로 dispatch하는 순간 마무리 중인 부모 job이 취소돼, 정상 인계가 모두 이력에 `cancelled`로 남는다. 그러면 진짜 취소와 정상 인계를 구분할 수 없다.

따라서 이력의 `cancelled`는 "설정을 push해서 새 설정으로 재시작했다"는 뜻으로만 읽으면 된다.

### exit code와 워크플로의 관계

`monitor`가 `3`(활성 대상 없음)으로 끝나면 job은 성공으로 처리하되 다음 실행을 만들지 않는다. `1`·`2`로 끝나면 job이 실패해 연결이 끊기고, 5시간 cron이 복구한다. 루프는 조회 실패로 종료하지 않으므로 `4`를 반환하지 않는다.

## 프로젝트 구조

```text
.github/workflows/
  monitor.yml            5시간 30분 감시 job, 상태 커밋, 다음 실행 연결, cron 복구
  test.yml               ruff / mypy / pytest
src/booking_slot_watch/
  __main__.py            CLI 진입점과 exit code
  models.py              설정 자료형 (frozen dataclass)
  config.py              monitors.json 로딩·검증, URL 파싱, 요청 그룹화
  naver.py               hourlySchedule 클라이언트와 응답 파싱 순수 함수
  state.py               상태 파일 입출력, 상태 전이, 알림 판단 순수 함수
  notifier.py            ntfy 전송
  monitor.py             1회 조회 오케스트레이션과 반복 루프
tests/                   테스트, fixtures/에 실제 응답 형태
monitors.json            실제 감시 설정
monitors.example.json    형식 예시
state/availability.json  회차별 상태 (Actions가 커밋한다)
```

## 문제 해결

**알림이 안 온다**
`uv run booking-slot-watch send-test-notification`으로 경로부터 확인한다. 성공하는데 안 보이면 앱 구독 토픽이 다르다. 실패하면 `NTFY_TOPIC`이 비었거나 서버에 못 닿는다.

**회차가 계속 `unknown`이다**
로그의 `error=` 값을 본다.
- `slot_not_found` — 그 날짜에 다른 회차는 있는데 **지정한 시간이 없다.** `monitors.json`의 `times`가 실제 회차와 다르다. 이게 가장 흔한 원인이다.
- `empty_schedule` — 그 날짜에 회차가 아예 없다. 판매일이 아니다.
- `malformed_response` — 네이버가 응답 구조를 바꿨다. 파서를 고쳐야 한다.
- `rate_limited` — 403/429다. 자동으로 백오프하지만 계속되면 `CHECK_INTERVAL_SEC`를 올린다.

**매진인데 알림이 왔다 / 잔여가 있는데 안 온다**
`stock=0`인 회차는 정원 자체가 0이라 화면에 "매진"으로 보이지만 취소가 나도 잔여가 생기지 않는다. 판매자가 정원을 열면 그때 알림이 간다.

**Actions job이 바로 실패한다**
exit code를 본다. `2`면 설정 오류다 — `NTFY_TOPIC` Secret이 없거나 `monitors.json`이 잘못됐다. `gh run view <RUN_ID> --log`로 첫 ERROR 줄을 확인한다.

**감시가 멈췄다**
`uv run booking-slot-watch has-active-targets`를 돌린다. `3`이면 만료되거나 비활성이라 정상 종료된 것이다. `0`인데 실행이 없으면 체인이 끊긴 것이므로 `gh workflow run monitor.yml`로 다시 시작한다(cron도 5시간 안에 복구한다).

**같은 알림이 여러 번 온다**
전송이 확인되지 않으면 `last_notified_remaining`을 갱신하지 않고 다음 루프에서 재시도한다. 상태 커밋이 rebase 충돌로 푸시되지 못한 경우에도 다음 실행이 예전 상태로 시작해 재알림할 수 있다. Actions 로그의 `상태 파일 rebase 충돌` 경고를 확인한다.

## 주의

- **공개 저장소다.** `monitors.json`의 예약 URL·모니터 이름·날짜·시간, 상태 파일에 기록된 잔여 수량, Actions 로그가 모두 공개된다. `NTFY_TOPIC`과 `NTFY_TOKEN`은 Secret에만 저장한다.
- **네이버 GraphQL은 공식 공개 API가 아니다.** 웹 프런트엔드가 쓰는 내부 인터페이스에 의존하므로 네이버가 응답 구조를 바꾸면 동작이 깨진다. 실제로 참고했던 저장소가 쓰던 `saleStartDate` 필드는 이미 스키마에서 사라졌다.
- Actions를 무기한 상시 서버로 쓰는 것은 보장된 운영 모델이 아니다. 감시가 끝나면 자동으로 멈추도록 `expires_at`을 넣는다.
- 이 도구는 알림만 보낸다. 예약은 사람이 한다.

## 설계 문서

- [`CLAUDE_HANDOFF.md`](CLAUDE_HANDOFF.md): 구현 요청 핵심 요약
- [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md): 기능·범위·상태 전이 명세
- [`docs/CONFIG_AND_STATE.md`](docs/CONFIG_AND_STATE.md): 설정 및 상태 JSON 형식
- [`docs/GITHUB_ACTIONS.md`](docs/GITHUB_ACTIONS.md): 장시간 Actions 운영 설계
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md): 단계별 구현·테스트·완료 기준
- [`docs/REFERENCES.md`](docs/REFERENCES.md): 참고 저장소와 채택·제외할 부분

## 라이선스

[MIT](LICENSE)
