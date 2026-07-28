# Claude 구현 요청서 — naver-booking-slot-watch

## 목표

`cronoth/naver-booking-slot-watch` 공개 GitHub 저장소에 네이버 예약의 특정 날짜·시간 회차를 주기적으로 감시하고, 잔여석이 생기면 `ntfy`로 알림을 보내는 도구를 구현한다.

현재 저장소는 비어 있는 상태를 전제로 한다.

이 프로젝트는 아래 두 공개 프로젝트의 장점을 결합하되, 필요한 기능만 작고 명확하게 새로 구현한다.

- DuckOnDesk/naver-booking-monitor  
  https://github.com/DuckOnDesk/naver-booking-monitor
  - 네이버 예약 GraphQL `schedule`, `hourlySchedule` 조회 방식 참고
  - GitHub Actions에서 약 5시간 30분 동안 내부 반복 실행 후 다음 실행을 연결하는 구조 참고
- munlucky/naver-booking-ping  
  https://github.com/munlucky/naver-booking-ping
  - `ntfy` 알림 구조
  - 상태 저장과 중복 알림 방지 개념
  - Heartbeat 개념 참고

## 가장 중요한 설계 결정

1. 기본 조회는 Playwright가 아니라 네이버 예약 GraphQL API를 사용한다.
2. Playwright는 초기 버전에 넣지 않는다.
3. 하나의 `monitors.json`에서 여러 URL, 여러 날짜, 여러 시간 회차를 동시에 관리한다.
4. 같은 URL과 날짜에 속한 여러 시간은 GraphQL 요청 한 번으로 묶어서 처리한다.
5. 상태는 각 `monitor ID + 날짜 + 시간` 조합별로 독립 관리한다.
6. `매진 → 예약 가능` 전환 시 `ntfy` 알림을 보낸다.
7. 예약 가능 상태에서 잔여 수량이 증가할 때도 재알림할 수 있게 한다.
8. 동일 상태와 동일 수량은 중복 알림하지 않는다.
9. GitHub Actions 공개 저장소의 표준 `ubuntu-latest`에서 실행한다.
10. 단순 5분 cron 감시가 아니라, 한 Action 안에서 60~90초 간격으로 반복 확인한다.
11. 한 작업은 GitHub-hosted runner 최대 실행 시간보다 짧은 약 5시간 30분 동안 실행한다.
12. 실행 종료 시 활성 감시 대상이 남아 있으면 다음 워크플로를 즉시 트리거한다.
13. 체인이 끊긴 경우를 복구하기 위한 5시간 단위 cron을 둔다.
14. 모든 대상이 비활성 또는 만료 상태이면 장시간 실행과 다음 실행 연결을 중단한다.
15. 예약 설정 변경은 코드 수정 없이 `monitors.json`만 수정해서 적용한다.

## 문서 순서

구현 전 다음 문서를 순서대로 읽는다.

1. [프로젝트 명세](docs/PROJECT_SPEC.md)
2. [설정과 상태 형식](docs/CONFIG_AND_STATE.md)
3. [GitHub Actions 운영 설계](docs/GITHUB_ACTIONS.md)
4. [구현 계획과 완료 기준](docs/IMPLEMENTATION_PLAN.md)
5. [참고 프로젝트와 코드 포인트](docs/REFERENCES.md)

## 구현 시 지켜야 할 원칙

- 파이썬 버전은 `.python-version`과 `pyproject.toml`의 `requires-python`만을 단일 출처로 삼고, 문서에는 적지 않는다
- 로컬과 CI 모두 `uv run`으로 실행한다. 시스템 `python`을 직접 호출하지 않는다
- `requests` 또는 `httpx` 중 하나만 사용
- 과도한 추상화 금지
- 네이버 GraphQL 응답 파싱과 상태 전이 판단은 순수 함수로 분리
- 파일 쓰기는 원자적으로 처리
- API 실패와 `0석`을 절대 같은 상태로 취급하지 않음
- 네이버 응답 구조 변경이나 HTTP 오류 시 기존 상태를 덮어쓰지 않음
- 로그에는 `NTFY_TOPIC`이나 인증 정보 출력 금지
- 공개 저장소이므로 예약 URL, 대상 이름, 날짜·시간은 공개될 수 있음을 README에 명시
- 비밀 정보는 GitHub Actions Secret으로만 관리
- 첫 구현은 자동 예약 기능을 포함하지 않음
- 네이버 로그인 쿠키를 요구하지 않음
- 카카오 예약, 항공권 감시 등 범위 밖 기능을 추가하지 않음

## 예상 최종 구조

```text
naver-booking-slot-watch/
├─ .github/
│  └─ workflows/
│     ├─ monitor.yml
│     └─ test.yml
├─ src/
│  └─ booking_slot_watch/
│     ├─ __init__.py
│     ├─ __main__.py
│     ├─ config.py
│     ├─ models.py
│     ├─ naver.py
│     ├─ notifier.py
│     ├─ state.py
│     └─ monitor.py
├─ tests/
│  ├─ fixtures/
│  ├─ test_config.py
│  ├─ test_naver.py
│  ├─ test_state.py
│  └─ test_monitor.py
├─ state/
│  └─ availability.json
├─ monitors.example.json
├─ monitors.json
├─ pyproject.toml
├─ README.md
├─ LICENSE
└─ .gitignore
```

## 작업 결과 요청

구현 완료 후 다음을 보고한다.

- 생성·수정한 파일 목록
- 핵심 설계 요약
- 로컬 실행 명령
- GitHub Secret 등록 명령
- 최초 Actions 실행 명령
- 테스트 결과
- 실제 네이버 URL을 사용한 1회 진단 결과
- 아직 확인하지 못한 위험이나 네이버 API 의존성
