# naver-booking-slot-watch

네이버 예약 상품의 **특정 날짜·특정 시간 회차** 잔여석을 감시하고, 매진 상태가 풀리면 [`ntfy`](https://ntfy.sh)로 알림을 보내는 도구다.

- 조회는 Playwright 없이 네이버 예약 GraphQL(`hourlySchedule`)을 직접 호출한다.
- 감시 대상은 `monitors.json` 하나만 수정해서 추가·변경·중단한다.
- 자동 예약은 하지 않는다. 알림만 보낸다.

## 현재 구현 상태

단계 3까지 완료된 상태다.

- 단계 1: 패키지 구조, `pyproject.toml`, `ruff`/`pytest`/`mypy` 설정, CLI 진입점 골격
- 단계 2: `monitors.json` 로딩·검증(`config.py`), 자료형(`models.py`), URL 파싱, 기본값 병합, 활성 대상 계산, 상품·날짜 단위 요청 그룹화
- 단계 3: 네이버 GraphQL `hourlySchedule` 클라이언트(`naver.py`), 응답 파싱 순수 함수, timeout·재시도·403/429 백오프
- 단계 4: 상태 파일 읽기·원자적 저장(`state.py`), 회차 상태 키, 오류 누적, Heartbeat 상태
- 단계 5: 상태 전이와 알림 판단 순수 함수(`evaluate_slot`, `evaluate_error`)
- 단계 6: ntfy 전송(`notifier.py`) — 예약 가능·오류·Heartbeat·테스트 알림
- 단계 7: 1회 실행 CLI(`monitor.py` 오케스트레이션 + `__main__.py` 배선)
- 단계 8: 장시간 반복 루프(`run_loop`) — 종료 시각, 주기와 지터, SIGTERM 처리, 종료 직전 저장

남은 것은 단계 9(GitHub Actions 워크플로)와 단계 10(문서 마무리)이다. 단계별 범위와 완료 기준은 [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)에 있다.

### 루프 환경변수

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `CHECK_INTERVAL_SEC` | `70` | 조회 간격. `30` 미만은 거부한다 |
| `CHECK_JITTER_SEC` | `20` | 간격에 더할 무작위 지터 |
| `LOOP_HOURS` | `5.5` | 한 프로세스가 살아 있는 시간 |

`check-once`와 `send-test-notification`은 `NTFY_TOPIC` 환경변수가 필요하다.

```powershell
$env:NTFY_TOPIC = "추측하기-어려운-무작위-문자열"
uv run booking-slot-watch send-test-notification
uv run booking-slot-watch check-once
```

## 감시 대상 설정

[`monitors.json`](monitors.json)이 실제 감시 설정이고, [`monitors.example.json`](monitors.example.json)이 형식 예시다. 코드 수정 없이 `monitors.json`만 바꿔서 대상을 추가·변경·중단한다.

- 대상 중단: 해당 모니터의 `"enabled": false`
- 상태 키는 `{monitor id}:{date}:{time}`이므로 `id`는 저장소 전체에서 유일해야 한다
- `expires_at`에는 반드시 timezone offset을 넣는다(`+09:00`)
- 같은 URL·같은 날짜의 여러 시간은 GraphQL 호출 한 번으로 묶인다

전체 필드 정의는 [`docs/CONFIG_AND_STATE.md`](docs/CONFIG_AND_STATE.md)에 있다.

## 빠른 시작

[`uv`](https://docs.astral.sh/uv/)만 설치하면 된다. 인터프리터와 가상환경은 `uv`가 `.python-version`과 `pyproject.toml`을 보고 맞춘다.

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run mypy
uv run booking-slot-watch --help
```

**모든 실행은 `uv run`을 쓴다.** 시스템 `python`을 직접 호출하지 않는다. 파이썬 버전의 단일 출처는 `.python-version`과 `pyproject.toml`의 `requires-python`이며, 문서에는 버전을 적지 않는다.

## CLI

| 명령 | 설명 |
|---|---|
| `validate-config` | `monitors.json` 검증 |
| `check-once` | 활성 대상을 한 번 조회하고 필요하면 알림 |
| `monitor` | 장시간 반복 감시 루프 |
| `has-active-targets` | 활성 대상 존재 여부를 exit code로 반환 |
| `send-test-notification` | ntfy 테스트 알림 전송 |

exit code:

| 코드 | 의미 |
|---:|---|
| `0` | 성공 |
| `1` | 실행 오류 |
| `2` | 설정 오류 |
| `3` | 활성 대상 없음 |
| `4` | 모든 조회 실패 |

## GitHub Actions 운영

한 job이 약 5.4시간 동안 살아 있으면서 70~90초마다 조회하고, 끝날 때 활성 대상이 남아 있으면 다음 실행을 트리거한다. 체인이 끊기면 5시간 cron이 복구한다. 모든 대상이 만료·비활성이면 연결 실행을 만들지 않고 멈춘다.

### Secret 등록

```powershell
$topic = Read-Host "ntfy 토픽"
$topic | gh secret set NTFY_TOPIC
```

선택 항목:

```powershell
"https://ntfy.sh" | gh secret set NTFY_SERVER_URL
$token | gh secret set NTFY_TOKEN
$heartbeatTopic | gh secret set NTFY_HEARTBEAT_TOPIC
```

`NTFY_SERVER_URL`이 없으면 `https://ntfy.sh`를 쓴다.

### 최초 실행과 로그

```powershell
gh workflow list
gh workflow run monitor.yml
gh run list --workflow monitor.yml --limit 5
gh run watch
gh run view <RUN_ID> --log
```

### 감시 대상 변경·중단

`monitors.json`을 고쳐서 push하면 실행 중인 job이 취소되고 새 설정으로 다시 시작한다(`concurrency` + `cancel-in-progress`).

```powershell
git add monitors.json
git commit -m "config: update booking targets"
git push
```

중단은 해당 모니터의 `"enabled": false` 또는 `expires_at`을 과거로 두면 된다. 다음 실행 연결이 자동으로 멈춘다.

### exit code와 워크플로의 관계

`monitor`가 `3`(활성 대상 없음)으로 끝나면 job은 성공으로 처리하되 다음 실행을 만들지 않는다. `1`·`2`로 끝나면 job이 실패해 연결이 끊기고, cron이 복구한다.

## 주의

- **공개 저장소다.** `monitors.json`의 예약 URL·모니터 이름·날짜·시간, 상태 파일의 잔여 수량, Actions 로그는 모두 공개된다. `NTFY_TOPIC`과 `NTFY_TOKEN`은 GitHub Actions Secret에만 저장하고, 토픽 이름은 추측하기 어려운 무작위 문자열을 쓴다.
- **네이버 GraphQL은 공식 공개 API가 아니다.** 웹 프런트엔드가 쓰는 내부 인터페이스에 의존하므로, 네이버가 응답 구조를 바꾸면 동작이 깨질 수 있다.
- 조회 실패는 매진으로 취급하지 않는다(`unknown`). 실패 시 이전 상태를 유지한다.

## 설계 문서

- [`CLAUDE_HANDOFF.md`](CLAUDE_HANDOFF.md): 구현 요청 핵심 요약
- [`docs/PROJECT_SPEC.md`](docs/PROJECT_SPEC.md): 기능·범위·상태 전이 명세
- [`docs/CONFIG_AND_STATE.md`](docs/CONFIG_AND_STATE.md): 설정 및 상태 JSON 형식
- [`docs/GITHUB_ACTIONS.md`](docs/GITHUB_ACTIONS.md): 장시간 Actions 운영 설계
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md): 단계별 구현·테스트·완료 기준
- [`docs/REFERENCES.md`](docs/REFERENCES.md): 참고 저장소와 채택·제외할 부분

## 라이선스

[MIT](LICENSE)
