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
      "last_error": null,
      "error_alert_sent": false,
      "fingerprint": "12/472710/7804183"
    },
    "event-20260829:2026-08-29:14:00": {
      "status": "available",
      "remaining": 1,
      "last_checked_at": "2026-07-28T14:30:10+09:00",
      "last_changed_at": "2026-07-28T14:30:10+09:00",
      "last_notified_remaining": 1,
      "consecutive_errors": 0,
      "last_error": null,
      "error_alert_sent": false,
      "fingerprint": "12/472710/7804183"
    }
  },
  "heartbeat": {
    "last_sent_date": "2026-07-28"
  },
  "outage_alert_sent": false,
  "recovery_alert_pending": false
}
```

`error_alert_sent`는 조회 실패 알림을 실제로 보냈는지다. 임계값에 도달한 순간에만 보내면 그때 ntfy가 실패하면 다음 조회는 이미 임계값을 넘어서서 다시 보내지 않는다. 그래서 `연속 오류 >= 임계값 && !error_alert_sent`이면 계속 시도하고, 전송에 성공한 뒤에만 표시를 남긴다. 정상 조회가 한 번이라도 있으면 `consecutive_errors`와 함께 풀린다.

`fingerprint`는 `businessTypeId/businessId/bizItemId`다. 상태 키에는 상품 식별자가 없어서, 같은 `monitor.id`로 URL만 다른 상품으로 바꾸면 이전 상품의 알림 기록이 새 상품에 적용된다. 지문이 현재 설정과 다르면 그 회차 상태를 초기화한다. `null`은 지문을 남기기 전의 상태 파일이며 일치로 취급한다(배포 직후 중복 알림 방지).

`outage_alert_sent`는 현재 전역 `감시 실패` 알림을 성공적으로 보냈는지 표시한다. `recovery_alert_pending`은 정상 조회는 확인됐지만 전역 `감시 복구` 알림이 아직 성공하지 않았다는 표시다. 슬롯별 `error_alert_sent`와 별개라 여러 슬롯이 같은 응답을 공유해도 복구 알림을 하나만 보낼 수 있다.

네 필드 모두 없으면 기본값(`false`, `null`, `false`, `false`)으로 읽으므로 기존 상태 파일을 그대로 이어받는다.

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
- `error_alert_sent = false`

### 조회 실패

- 기존 `status`, `remaining`, `last_changed_at` 유지
- `consecutive_errors += 1`
- `last_error` 갱신
- 실패 시각은 별도 필드로 두어도 됨
- 실패 결과를 `sold_out`으로 저장하지 않음

### 전역 감시 실패와 복구

모든 활성 조회가 5회 연속 실패하면 `감시 실패` 알림을 시도한 뒤 현재 Naver 요청 Session을 한 번 교체한다. 루프 deadline·반복 횟수·조회 주기는 바꾸지 않고, 추가 요청 없이 다음 정규 회차부터 새 Session으로 계속 조회한다.

전역 알림 전송이 성공하면 `outage_alert_sent = true`를 즉시 저장한다. 다음 회차에서 하나라도 정상 조회하면 `outage_alert_sent = false`, `recovery_alert_pending = true`를 즉시 저장한 뒤 `감시 복구` 알림을 보낸다. 복구 알림 전송에 성공할 때만 `recovery_alert_pending = false`로 되돌리며, 실패하면 다음 성공 회차에서 재시도한다. 복구 알림이 실패한 뒤 새 장애가 나면 `outage_alert_sent`는 이미 해제돼 있으므로 새 `감시 실패` 알림을 보낼 수 있다.

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

### 전송 실패 처리

`last_notified_remaining`은 "전송에 성공한 수량"이다. 전송이 확인되지 않으면 갱신하지 않는다. 여기서 알림 이유에 따라 다르게 다뤄야 한다.

| 실패한 알림 | `last_notified_remaining` | 이유 |
| --- | --- | --- |
| `initial_available` | 그대로(`null`) | 이미 비어 있어 다음 조회가 재시도한다 |
| `became_available` | **`null`로 지운다** | 이전 available 구간의 수량이 남아 있으면 재시도가 막힌다 |
| `remaining_increased` | 그대로 유지 | 지우면 수량이 줄었을 때 재개방으로 오판한다 |

`became_available`에서 지우는 이유: 5석 알림 성공 → 매진 → 1석 재개방 전송 실패 순서에서 `5`가 남으면 다음 조회의 `1 > 5`가 거짓이라 재개방 알림이 영구히 묻힌다.

`remaining_increased`에서 유지하는 이유: 5석 알림 성공 → 6석 증가 알림 실패 후 `null`로 지우면, 다음 조회에서 4석으로 줄어도 "전송 미확인" 분기를 타고 `became_available`로 판단해 이미 알린 것보다 적은 수량을 또 알린다. `5`를 남겨두면 수량이 계속 높을 때만(`6 > 5`) 재시도하고 줄어들면 알리지 않는다.

## 8. 상태 파일 커밋 정책

매 루프마다 커밋하지 않는다.

권장:

- 메모리에서 상태 갱신
- **조회 회차 완료 직후 로컬 파일 체크포인트**
- 알림 전송 성공 직후 로컬 파일 저장
- 루프 정상 종료 시 최종 저장
- 커밋은 job이 끝날 때 한 번, 동일 내용이면 커밋하지 않음

저장 시점이 세 개인 이유가 각각 다르다.

**회차 체크포인트** — 루프 종료 저장에만 의지할 수 없다. GitHub이 실행을 취소할 때 신호 기반 정상 종료를 보장하지 않는다(실제로 취소된 실행의 로그에 `루프 종료:` 줄이 없고 `Commit state`가 "상태 변화 없음"으로 끝났다). 그러면 알림을 동반하지 않는 전이가 통째로 사라진다. 가장 위험한 경로는 이것이다.

```text
원격 상태: available, 1석 알림 완료
→ 실행 중 sold_out으로 바뀜 (알림 없음 → 저장 안 됨)
→ 코드 push로 실행 취소, 전이 유실
→ 다시 1석 available
→ 새 실행은 원격의 available/lnr=1을 읽는다
→ 1 > 1이 거짓이라 재개방 알림을 보내지 않는다
```

`consecutive_errors` 누적, 오류 복구, 상품 지문도 같이 사라진다.

**알림 직후 저장** — 회차 체크포인트로 대체되지 않는다. 상품·날짜 그룹이 여러 개일 때 뒤 그룹 조회가 재시도·timeout으로 길어지고 그 사이 취소되면 체크포인트까지 오지 못한다. 앞 그룹의 알림 기록이 사라져 다음 실행이 같은 알림을 다시 보낸다.

**루프 정상 종료 저장** — 마지막 회차 뒤 종료 시각까지 대기하는 동안의 변화는 없지만, 활성 대상 소멸로 끝나는 경로를 포함해 종료 상태를 확정한다.

체크포인트의 비용은 회차마다 기존 상태 파일을 읽어 비교하는 것뿐이다. `save_state`가 변화 판단을 위해 매번 파일을 읽으므로 읽기는 회차당 한 번 늘어난다. 실질 상태가 같으면 임시 파일 작성과 `os.replace`는 하지 않으므로 **쓰기와 커밋은 늘지 않는다**(아래 참고).

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
