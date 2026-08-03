"""네이버 예약 GraphQL(`hourlySchedule`) 클라이언트.

요청 형식과 응답 경로는 docs/REFERENCES.md가 지정한
DuckOnDesk/naver-booking-monitor 의 `check_booking.py`를 근거로 한다.
공식 공개 API가 아니므로 응답 구조가 바뀌면 `NaverApiError`로 끝난다.

조회 실패를 `0석`으로 바꾸지 않는 것이 이 모듈의 핵심 계약이다.
실패는 반드시 예외로 나가고, 상태 판단은 호출자가 한다.
"""

import logging
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .models import BookingIdentifiers

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=hourlySchedule"

HOURLY_SCHEDULE_QUERY = (
    "query hourlySchedule($scheduleParams: ScheduleParams) {"
    "  schedule(input: $scheduleParams) {"
    "    bizItemSchedule {"
    "      hourly {"
    "        unitStartTime unitBookingCount unitStock isUnitSaleDay __typename"
    "      } __typename"
    "    } __typename"
    "  }"
    "}"
)

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://m.booking.naver.com/",
}

REQUEST_TIMEOUT_SEC = 15.0
#: 같은 조회에서 최대 2회 재시도하며 그 사이 대기 시간.
RETRY_DELAYS_SEC = (2.0, 5.0)
RATE_LIMITED_STATUSES = frozenset({403, 429})
#: 403/429가 이어질 때 대기 배수의 상한.
#:
#: 이 sleep은 종료 신호로 끊을 수 없다. 상한이 없으면 5시간 30분 job에서 연속
#: 실패가 쌓여 한 번의 조회가 수십 분 잠들고, 작업이 취소될 때 유예 시간을
#: 넘겨 종료 직전 상태 저장을 잃는다.
MAX_RATE_LIMIT_BACKOFF = 6

_START_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class NaverApiError(Exception):
    """조회 실패. `kind`로 실패 종류를 구분해 로그와 백오프에 쓴다."""

    def __init__(self, message: str, *, kind: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class SlotAvailability:
    start_at: datetime
    stock: int
    booking_count: int
    remaining: int
    sale_enabled: bool


@dataclass(frozen=True)
class HourlySchedule:
    """한 상품·한 날짜의 정상 응답.

    `slots`가 비어 있으면 그 날짜에 회차가 아예 없다는 뜻이고,
    `find()`가 None이면 다른 회차는 있으나 지정 시간만 없다는 뜻이다.
    둘 다 매진이 아니므로 호출자가 구분해서 처리한다.
    """

    date: date
    slots: tuple[SlotAvailability, ...]

    def find(self, target: time) -> SlotAvailability | None:
        expected = datetime.combine(self.date, target, tzinfo=KST)
        for slot in self.slots:
            if slot.start_at == expected:
                return slot
        return None


def _malformed(message: str) -> NaverApiError:
    """응답 구조가 예상과 다를 때 쓰는 오류. 매진으로 오판하지 않도록 항상 예외다."""
    return NaverApiError(message, kind="malformed_response")


def parse_hourly_schedule(payload: Any, target_date: date) -> HourlySchedule:
    """GraphQL 응답을 회차 목록으로 변환한다. 순수 함수."""
    if not isinstance(payload, dict):
        raise _malformed(f"응답이 객체가 아니다: {type(payload).__name__}")
    if payload.get("errors"):
        raise NaverApiError(f"GraphQL errors: {payload['errors']}", kind="graphql_errors")

    node: Any = payload
    for key in ("data", "schedule", "bizItemSchedule"):
        if not isinstance(node, dict) or key not in node or node[key] is None:
            raise _malformed(f"응답에 {key}가 없다")
        node = node[key]

    raw_slots = node.get("hourly")
    if raw_slots is None:
        raw_slots = []
    if not isinstance(raw_slots, list):
        raise _malformed(f"hourly가 배열이 아니다: {type(raw_slots).__name__}")

    return HourlySchedule(
        date=target_date,
        slots=tuple(_parse_slot(raw) for raw in raw_slots),
    )


def _parse_slot(raw: Any) -> SlotAvailability:
    if not isinstance(raw, dict):
        raise _malformed(f"hourly 항목이 객체가 아니다: {type(raw).__name__}")
    stock = _required_int(raw, "unitStock")
    booking_count = _required_int(raw, "unitBookingCount")
    sale_enabled = _required_bool(raw, "isUnitSaleDay")
    return SlotAvailability(
        start_at=_parse_start_time(raw.get("unitStartTime")),
        stock=stock,
        booking_count=booking_count,
        remaining=max(stock - booking_count, 0),
        sale_enabled=sale_enabled,
    )


def _required_bool(raw: dict[str, Any], key: str) -> bool:
    """누락도 오류로 다룬다.

    쿼리에 명시해 요청한 필드가 없으면 응답 계약이 바뀐 것이다. 기본값을 주면
    판매하지 않는 회차의 재고를 예약 가능으로 오판한다.
    """
    value = raw.get(key)
    if not isinstance(value, bool):
        raise _malformed(f"{key}가 불리언이 아니다: {value!r}")
    return value


def _required_int(raw: dict[str, Any], key: str) -> int:
    value: object = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(f"{key}가 정수가 아니다: {value!r}")
    return value


def _parse_start_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _malformed(f"unitStartTime이 문자열이 아니다: {value!r}")
    try:
        return datetime.strptime(value, _START_TIME_FORMAT).replace(tzinfo=KST)
    except ValueError as exc:
        raise _malformed(f"unitStartTime 형식이 다르다: {value!r}") from exc


class NaverBookingClient:
    """`hourlySchedule` 조회 전용 클라이언트. 연결은 하나의 Session으로 재사용한다."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] | None = None,
        timeout: float = REQUEST_TIMEOUT_SEC,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SEC,
    ) -> None:
        self._session_factory = session_factory
        self.session = session if session is not None else session_factory()
        self.timeout = timeout
        # 기본값을 정의 시점에 묶지 않는다. 테스트가 time.sleep을 대체할 수 있어야 한다.
        self._sleep = sleep if sleep is not None else time_module.sleep
        self._retry_delays = retry_delays
        #: 403/429가 연속된 조회 횟수. 대기 시간을 늘리는 데 쓴다.
        self._rate_limit_streak = 0
        self._session_was_reset = False

    def close(self) -> None:
        self.session.close()

    def reset_session(self) -> None:
        """연결 풀만 비우고, 다음 정규 조회부터 새 Session을 쓴다."""
        self.session.close()
        self.session = self._session_factory()
        self._session_was_reset = True
        logger.info("Naver requests.Session 초기화")

    def fetch_hourly_schedule(
        self, identifiers: BookingIdentifiers, target_date: date
    ) -> HourlySchedule:
        if self._session_was_reset:
            logger.info("초기화된 Session으로 조회 재개")
            self._session_was_reset = False
        body = _request_body(identifiers, target_date)
        backoff = min(1 + self._rate_limit_streak, MAX_RATE_LIMIT_BACKOFF)
        last_error: NaverApiError | None = None

        for attempt in range(len(self._retry_delays) + 1):
            try:
                schedule = self._attempt(body, target_date)
            except NaverApiError as error:
                last_error = error
                if not _is_retryable(error) or attempt == len(self._retry_delays):
                    break
                self._sleep(self._retry_delays[attempt] * backoff)
            else:
                self._rate_limit_streak = 0
                return schedule

        assert last_error is not None
        if last_error.kind == "rate_limited":
            self._rate_limit_streak += 1
        raise last_error

    def _attempt(self, body: dict[str, Any], target_date: date) -> HourlySchedule:
        try:
            response = self.session.post(
                GRAPHQL_URL, json=body, headers=DEFAULT_HEADERS, timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise NaverApiError(f"요청 시간 초과: {exc}", kind="timeout") from exc
        except requests.RequestException as exc:
            raise NaverApiError(f"네트워크 오류: {exc}", kind="network_error") from exc

        if response.status_code in RATE_LIMITED_STATUSES:
            raise NaverApiError(
                f"요청이 차단됐다: HTTP {response.status_code}",
                kind="rate_limited",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise NaverApiError(
                f"HTTP {response.status_code}", kind="http_error", status=response.status_code
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise NaverApiError(f"JSON이 아닌 응답: {exc}", kind="invalid_json") from exc
        return parse_hourly_schedule(payload, target_date)


def _request_body(identifiers: BookingIdentifiers, target_date: date) -> dict[str, Any]:
    day_start = f"{target_date.isoformat()}T00:00:00+09:00"
    return {
        "operationName": "hourlySchedule",
        "variables": {
            "scheduleParams": {
                "businessId": identifiers.business_id,
                "businessTypeId": identifiers.business_type_id,
                "bizItemId": identifiers.biz_item_id,
                "startDateTime": day_start,
                "endDateTime": day_start,
            }
        },
        "query": HOURLY_SCHEDULE_QUERY,
    }


def _is_retryable(error: NaverApiError) -> bool:
    if error.kind in ("timeout", "network_error", "rate_limited"):
        return True
    return error.kind == "http_error" and error.status is not None and error.status >= 500
