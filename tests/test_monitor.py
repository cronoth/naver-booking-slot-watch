"""단계 7 — check_once 오케스트레이션. 네이버와 ntfy를 모두 responses로 태운다."""

import json
from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type
from pathlib import Path
from typing import Any

import pytest
import requests
import responses

from booking_slot_watch.config import Config, load_config
from booking_slot_watch.monitor import check_once, should_send_heartbeat
from booking_slot_watch.naver import GRAPHQL_URL, KST, NaverBookingClient
from booking_slot_watch.notifier import DEFAULT_SERVER_URL, Notifier, NtfyConfig
from booking_slot_watch.state import State, slot_key

TOPIC = "topic-for-tests"
NTFY_URL = f"{DEFAULT_SERVER_URL}/{TOPIC}"

#: 기본 기준 시각은 Heartbeat 시각(07시) 전으로 둬서 알림 집계가 섞이지 않게 한다.
NOW = datetime(2026, 7, 28, 6, 30, 0, tzinfo=KST)
HEARTBEAT_NOW = datetime(2026, 7, 28, 14, 30, 12, tzinfo=KST)

URL_A = "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183"
URL_B = "https://m.booking.naver.com/booking/6/bizes/333/items/444"


def payload(*, remaining_at_1430: int = 0, sale: bool = True) -> dict[str, Any]:
    """tests/fixtures/hourly_schedule.json과 같은 형태의 응답을 만든다.

    12:30과 17:00은 항상 매진, 14:30만 잔여를 조절한다.
    """
    slots = {"12:30": (16, 16), "14:30": (16, 16 - remaining_at_1430), "17:00": (16, 16)}
    return {
        "data": {
            "schedule": {
                "bizItemSchedule": {
                    "hourly": [
                        {
                            "unitStartTime": f"2026-08-29 {hhmm}:00",
                            "unitBookingCount": booking,
                            "unitStock": stock,
                            "isUnitSaleDay": sale,
                            "__typename": "HourlySchedule",
                        }
                        for hhmm, (stock, booking) in slots.items()
                    ],
                    "__typename": "BizItemSchedule",
                },
                "__typename": "Schedule",
            }
        }
    }


def write_config(tmp_path: Path, monitors: list[dict[str, Any]]) -> Config:
    path = tmp_path / "monitors.json"
    path.write_text(
        json.dumps({"version": 1, "monitors": monitors}, ensure_ascii=False), encoding="utf-8"
    )
    return load_config(path)


def monitor_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "event",
        "name": "8월 29일 예약",
        "enabled": True,
        "url": URL_A,
        "targets": [{"date": "2026-08-29", "times": ["14:30"]}],
        "expires_at": "2026-08-29T17:00:00+09:00",
    }
    entry.update(overrides)
    return entry


def run(config: Config, state: State, now: datetime = NOW) -> Any:
    client = NaverBookingClient(sleep=lambda _: None)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        return check_once(config, state, client=client, notifier=notifier, now=now)
    finally:
        client.close()
        notifier.close()


def ntfy_calls() -> list[Any]:
    return [call for call in responses.calls if call.request.url.startswith(DEFAULT_SERVER_URL)]


# --- 정상 판정 ------------------------------------------------------------


@responses.activate
def test_sold_out_slot_is_recorded_without_notification(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=0))
    state = State()

    outcome = run(write_config(tmp_path, [monitor_entry()]), state)

    slot = state.slots["event:2026-08-29:14:30"]
    assert slot.status == "sold_out"
    assert slot.last_notified_remaining is None
    assert ntfy_calls() == []
    assert (outcome.slots_checked, outcome.slots_failed, outcome.notifications_sent) == (1, 0, 0)


@responses.activate
def test_available_slot_notifies_and_records_the_sent_amount(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=2))
    responses.add(responses.POST, NTFY_URL, json={})
    state = State()

    outcome = run(write_config(tmp_path, [monitor_entry()]), state)

    slot = state.slots["event:2026-08-29:14:30"]
    assert (slot.status, slot.remaining) == ("available", 2)
    assert slot.last_notified_remaining == 2
    assert outcome.notifications_sent == 1
    assert len(ntfy_calls()) == 1


@responses.activate
def test_failed_notification_does_not_mark_the_slot_as_notified(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=1))
    responses.add(responses.POST, NTFY_URL, json={}, status=500)
    state = State()

    outcome = run(write_config(tmp_path, [monitor_entry()]), state)

    slot = state.slots["event:2026-08-29:14:30"]
    assert slot.status == "available"
    assert slot.last_notified_remaining is None, "전송 실패를 성공처럼 기록하면 안 된다"
    assert outcome.notifications_sent == 0
    assert outcome.slots_failed == 0, "ntfy 실패는 조회 실패가 아니다"


@responses.activate
def test_notification_is_retried_on_the_next_run(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=1))
    responses.add(responses.POST, NTFY_URL, json={}, status=500)
    config = write_config(tmp_path, [monitor_entry()])
    state = State()
    run(config, state)

    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=1))
    responses.add(responses.POST, NTFY_URL, json={})
    outcome = run(config, state)

    assert outcome.notifications_sent == 1
    assert state.slots["event:2026-08-29:14:30"].last_notified_remaining == 1


# --- 조회 실패 ------------------------------------------------------------


@responses.activate
def test_api_failure_preserves_the_previous_state(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=0))
    config = write_config(tmp_path, [monitor_entry()])
    state = State()
    run(config, state)
    responses.reset()

    responses.add(responses.POST, GRAPHQL_URL, json={}, status=429)
    outcome = run(config, state)

    slot = state.slots["event:2026-08-29:14:30"]
    assert slot.status == "sold_out", "조회 실패를 매진/unknown으로 덮어쓰면 안 된다"
    assert slot.remaining == 0
    assert slot.consecutive_errors == 1
    assert slot.last_error == "rate_limited"
    assert (outcome.slots_checked, outcome.slots_failed) == (1, 1)
    assert ntfy_calls() == []


@responses.activate
def test_missing_target_time_is_unknown_not_sold_out(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    entry = monitor_entry(targets=[{"date": "2026-08-29", "times": ["13:00"]}])

    outcome = run(write_config(tmp_path, [entry]), state := State())

    slot = state.slots["event:2026-08-29:13:00"]
    assert slot.status == "unknown"
    assert slot.last_error == "slot_not_found"
    assert outcome.slots_failed == 1


@responses.activate
def test_empty_schedule_is_distinguished_from_a_missing_time(tmp_path: Path) -> None:
    empty = payload()
    empty["data"]["schedule"]["bizItemSchedule"]["hourly"] = []
    responses.add(responses.POST, GRAPHQL_URL, json=empty)

    run(write_config(tmp_path, [monitor_entry()]), state := State())

    assert state.slots["event:2026-08-29:14:30"].last_error == "empty_schedule"


@responses.activate
def test_error_alert_fires_at_the_configured_threshold(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, NTFY_URL, json={})
    config = write_config(tmp_path, [monitor_entry()])
    state = State()

    sent = [run(config, state).notifications_sent for _ in range(4)]

    assert sent == [0, 0, 1, 0], "임계값에 도달한 회차에만 운영 알림"


@responses.activate
def test_one_failing_monitor_does_not_stop_the_others(tmp_path: Path) -> None:
    def graphql(request: Any) -> tuple[int, dict[str, str], str]:
        item = json.loads(request.body or "")["variables"]["scheduleParams"]["bizItemId"]
        if item == "444":  # URL_B 상품만 계속 실패시킨다
            return 500, {}, json.dumps({})
        return 200, {}, json.dumps(payload(remaining_at_1430=1))

    responses.add_callback(
        responses.POST, GRAPHQL_URL, callback=graphql, content_type="application/json"
    )
    responses.add(responses.POST, NTFY_URL, json={})
    entries = [
        monitor_entry(id="broken", url=URL_B, targets=[{"date": "2026-08-29", "times": ["14:30"]}]),
        monitor_entry(id="ok"),
    ]

    outcome = run(write_config(tmp_path, entries), state := State())

    assert outcome.slots_failed == 1
    assert outcome.notifications_sent == 1
    assert state.slots["ok:2026-08-29:14:30"].status == "available"
    assert state.slots["broken:2026-08-29:14:30"].status == "unknown"


# --- 그룹화와 집계 --------------------------------------------------------


@responses.activate
def test_same_product_and_date_uses_a_single_graphql_call(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=0))
    entry = monitor_entry(targets=[{"date": "2026-08-29", "times": ["14:30", "12:30", "17:00"]}])

    outcome = run(write_config(tmp_path, [entry]), State())

    graphql_calls = [c for c in responses.calls if c.request.url == GRAPHQL_URL]
    assert len(graphql_calls) == 1
    assert outcome.slots_checked == 3


@responses.activate
def test_stops_starting_new_request_groups_when_told_to(tmp_path: Path) -> None:
    """종료 시각이 임박하면 남은 그룹을 시작하지 않는다.

    한 회차가 모든 그룹을 끝까지 돌면 대상이 많을 때 연결 실행 예산을 다 먹는다.
    """
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    entry = monitor_entry(
        targets=[
            {"date": "2026-08-29", "times": ["12:30", "14:30"]},
            {"date": "2026-08-30", "times": ["12:30"]},
        ]
    )
    config = write_config(tmp_path, [entry])
    state = State()
    calls = {"n": 0}

    def should_continue() -> bool:
        calls["n"] += 1
        return calls["n"] <= 1  # 첫 그룹만 허용

    client = NaverBookingClient(sleep=lambda _: None)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        outcome = check_once(
            config, state, client=client, notifier=notifier, now=NOW,
            should_continue=should_continue,
        )
    finally:
        client.close()
        notifier.close()

    graphql = [c for c in responses.calls if c.request.url == GRAPHQL_URL]
    assert len(graphql) == 1, "두 번째 그룹은 요청조차 하지 않아야 한다"
    assert outcome.slots_checked == 2
    assert outcome.slots_skipped == 1
    assert "event:2026-08-30:12:30" not in state.slots, "건너뛴 회차는 상태를 만들지 않는다"


@responses.activate
def test_skipped_slots_are_not_counted_as_failures(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    entry = monitor_entry(
        targets=[
            {"date": "2026-08-29", "times": ["14:30"]},
            {"date": "2026-08-30", "times": ["14:30"]},
        ]
    )
    client = NaverBookingClient(sleep=lambda _: None)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        outcome = check_once(
            write_config(tmp_path, [entry]), State(), client=client, notifier=notifier,
            now=NOW, should_continue=lambda: False,
        )
    finally:
        client.close()
        notifier.close()

    assert (outcome.slots_checked, outcome.slots_failed, outcome.slots_skipped) == (0, 0, 2)
    assert len(responses.calls) == 0


@responses.activate
def test_no_limit_when_should_continue_is_absent(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    entry = monitor_entry(
        targets=[
            {"date": "2026-08-29", "times": ["14:30"]},
            {"date": "2026-08-30", "times": ["14:30"]},
        ]
    )
    outcome = run(write_config(tmp_path, [entry]), State())
    assert outcome.slots_checked == 2
    assert outcome.slots_skipped == 0


@responses.activate
def test_outcome_reports_active_counts(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=0))
    entries = [monitor_entry(), monitor_entry(id="off", enabled=False)]

    outcome = run(write_config(tmp_path, entries), State())

    assert (outcome.monitors_active, outcome.slots_active) == (1, 1)


@responses.activate
def test_expired_monitor_is_skipped_entirely(tmp_path: Path) -> None:
    entry = monitor_entry(expires_at="2026-07-01T00:00:00+09:00")

    outcome = run(write_config(tmp_path, [entry]), State())

    assert (outcome.monitors_active, outcome.slots_checked) == (0, 0)
    assert len(responses.calls) == 0


# --- Heartbeat ------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "last_sent", "expected"),
    [
        (6, None, False),
        (7, None, True),
        (14, None, True),
        (7, "2026-07-28", False),
        (7, "2026-07-27", True),
    ],
)
def test_heartbeat_is_sent_once_a_day_after_seven(
    hour: int, last_sent: str | None, expected: bool
) -> None:
    now = datetime(2026, 7, 28, hour, 0, tzinfo=KST)
    previous = date_type.fromisoformat(last_sent) if last_sent else None
    assert should_send_heartbeat(previous, now) is expected


@responses.activate
def test_no_heartbeat_before_the_configured_hour(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    run(write_config(tmp_path, [monitor_entry()]), state := State(), now=NOW)
    assert state.heartbeat_last_sent is None
    assert ntfy_calls() == []


@responses.activate
def test_heartbeat_is_sent_and_recorded(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    responses.add(responses.POST, NTFY_URL, json={})
    state = State()

    run(write_config(tmp_path, [monitor_entry()]), state, now=HEARTBEAT_NOW)

    assert state.heartbeat_last_sent == HEARTBEAT_NOW.date()
    body = (ntfy_calls()[0].request.body or b"").decode("utf-8")
    assert "활성 모니터 수: 1" in body
    assert "활성 회차 수: 1" in body


@responses.activate
def test_failed_heartbeat_is_not_recorded_as_sent(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    responses.add(responses.POST, NTFY_URL, body=requests.Timeout("t"))

    run(write_config(tmp_path, [monitor_entry()]), state := State(), now=HEARTBEAT_NOW)

    assert state.heartbeat_last_sent is None


@responses.activate
def test_heartbeat_is_not_repeated_within_the_same_day(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    responses.add(responses.POST, NTFY_URL, json={})
    config = write_config(tmp_path, [monitor_entry()])
    state = State()

    run(config, state, now=HEARTBEAT_NOW)
    responses.add(responses.POST, GRAPHQL_URL, json=payload())
    run(config, state, now=HEARTBEAT_NOW)

    assert len(ntfy_calls()) == 1


@responses.activate
def test_heartbeat_is_skipped_when_nothing_is_active(tmp_path: Path) -> None:
    entry = monitor_entry(expires_at="2026-07-01T00:00:00+09:00")

    run(write_config(tmp_path, [entry]), state := State(), now=HEARTBEAT_NOW)

    assert state.heartbeat_last_sent is None
    assert ntfy_calls() == []


# --- 상태 키 ------------------------------------------------------------


@responses.activate
def test_each_time_gets_an_independent_state_key(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(remaining_at_1430=1))
    responses.add(responses.POST, NTFY_URL, json={})
    entry = monitor_entry(targets=[{"date": "2026-08-29", "times": ["12:30", "14:30"]}])

    run(write_config(tmp_path, [entry]), state := State())

    sold_out = slot_key("event", date_type(2026, 8, 29), time_type(12, 30))
    available = slot_key("event", date_type(2026, 8, 29), time_type(14, 30))
    assert state.slots[sold_out].status == "sold_out"
    assert state.slots[available].status == "available"
