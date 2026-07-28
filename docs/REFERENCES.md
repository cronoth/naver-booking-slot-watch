# 참고 프로젝트와 코드 포인트

## 1. DuckOnDesk/naver-booking-monitor

저장소:

https://github.com/DuckOnDesk/naver-booking-monitor

핵심 참고 파일:

- `check_booking.py`  
  https://github.com/DuckOnDesk/naver-booking-monitor/blob/main/check_booking.py
- `.github/workflows/monitor.yml`  
  https://github.com/DuckOnDesk/naver-booking-monitor/blob/main/.github/workflows/monitor.yml
- `monitors.json`  
  https://github.com/DuckOnDesk/naver-booking-monitor/blob/main/monitors.json

참고할 부분:

- 네이버 예약 URL 파싱
- `schedule` GraphQL 요청
- `hourlySchedule` GraphQL 요청
- `unitStock - unitBookingCount` 잔여 수량 계산
- 특정 날짜·시간 설정 형식
- GitHub Actions 내부 장시간 반복 루프
- job 종료 후 다음 workflow 수동 트리거
- 체인 복구용 schedule

그대로 가져오지 않을 부분:

- 자동 예약
- 네이버 계정 쿠키
- 카카오 예약
- 여러 계정 자동 예약
- 과도하게 큰 단일 Python 파일
- 설정과 상태를 자주 커밋하는 복잡한 로직

## 2. munlucky/naver-booking-ping

저장소:

https://github.com/munlucky/naver-booking-ping

핵심 참고 파일:

- `README.md`  
  https://github.com/munlucky/naver-booking-ping/blob/main/README.md
- `src/main.ts`  
  https://github.com/munlucky/naver-booking-ping/blob/main/src/main.ts
- `src/core/checker.ts`  
  https://github.com/munlucky/naver-booking-ping/blob/main/src/core/checker.ts
- `config/config.example.yaml`  
  https://github.com/munlucky/naver-booking-ping/blob/main/config/config.example.yaml

참고할 부분:

- ntfy 알림
- 상태 유지와 중복 알림 방지
- OPEN → CLOSED → OPEN 재알림
- 일일 Heartbeat
- 설정 기반 다중 타겟 구조
- 로깅과 종료 처리 개념

그대로 가져오지 않을 부분:

- 네이버 예약 버튼 존재 여부만으로 OPEN 판단
- 특정 날짜·특정 시간·잔여 수량을 확인하지 않는 Rule A/B/C
- 5초 폴링
- Playwright 기본 의존성
- 항공권 가격 감시
- PM2와 Windows 장기 실행 구조

## 3. 설계 결론

```text
DuckOnDesk
├─ GraphQL
├─ 시간별 재고
└─ GitHub Actions 연결 실행

munlucky
├─ ntfy
├─ 상태 관리
└─ Heartbeat

새 프로젝트
├─ GraphQL 기반 다중 회차 감시
├─ ntfy 상태 전이 알림
├─ 간단한 JSON 상태
└─ GitHub Actions 5시간대 연결 실행
```

두 저장소의 코드를 무비판적으로 합치지 말고, 필요한 아이디어와 API 사용법만 참고해 모듈형으로 새로 구현한다.

## 4. 외부 서비스

- 네이버 예약 GraphQL: 공식 공개 API가 아니라 웹 프런트엔드가 사용하는 내부 인터페이스에 의존한다.
- ntfy 문서: https://docs.ntfy.sh/
- GitHub Actions 문서: https://docs.github.com/actions

README에 네이버 내부 API 변경 시 동작이 깨질 수 있다는 점을 명시한다.
