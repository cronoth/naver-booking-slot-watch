import copy
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
import requests
import responses

from booking_slot_watch.models import BookingIdentifiers
from booking_slot_watch.naver import (
    GRAPHQL_URL,
    KST,
    NaverApiError,
    NaverBookingClient,
    parse_hourly_schedule,
)

FIXTURES = Path(__file__).parent / "fixtures"
TARGET_DATE = date(2026, 8, 29)
IDENTIFIERS = BookingIdentifiers(business_type_id=12, business_id="472710", biz_item_id="7804183")


def payload() -> dict[str, Any]:
    """실제 응답 형태 fixture의 깊은 복사본."""
    raw = (FIXTURES / "hourly_schedule.json").read_text(encoding="utf-8")
    return copy.deepcopy(json.loads(raw))


def hourly(data: dict[str, Any]) -> list[dict[str, Any]]:
    schedule: list[dict[str, Any]] = data["data"]["schedule"]["bizItemSchedule"]["hourly"]
    return schedule


class SleepRecorder:
    """재시도 대기 시간을 기록하는 sleep 대역."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# --- 응답 파싱 (순수 함수) -------------------------------------------------


def test_parses_every_slot_from_real_response_shape() -> None:
    schedule = parse_hourly_schedule(payload(), TARGET_DATE)
    assert schedule.date == TARGET_DATE
    assert [slot.start_at for slot in schedule.slots] == [
        datetime(2026, 8, 29, 11, 0, tzinfo=KST),
        datetime(2026, 8, 29, 14, 0, tzinfo=KST),
        datetime(2026, 8, 29, 17, 0, tzinfo=KST),
        datetime(2026, 8, 29, 20, 0, tzinfo=KST),
    ]
    assert [slot.remaining for slot in schedule.slots] == [0, 1, 3, 2]
    assert [slot.sale_enabled for slot in schedule.slots] == [True, True, True, False]


def test_keeps_raw_stock_and_booking_count() -> None:
    schedule = parse_hourly_schedule(payload(), TARGET_DATE)
    sold_out = schedule.slots[0]
    assert (sold_out.stock, sold_out.booking_count, sold_out.remaining) == (10, 10, 0)


def test_remaining_is_clamped_at_zero_when_overbooked() -> None:
    data = payload()
    hourly(data)[0].update(unitStock=2, unitBookingCount=5)
    assert parse_hourly_schedule(data, TARGET_DATE).slots[0].remaining == 0


def test_finds_slot_by_target_time() -> None:
    schedule = parse_hourly_schedule(payload(), TARGET_DATE)
    found = schedule.find(time(14, 0))
    assert found is not None
    assert found.remaining == 1


def test_find_returns_none_when_target_time_is_absent_but_others_exist() -> None:
    schedule = parse_hourly_schedule(payload(), TARGET_DATE)
    assert schedule.find(time(13, 0)) is None
    assert schedule.slots, "지정 시간만 없는 경우와 응답이 빈 경우를 구분할 수 있어야 한다"


def test_find_does_not_match_a_slot_from_another_date() -> None:
    data = payload()
    for slot in hourly(data):
        slot["unitStartTime"] = slot["unitStartTime"].replace("2026-08-29", "2026-08-30")
    schedule = parse_hourly_schedule(data, TARGET_DATE)
    assert schedule.find(time(14, 0)) is None


@pytest.mark.parametrize("empty", [[], None])
def test_empty_hourly_parses_to_no_slots(empty: Any) -> None:
    data = payload()
    data["data"]["schedule"]["bizItemSchedule"]["hourly"] = empty
    schedule = parse_hourly_schedule(data, TARGET_DATE)
    assert schedule.slots == ()
    assert schedule.find(time(14, 0)) is None


def test_graphql_errors_raise_api_error() -> None:
    data = payload()
    data["errors"] = [{"message": "Variable $scheduleParams got invalid value"}]
    with pytest.raises(NaverApiError) as exc:
        parse_hourly_schedule(data, TARGET_DATE)
    assert exc.value.kind == "graphql_errors"


@pytest.mark.parametrize("missing_key", ["data", "schedule", "bizItemSchedule"])
def test_missing_response_path_raises_malformed_response(missing_key: str) -> None:
    data = payload()
    if missing_key == "data":
        del data["data"]
    elif missing_key == "schedule":
        del data["data"]["schedule"]
    else:
        del data["data"]["schedule"]["bizItemSchedule"]
    with pytest.raises(NaverApiError) as exc:
        parse_hourly_schedule(data, TARGET_DATE)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("field", ["unitStartTime", "unitStock", "unitBookingCount"])
def test_missing_slot_field_raises_malformed_response(field: str) -> None:
    data = payload()
    del hourly(data)[1][field]
    with pytest.raises(NaverApiError) as exc:
        parse_hourly_schedule(data, TARGET_DATE)
    assert exc.value.kind == "malformed_response"


def test_missing_is_unit_sale_day_defaults_to_enabled() -> None:
    data = payload()
    del hourly(data)[1]["isUnitSaleDay"]
    assert parse_hourly_schedule(data, TARGET_DATE).slots[1].sale_enabled is True


@pytest.mark.parametrize("bad_time", ["2026-08-29T14:00:00", "14:00", "", "언제인가"])
def test_unparseable_start_time_raises_malformed_response(bad_time: str) -> None:
    data = payload()
    hourly(data)[1]["unitStartTime"] = bad_time
    with pytest.raises(NaverApiError) as exc:
        parse_hourly_schedule(data, TARGET_DATE)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("bad_count", ["10", None, 1.5])
def test_non_integer_counts_raise_malformed_response(bad_count: Any) -> None:
    data = payload()
    hourly(data)[1]["unitStock"] = bad_count
    with pytest.raises(NaverApiError) as exc:
        parse_hourly_schedule(data, TARGET_DATE)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("bad_payload", [[], "text", None, 3])
def test_non_object_payload_raises_malformed_response(bad_payload: Any) -> None:
    with pytest.raises(NaverApiError) as exc:
        parse_hourly_schedule(bad_payload, TARGET_DATE)
    assert exc.value.kind == "malformed_response"


# --- HTTP 클라이언트 ------------------------------------------------------


@responses.activate
def test_sends_expected_graphql_request() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(), status=200)
    NaverBookingClient(sleep=SleepRecorder()).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    (call,) = responses.calls
    body = json.loads(call.request.body or "")
    assert body["operationName"] == "hourlySchedule"
    assert body["variables"]["scheduleParams"] == {
        "businessId": "472710",
        "businessTypeId": 12,
        "bizItemId": "7804183",
        "startDateTime": "2026-08-29T00:00:00+09:00",
        "endDateTime": "2026-08-29T00:00:00+09:00",
    }
    assert "hourly" in body["query"]
    assert call.request.headers["Content-Type"] == "application/json"
    assert call.request.headers["Referer"] == "https://m.booking.naver.com/"
    assert "Mozilla/5.0" in call.request.headers["User-Agent"]


@responses.activate
def test_returns_parsed_schedule_on_success() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(), status=200)
    schedule = NaverBookingClient(sleep=SleepRecorder()).fetch_hourly_schedule(
        IDENTIFIERS, TARGET_DATE
    )
    found = schedule.find(time(17, 0))
    assert found is not None
    assert found.remaining == 3


@responses.activate
def test_retries_twice_with_documented_delays_then_succeeds() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=502)
    responses.add(responses.POST, GRAPHQL_URL, json=payload(), status=200)
    sleeper = SleepRecorder()

    NaverBookingClient(sleep=sleeper).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    assert len(responses.calls) == 3
    assert sleeper.delays == [2.0, 5.0]


@responses.activate
def test_gives_up_after_three_attempts() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    sleeper = SleepRecorder()

    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=sleeper).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    assert exc.value.kind == "http_error"
    assert exc.value.status == 500
    assert len(responses.calls) == 3
    assert sleeper.delays == [2.0, 5.0]


@pytest.mark.parametrize("status", [403, 429])
@responses.activate
def test_rate_limited_status_has_its_own_kind(status: int) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=status)
    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=SleepRecorder()).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    assert exc.value.kind == "rate_limited"
    assert exc.value.status == status


@responses.activate
def test_repeated_rate_limiting_extends_backoff() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=429)
    sleeper = SleepRecorder()
    client = NaverBookingClient(sleep=sleeper)

    for _ in range(2):
        with pytest.raises(NaverApiError):
            client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    first_round, second_round = sleeper.delays[:2], sleeper.delays[2:4]
    assert second_round[0] > first_round[0]
    assert second_round[1] > first_round[1]


@responses.activate
def test_success_resets_rate_limit_backoff() -> None:
    for _ in range(3):  # 1회차: 전부 429 → 조회 실패로 스트릭 누적
        responses.add(responses.POST, GRAPHQL_URL, json={}, status=429)
    responses.add(responses.POST, GRAPHQL_URL, json=payload(), status=200)  # 2회차: 성공
    for _ in range(3):  # 3회차: 다시 429
        responses.add(responses.POST, GRAPHQL_URL, json={}, status=429)
    sleeper = SleepRecorder()
    client = NaverBookingClient(sleep=sleeper)

    with pytest.raises(NaverApiError):
        client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    with pytest.raises(NaverApiError):
        client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    # 성공으로 스트릭이 초기화됐으므로 3회차 대기가 1회차와 같아야 한다.
    assert sleeper.delays == [2.0, 5.0, 2.0, 5.0]


@responses.activate
def test_client_error_is_not_retried() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=404)
    sleeper = SleepRecorder()

    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=sleeper).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    assert exc.value.kind == "http_error"
    assert len(responses.calls) == 1
    assert sleeper.delays == []


@responses.activate
def test_graphql_errors_are_not_retried() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={"errors": [{"message": "boom"}]}, status=200)
    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=SleepRecorder()).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    assert exc.value.kind == "graphql_errors"
    assert len(responses.calls) == 1


@responses.activate
def test_timeout_is_retried_then_reported() -> None:
    responses.add(responses.POST, GRAPHQL_URL, body=requests.Timeout("timed out"))
    sleeper = SleepRecorder()

    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=sleeper).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    assert exc.value.kind == "timeout"
    assert len(responses.calls) == 3
    assert sleeper.delays == [2.0, 5.0]


@responses.activate
def test_connection_error_is_retried_then_reported() -> None:
    responses.add(responses.POST, GRAPHQL_URL, body=requests.ConnectionError("no route"))
    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=SleepRecorder()).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    assert exc.value.kind == "network_error"
    assert len(responses.calls) == 3


@responses.activate
def test_non_json_body_raises_invalid_json() -> None:
    responses.add(responses.POST, GRAPHQL_URL, body="<html>maintenance</html>", status=200)
    with pytest.raises(NaverApiError) as exc:
        NaverBookingClient(sleep=SleepRecorder()).fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    assert exc.value.kind == "invalid_json"


@responses.activate
def test_request_uses_documented_timeout() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(), status=200)
    client = NaverBookingClient(sleep=SleepRecorder())
    client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    assert client.timeout == 15.0


@responses.activate
def test_uses_the_injected_session_for_connection_reuse() -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(), status=200)
    session = requests.Session()
    client = NaverBookingClient(session=session, sleep=SleepRecorder())

    client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)
    client.fetch_hourly_schedule(IDENTIFIERS, TARGET_DATE)

    assert client.session is session
    assert len(responses.calls) == 2


def test_creates_its_own_session_when_none_is_given() -> None:
    client = NaverBookingClient(sleep=SleepRecorder())
    assert isinstance(client.session, requests.Session)
    client.close()
