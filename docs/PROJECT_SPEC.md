# 프로젝트 명세

## 1. 프로젝트 이름

- 저장소: `cronoth/naver-booking-slot-watch`
- 패키지명: `booking_slot_watch`
- 목적: 네이버 예약의 특정 날짜·시간 회차 잔여석을 감시하고 `ntfy` 알림 전송

## 2. 해결하려는 문제

사용자는 네이버 예약 상품에서 특정 일시의 특정 회차가 `매진` 상태였다가 다음과 같이 예약 가능한 상태로 변할 때 빠르게 알림을 받고 싶다.

- `매진 → 1매`
- `0석 → 1석 이상`
- `예약 불가 → 예약 가능`
- 예약 가능 상태에서 잔여 수량 증가

예시 대상 URL:

```text
https://m.booking.naver.com/booking/12/bizes/472710/items/7804183?area=ple&lang=ko&startDateTime=2026-08-29T00%3A00%3A00%2B09%3A00&tab=book&theme=place
```

URL의 핵심 식별자는 다음 경로에서 추출한다.

```text
/booking/{businessTypeId}/bizes/{businessId}/items/{bizItemId}
```

예시:

```text
businessTypeId = 12
businessId     = 472710
bizItemId      = 7804183
```

## 3. 범위

### 포함

- 다중 네이버 예약 URL
- URL별 다중 날짜
- 날짜별 다중 시간 회차
- 회차별 잔여 수량 확인
- 매진에서 예약 가능으로 바뀔 때 알림
- 잔여 수량 증가 시 선택적 재알림
- 중복 알림 방지
- JSON 상태 저장
- 자동 만료
- GitHub Actions 장시간 반복 실행
- `ntfy` 알림
- 일일 Heartbeat
- 수동 1회 실행과 설정 검증
- 테스트

### 제외

- 자동 예약
- 네이버 로그인
- 쿠키 저장
- CAPTCHA 우회
- Playwright 기본 사용
- 카카오 예약
- 항공권 가격 추적
- 웹 관리 UI
- 데이터베이스
- Vercel 배포
- OCI 배포

## 4. 조회 방식

### 기본 원칙

Playwright로 페이지를 렌더링하지 않고 네이버 예약 GraphQL API를 직접 호출한다.

시간별 회차 조회 엔드포인트:

```text
POST https://m.booking.naver.com/graphql?opName=hourlySchedule
```

요청에서 사용할 주요 변수:

```json
{
  "businessId": "472710",
  "businessTypeId": 12,
  "bizItemId": "7804183",
  "startDateTime": "2026-08-29T00:00:00+09:00",
  "endDateTime": "2026-08-29T00:00:00+09:00"
}
```

조회할 주요 응답 필드:

```graphql
hourly {
  unitStartTime
  unitBookingCount
  unitStock
  isUnitSaleDay
}
```

잔여 수량 계산:

```text
remaining = max(unitStock - unitBookingCount, 0)
```

### 중요 상태 구분

아래 세 상태를 반드시 구분한다.

- `available`: 정상 응답이며 잔여 수량이 기준 이상
- `sold_out`: 정상 응답이며 잔여 수량이 기준 미만
- `unknown`: API 오류, 파싱 오류, 대상 회차 없음, 응답 구조 변경 등

`unknown`은 `sold_out`으로 바꾸면 안 된다. 조회 실패 시 이전 정상 상태를 유지한다.

## 5. 다중 대상 처리

`monitors.json` 배열의 모든 활성 대상을 하나의 프로세스에서 처리한다.

효율화를 위해 다음 단위로 API 호출을 묶는다.

```text
businessTypeId + businessId + bizItemId + targetDate
```

같은 상품과 같은 날짜의 여러 시간은 GraphQL 한 번 호출 후 응답에서 각각 판정한다.

예시:

```text
URL A / 2026-08-29 / 11:00, 14:00, 17:00
→ hourlySchedule 요청 1번
→ 세 회차 상태 각각 판정
```

대상은 기본적으로 순차 처리한다. 초기 버전에서 무제한 병렬 요청은 사용하지 않는다.

## 6. 상태 키

회차 상태는 다음 키로 독립 관리한다.

```text
{monitor_id}:{target_date}:{target_time}
```

예시:

```text
concert-a:2026-08-29:11:00
concert-a:2026-08-29:14:00
restaurant-b:2026-09-05:18:00
```

## 7. 알림 상태 전이

### 기본 알림

```text
sold_out → available
```

### 초기 상태

첫 정상 조회가 `available`인 경우 기본값은 알림을 보낸다.

```json
"notify_on_initial_available": true
```

### 잔여 수량 증가

예약 가능 상태가 유지되어도 수량이 증가하면 재알림할 수 있다.

```text
1 → 1: 알림 없음
1 → 2: 알림
2 → 1: 알림 없음
1 → 0: 상태만 sold_out으로 변경
0 → 1: 다시 알림
```

```json
"notify_on_increase": true
```

### 조회 실패

```text
available → unknown: 알림 없음, 이전 상태 유지
sold_out → unknown: 알림 없음, 이전 상태 유지
```

연속 오류 횟수는 별도 저장하고 일정 횟수 이상이면 운영 오류 알림을 보낼 수 있다.

```json
"error_alert_threshold": 3
```

## 8. ntfy 알림

기본 서버:

```text
https://ntfy.sh
```

필수 GitHub Secret:

```text
NTFY_TOPIC
```

선택 Secret:

```text
NTFY_SERVER_URL
NTFY_TOKEN
NTFY_HEARTBEAT_TOPIC
```

알림 예시:

```text
제목: [공연 A] 예약 가능 — 2026-08-29 14:00

잔여 수량: 1
이전 상태: 매진
확인 시각: 2026-07-28 14:30:12 KST
```

알림 클릭 URL은 해당 네이버 예약 URL로 설정한다.

권장 헤더:

```text
Priority: high
Tags: bell,calendar
Click: 예약 URL
```

## 9. Heartbeat

매일 한국 시간 오전 7시 이후 첫 번째 루프에서 한 번만 전송한다.

```text
Naver Booking Slot Watch 정상 작동 중
활성 모니터 수: N
활성 회차 수: N
최근 정상 조회: YYYY-MM-DD HH:MM:SS KST
```

Heartbeat 토픽이 별도로 설정되지 않으면 일반 토픽을 사용한다.

## 10. 만료 처리

각 모니터에는 만료 시각을 둔다.

```json
"expires_at": "2026-08-29T17:00:00+09:00"
```

현재 시간이 만료 시각 이후면 해당 모니터는 조회하지 않는다.

모든 대상이 다음 상태 중 하나면 프로세스를 즉시 종료한다.

- `enabled: false`
- 만료됨
- 유효한 날짜·시간이 없음

이때 다음 GitHub Actions 연결 실행도 만들지 않는다.

## 11. 조회 간격

권장 기본값:

```text
기본 간격: 70초
지터: 0~20초
실제 간격: 70~90초
```

환경변수:

```text
CHECK_INTERVAL_SEC=70
CHECK_JITTER_SEC=20
LOOP_HOURS=5.4
```

5초나 10초와 같은 짧은 주기는 사용하지 않는다.

## 12. HTTP 안정성

- 요청 timeout: 15초
- 재시도: 동일 루프에서 최대 2회
- 재시도 대기: 2초, 5초
- HTTP 403/429는 별도 로그
- 403/429 연속 발생 시 추가 백오프
- `User-Agent`, `Referer`, `Content-Type` 설정
- 연결 재사용을 위해 `requests.Session` 또는 `httpx.Client` 사용

## 13. 로깅

```text
2026-07-28 14:00:00 INFO monitor=concert-a date=2026-08-29 time=11:00 status=sold_out remaining=0
2026-07-28 14:00:01 INFO monitor=concert-a date=2026-08-29 time=14:00 status=available remaining=1 notified=true
2026-07-28 14:00:02 ERROR monitor=restaurant-b error=http_429 previous_state_preserved=true
```

로그에 포함하면 안 되는 값:

- ntfy 토픽
- ntfy 토큰
- 향후 인증 쿠키
- GitHub Token

## 14. 보안과 공개 저장소 주의

공개 저장소에서는 다음이 공개된다.

- 예약 URL
- 모니터 이름
- 날짜와 시간
- 상태 파일에 기록된 잔여 수량
- GitHub Actions 로그

다음은 Secret에만 저장한다.

- `NTFY_TOPIC`
- `NTFY_TOKEN`
- 별도 인증 정보

토픽 이름은 추측하기 어려운 무작위 문자열을 권장한다.

## 15. 구현 언어와 품질

- 파이썬 버전은 `.python-version`과 `pyproject.toml`의 `requires-python`만을 단일 출처로 삼는다. 문서에 버전을 고정 기재하지 않는다
- 실행·테스트·린트는 모두 `uv run`을 사용한다
- 타입 힌트 필수
- `ruff` 사용
- `pytest` 사용
- 가능하면 `mypy` 사용
- 날짜와 시간은 `zoneinfo.ZoneInfo("Asia/Seoul")` 사용
- 직접 UTC+9를 더하는 방식 금지
- 파일 저장은 임시 파일 작성 후 `os.replace`로 원자적 교체
