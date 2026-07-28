# 구현 계획과 완료 기준

## 1. 구현 단계

### 단계 1 — 프로젝트 초기화

구현 항목:

- Python 패키지 구조
- `pyproject.toml`
- `ruff`
- `pytest`
- `.gitignore`
- README 기본 작성
- CLI 진입점

완료 확인:

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
uv run mypy
uv run booking-slot-watch --help
```

### 단계 2 — 설정 모델

구현 항목:

- `monitors.json` 읽기
- URL 파싱
- 날짜·시간·만료 검증
- 기본값 병합
- 활성 대상 계산
- 동일 날짜 요청 그룹화

권장 자료형:

```python
@dataclass(frozen=True)
class BookingIdentifiers:
    business_type_id: int
    business_id: str
    biz_item_id: str

@dataclass(frozen=True)
class SlotTarget:
    date: date
    times: tuple[time, ...]

@dataclass(frozen=True)
class MonitorConfig:
    id: str
    name: str
    enabled: bool
    url: str
    identifiers: BookingIdentifiers
    targets: tuple[SlotTarget, ...]
    threshold: int
    notify_on_initial_available: bool
    notify_on_increase: bool
    expires_at: datetime | None
```

테스트:

- 정상 URL
- 잘못된 URL
- 중복 ID
- 잘못된 시간
- timezone 없는 만료
- 활성 대상 없음
- 만료된 대상 필터

### 단계 3 — 네이버 GraphQL 클라이언트

구현 항목:

- `hourlySchedule` 요청
- 요청 헤더
- timeout·재시도
- 응답 파싱
- 회차별 잔여 수량 추출
- API 오류 모델

반환 예시:

```python
@dataclass(frozen=True)
class SlotAvailability:
    start_at: datetime
    stock: int
    booking_count: int
    remaining: int
    sale_enabled: bool
```

테스트 fixture:

- 정상 다중 회차
- 매진
- 잔여 1석
- `isUnitSaleDay=false`
- GraphQL errors
- 필드 누락
- HTTP 429
- 대상 시간 없음

`대상 시간 없음`은 즉시 매진으로 단정하지 않는다. API 응답이 정상이고 해당 날짜에 전체 슬롯이 있지만 지정 시간이 없어진 경우와 응답 자체가 불완전한 경우를 구분한다.

### 단계 4 — 상태 저장

구현 항목:

- JSON 읽기
- 파일 없음 초기화
- 손상 파일 오류
- 원자적 저장
- 회차 상태 키
- Heartbeat 상태
- 오류 횟수

테스트:

- round trip
- 원자적 저장
- 기존 상태 유지
- 오류 누적
- 정상 조회 후 오류 초기화

### 단계 5 — 상태 전이와 알림 판단

순수 함수로 구현한다.

```python
def evaluate_slot(
    previous: SlotState | None,
    remaining: int,
    threshold: int,
    notify_on_initial_available: bool,
    notify_on_increase: bool,
    checked_at: datetime,
) -> EvaluationResult:
    ...
```

테스트 매트릭스:

| 이전 | 현재 잔여 | 결과 | 알림 |
|---|---:|---|---|
| 없음 | 0 | sold_out | 없음 |
| 없음 | 1 | available | 설정에 따라 |
| sold_out 0 | 1 | available | 있음 |
| available 1 | 1 | available | 없음 |
| available 1 | 2 | available | 증가 설정 시 |
| available 2 | 1 | available | 없음 |
| available 1 | 0 | sold_out | 없음 |
| sold_out 0 | 조회 실패 | sold_out 유지 | 없음 |

### 단계 6 — ntfy

구현 항목:

- 일반 알림
- 오류 알림
- Heartbeat
- click URL
- priority·tags
- 선택 Bearer token
- 실패해도 조회 상태 처리와 분리

알림 전송 실패 시 `last_notified_remaining`을 성공한 것처럼 갱신하면 안 된다.

### 단계 7 — 1회 실행 CLI

```powershell
uv run booking-slot-watch check-once
```

기능:

- 설정 검증
- 모든 활성 대상 한 번 조회
- 결과 출력
- 필요 시 알림
- 상태 저장
- exit code 명확화

추가 명령:

```powershell
uv run booking-slot-watch validate-config
uv run booking-slot-watch has-active-targets
uv run booking-slot-watch send-test-notification
```

### 단계 8 — 장시간 모니터 루프

```powershell
uv run booking-slot-watch monitor
```

기능:

- 종료 시각 계산
- 반복 조회
- 주기와 지터
- SIGTERM 처리
- 종료 직전 상태 저장

`monitors.json`은 프로세스 시작 시 한 번 읽어도 된다. GitHub Actions에서는 설정 push가 기존 job을 취소하고 새 job을 시작하도록 구성한다.

### 단계 9 — GitHub Actions

- `test.yml`
- `monitor.yml`
- 상태 커밋
- 다음 실행 연결
- 활성 대상 없으면 종료
- cron 복구

### 단계 10 — 문서

README에 포함:

- 목적
- 지원 기능
- 구조
- 빠른 시작
- `monitors.json` 예시
- ntfy 앱 설정
- GitHub Secret 설정
- Actions 실행
- 대상 추가·중단 방법
- 공개 저장소 주의
- 네이버 비공식 API 의존성
- 자동 예약을 하지 않는다는 점
- 문제 해결

## 2. 권장 `pyproject.toml`

`requires-python`은 실제 `pyproject.toml`과 `.python-version`이 단일 출처이므로 아래 예시에서는 생략한다.

```toml
[project]
name = "naver-booking-slot-watch"
version = "0.1.0"
dependencies = [
  "requests>=2.32,<3",
  # Windows에는 시스템 tz 데이터베이스가 없어 zoneinfo가 실패한다
  "tzdata>=2025.1; sys_platform == 'win32'",
]

[project.optional-dependencies]
dev = [
  "pytest>=8,<9",
  "ruff>=0.12,<1",
  "mypy>=1.17,<2",
  "responses>=0.25,<1",
]

[project.scripts]
booking-slot-watch = "booking_slot_watch.__main__:main"
```

외부 설정 검증 라이브러리는 필수가 아니다. 초기 규모에서는 표준 라이브러리 기반 검증도 충분하다.

## 3. CLI exit code

| 코드 | 의미 |
|---:|---|
| `0` | 성공 |
| `1` | 실행 오류 |
| `2` | 설정 오류 |
| `3` | 활성 대상 없음 |
| `4` | 모든 조회 실패 |

## 4. 오류 처리 원칙

- 한 모니터 실패가 전체 루프를 중단하지 않음
- 설정 오류는 전체 시작 실패
- API 실패는 이전 상태 보존
- ntfy 실패는 상태 변경과 분리
- 상태 파일 쓰기 실패는 치명적 오류
- 예상하지 못한 GraphQL 구조는 명확한 로그와 테스트 fixture 후보로 저장
- 응답 전체를 로그에 남길 때 비밀 정보 포함 여부 확인

## 5. 테스트 완료 기준

필수:

- 설정 테스트
- URL 파서 테스트
- GraphQL 응답 파서 테스트
- 상태 전이 테스트
- 상태 파일 테스트
- ntfy 요청 테스트
- 다중 URL 테스트
- 같은 URL·날짜의 다중 시간 그룹화 테스트
- 만료 테스트
- 조회 실패 시 이전 상태 보존 테스트
- 알림 실패 시 notified 상태 미갱신 테스트

목표:

```text
ruff check 통과
pytest 전체 통과
핵심 모듈 mypy 통과
```

## 6. 실제 대상 진단

구현 완료 후 아래 URL로 `check-once`를 실행한다.

```text
https://m.booking.naver.com/booking/12/bizes/472710/items/7804183?area=ple&lang=ko&startDateTime=2026-08-29T00%3A00%3A00%2B09%3A00&tab=book&theme=place
```

진단 시 확인:

- URL 식별자 추출
- GraphQL HTTP 상태
- `hourly` 응답 존재
- `2026-08-29` 슬롯 목록
- 실제 시간 형식
- `unitStock`
- `unitBookingCount`
- 잔여 수량
- 지정 대상 시간이 정확히 일치하는지

네이버 응답이 해당 미래 날짜를 아직 제공하지 않거나 상품 구조가 다르면, 추측하지 말고 `unknown`과 진단 로그를 반환한다.

## 7. 완료 정의

다음을 모두 만족하면 1차 구현 완료다.

1. 공개 저장소에서 테스트 Actions 통과
2. `monitors.json`에 URL 여러 개 등록 가능
3. URL마다 날짜 여러 개 등록 가능
4. 날짜마다 시간 여러 개 등록 가능
5. 같은 URL·날짜는 API 한 번 호출
6. 회차별 상태 독립 저장
7. `sold_out → available` ntfy 알림
8. 잔여 증가 재알림
9. 중복 알림 없음
10. API 오류 시 매진 오판 없음
11. 만료 후 자동 중단
12. 활성 대상 없으면 다음 Action 미실행
13. 5시간대 장시간 루프와 연결 실행 동작
14. Heartbeat 1일 1회
15. README만 보고 새 대상 추가 가능

## 8. 후속 개선 후보

1차 범위 이후에만 검토한다.

- OCI Docker 실행 파일 추가
- Playwright fallback
- ntfy 자체 서버 인증
- 상태를 외부 KV로 이동
- GitHub Actions 사용 중단 시간 감지
- 여러 알림 채널
- 웹 설정 UI

자동 예약은 후속 개선 후보에도 넣지 않는다.
