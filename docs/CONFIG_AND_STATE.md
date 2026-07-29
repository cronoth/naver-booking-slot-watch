# 설정과 상태 형식

## 1. `monitors.json`

권장 전체 예시:

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
      "url": "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183?area=ple&lang=ko&startDateTime=2026-08-29T00%3A00%3A00%2B09%3A00&tab=book&theme=place",
      "targets": [
        {
          "date": "2026-08-29",
          "times": ["11:00", "14:00", "17:00"]
        }
      ],
      "notify_when_remaining_at_least": 1,
      "notify_on_initial_available": true,
      "notify_on_increase": true,
      "expires_at": "2026-08-29T17:00:00+09:00"
    },
    {
      "id": "restaurant-20260905",
      "name": "식당 예약",
      "enabled": true,
      "url": "https://m.booking.naver.com/booking/6/bizes/333/items/444",
      "targets": [
        {
          "date": "2026-09-05",
          "times": ["18:00", "19:00"]
        },
        {
          "date": "2026-09-06",
          "times": ["18:00"]
        }
      ],
      "expires_at": "2026-09-06T18:00:00+09:00"
    }
  ]
}
```

## 2. 필드 정의

### 루트

| 필드 | 필수 | 설명 |
|---|---:|---|
| `version` | 예 | 설정 형식 버전 |
| `defaults` | 아니오 | 모니터별 기본값 |
| `monitors` | 예 | 감시 대상 배열 |

### 모니터

| 필드 | 필수 | 설명 |
|---|---:|---|
| `id` | 예 | 상태 키에 사용하는 고유 ID |
| `name` | 예 | 로그·알림 표시명 |
| `enabled` | 예 | 활성화 여부 |
| `url` | 예 | 네이버 예약 URL |
| `targets` | 예 | 날짜와 시간 목록 |
| `expires_at` | 권장 | ISO 8601 KST 만료 시각 |
| `notify_when_remaining_at_least` | 아니오 | 알림 최소 잔여 수량 |
| `notify_on_initial_available` | 아니오 | 첫 조회부터 가능할 때 알림 |
| `notify_on_increase` | 아니오 | 잔여 수량 증가 시 재알림 |

### 대상 날짜

| 필드 | 필수 | 설명 |
|---|---:|---|
| `date` | 예 | `YYYY-MM-DD` |
| `times` | 예 | `HH:MM` 배열 |

## 3. 유효성 검증

실행 전에 다음을 검증한다.

- `version == 1`
- `id`는 비어 있지 않고 전체에서 중복되지 않음
- URL이 네이버 예약 경로 형식과 일치
- `target.date`는 ISO 날짜
- `target.times`는 `HH:MM`
- 같은 모니터 안에서 날짜·시간 중복 제거 또는 오류
- `notify_when_remaining_at_least >= 1`
- `expires_at`에 timezone offset 존재
- 활성 모니터에는 최소 하나의 대상 회차 존재

설정 오류가 하나라도 있으면 장시간 루프를 시작하지 말고 비정상 종료한다.

### 활성 대상 계산

`enabled`와 `expires_at` 외에 **지난 회차를 걷어낸다**. KST 기준으로 판단한다.

- 지난 날짜의 `target`은 제외
- 오늘 날짜에서 이미 시작된 `time`은 제외 (`시작 시각 <= now` → 제외, 유예 시간 없음)
- 남은 회차가 없는 모니터는 비활성

이미 시작된 회차는 예약할 수 없다. 계속 조회하면 참여할 수 없는 회차에 예약 가능 알림이 나가고 API 호출만 쌓인다. 오늘 대상이 전부 지나면 `has-active-targets`가 exit 3을 내고 연결 실행이 멈춘다.

## 4. 예시와 실제 설정

`monitors.example.json`에는 실제 URL이 없는 일반 예시를 제공한다.

`monitors.json`은 실제 감시 설정이며 공개 저장소에 커밋된다.

사용자가 GitHub 웹에서 `monitors.json`만 수정해 감시 대상을 추가·수정·비활성화할 수 있어야 한다.

## 5. 상태 파일

파일:

```text
state/availability.json
```

권장 형식:

```json
{
  "version": 1,
  "updated_at": "2026-07-28T14:30:12+09:00",
  "slots": {
    "event-20260829:2026-08-29:11:00": {
      "status": "sold_out",
      "remaining": 0,
      "last_checked_at": "2026-07-28T14:30:10+09:00",
      "last_changed_at": "2026-07-28T13:00:00+09:00",
      "last_notified_remaining": null,
      "consecutive_errors": 0,
      "last_error": null
    },
    "event-20260829:2026-08-29:14:00": {
      "status": "available",
      "remaining": 1,
      "last_checked_at": "2026-07-28T14:30:10+09:00",
      "last_changed_at": "2026-07-28T14:30:10+09:00",
      "last_notified_remaining": 1,
      "consecutive_errors": 0,
      "last_error": null
    }
  },
  "heartbeat": {
    "last_sent_date": "2026-07-28"
  }
}
```

## 6. 상태 값

```text
unknown
sold_out
available
```

### 정상 조회

- `remaining >= threshold` → `available`
- `remaining < threshold` → `sold_out`
- `last_checked_at` 갱신
- 상태나 수량 변화 시 `last_changed_at` 갱신
- `consecutive_errors = 0`
- `last_error = null`

### 조회 실패

- 기존 `status`, `remaining`, `last_changed_at` 유지
- `consecutive_errors += 1`
- `last_error` 갱신
- 실패 시각은 별도 필드로 두어도 됨
- 실패 결과를 `sold_out`으로 저장하지 않음

## 7. 알림 판단 함수

입력:

```text
previous state
current remaining
threshold
notify_on_initial_available
notify_on_increase
```

출력:

```text
should_notify
notification_reason
new state
```

알림 이유 예시:

```text
initial_available
became_available
remaining_increased
error_threshold_reached
heartbeat
```

## 8. 상태 파일 커밋 정책

매 루프마다 커밋하지 않는다.

권장:

- 메모리에서 상태 갱신
- Action 종료 직전 저장 및 커밋
- 중요한 상태 전이 또는 알림 직후 로컬 파일 저장 가능
- 동일 내용이면 커밋하지 않음

`updated_at`과 슬롯의 `last_checked_at`은 조회마다 바뀌므로 "동일 내용" 판단에서 제외한다. 포함하면 파일이 항상 달라 보여서 변화 없는 커밋이 계속 쌓인다. `save_state`는 실질 상태가 그대로면 파일을 아예 쓰지 않고 `False`를 돌려주며, 그 결과 `git diff --cached --quiet` 가드가 의도대로 동작한다.

따라서 커밋된 상태 파일의 `updated_at`은 "마지막 저장 시각"이 아니라 "상태가 마지막으로 바뀐 시각"이다. 살아 있는지는 Actions 실행 이력과 일일 Heartbeat로 확인한다.

커밋 메시지:

```text
chore: update monitor state
```

GitHub Actions bot 정보:

```bash
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
```

## 9. 오래된 상태 정리

다음 상태 키는 정리 가능하다.

- 해당 monitor ID가 설정에서 삭제됨
- 대상 날짜·시간이 설정에서 삭제됨
- 만료 후 보존 기간 경과

권장 보존 기간은 30일이다. 초기 버전에서는 자동 삭제 없이 유지해도 된다.
