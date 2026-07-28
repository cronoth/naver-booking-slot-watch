"""단계 5 — 상태 전이와 알림 판단. docs/IMPLEMENTATION_PLAN.md 단계 5 매트릭스."""

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from booking_slot_watch.state import (
    EvaluationResult,
    SlotState,
    evaluate_error,
    evaluate_slot,
    mark_notified,
    record_error,
    record_success,
)

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 7, 28, 13, 0, tzinfo=KST)
T1 = datetime(2026, 7, 28, 14, 0, tzinfo=KST)


def observed(remaining: int, *, notified: int | None, threshold: int = 1) -> SlotState:
    """이미 한 번 정상 조회된 상태. `notified`는 알림에 성공한 수량."""
    return replace(
        record_success(None, remaining=remaining, threshold=threshold, checked_at=T0),
        last_notified_remaining=notified,
    )


def evaluate(
    previous: SlotState | None,
    remaining: int,
    *,
    threshold: int = 1,
    initial: bool = True,
    increase: bool = True,
) -> EvaluationResult:
    return evaluate_slot(
        previous,
        remaining=remaining,
        threshold=threshold,
        notify_on_initial_available=initial,
        notify_on_increase=increase,
        checked_at=T1,
    )


# --- 문서 매트릭스 --------------------------------------------------------


def test_no_previous_and_no_stock_is_sold_out_without_notice() -> None:
    result = evaluate(None, 0)
    assert result.state.status == "sold_out"
    assert result.should_notify is False
    assert result.reason is None


def test_no_previous_with_stock_notifies_when_configured() -> None:
    result = evaluate(None, 1, initial=True)
    assert result.state.status == "available"
    assert (result.should_notify, result.reason) == (True, "initial_available")


def test_no_previous_with_stock_stays_silent_when_disabled() -> None:
    result = evaluate(None, 1, initial=False)
    assert result.state.status == "available"
    assert (result.should_notify, result.reason) == (False, None)


def test_sold_out_to_available_always_notifies() -> None:
    result = evaluate(observed(0, notified=None), 1)
    assert (result.should_notify, result.reason) == (True, "became_available")


def test_available_with_same_remaining_does_not_notify_again() -> None:
    result = evaluate(observed(1, notified=1), 1)
    assert result.state.status == "available"
    assert (result.should_notify, result.reason) == (False, None)


def test_increase_notifies_when_configured() -> None:
    result = evaluate(observed(1, notified=1), 2, increase=True)
    assert (result.should_notify, result.reason) == (True, "remaining_increased")


def test_increase_stays_silent_when_disabled() -> None:
    result = evaluate(observed(1, notified=1), 2, increase=False)
    assert (result.should_notify, result.reason) == (False, None)


def test_decrease_while_still_available_does_not_notify() -> None:
    result = evaluate(observed(2, notified=2), 1)
    assert result.state.status == "available"
    assert (result.should_notify, result.reason) == (False, None)


def test_available_to_zero_only_changes_status() -> None:
    result = evaluate(observed(1, notified=1), 0)
    assert result.state.status == "sold_out"
    assert (result.should_notify, result.reason) == (False, None)


def test_zero_then_one_notifies_again() -> None:
    sold_out = evaluate(observed(1, notified=1), 0).state
    result = evaluate(sold_out, 1)
    assert (result.should_notify, result.reason) == (True, "became_available")


def test_unknown_to_available_notifies() -> None:
    unknown = record_error(None, "timeout", T0)
    result = evaluate(unknown, 1)
    assert unknown.status == "unknown"
    assert (result.should_notify, result.reason) == (True, "became_available")


# --- 임계값 --------------------------------------------------------------


def test_remaining_below_threshold_is_sold_out() -> None:
    result = evaluate(None, 1, threshold=2)
    assert result.state.status == "sold_out"
    assert result.should_notify is False


def test_remaining_at_threshold_is_available() -> None:
    result = evaluate(None, 2, threshold=2)
    assert result.state.status == "available"
    assert (result.should_notify, result.reason) == (True, "initial_available")


# --- 알림 전송 실패 처리 --------------------------------------------------


def test_unconfirmed_notification_is_retried_next_loop() -> None:
    """became_available 알림 전송이 실패하면 last_notified_remaining이 비어 있고, 다시 알린다."""
    failed_send = evaluate(observed(0, notified=None), 1).state
    assert failed_send.last_notified_remaining is None

    retried = evaluate(failed_send, 1)
    assert (retried.should_notify, retried.reason) == (True, "became_available")


def test_deliberately_suppressed_initial_available_is_not_retried() -> None:
    """설정으로 첫 알림을 끈 경우는 '전송 실패'와 달라서 다음 루프에 알리지 않는다."""
    suppressed = evaluate(None, 1, initial=False).state
    assert suppressed.last_notified_remaining == 1

    later = evaluate(suppressed, 1, initial=False)
    assert (later.should_notify, later.reason) == (False, None)


def test_suppressed_initial_still_notifies_on_increase() -> None:
    suppressed = evaluate(None, 1, initial=False).state
    later = evaluate(suppressed, 2, initial=False, increase=True)
    assert (later.should_notify, later.reason) == (True, "remaining_increased")


def test_evaluate_never_marks_notification_as_sent() -> None:
    result = evaluate(observed(0, notified=None), 3)
    assert result.should_notify is True
    assert result.state.last_notified_remaining is None, "전송 성공 전에 갱신하면 안 된다"


def test_mark_notified_records_the_sent_amount() -> None:
    result = evaluate(observed(0, notified=None), 3)
    assert mark_notified(result.state).last_notified_remaining == 3


# --- 조회 실패 판단 ------------------------------------------------------


def test_error_keeps_previous_state_and_stays_silent() -> None:
    previous = observed(0, notified=None)
    result = evaluate_error(previous, "http_429", error_alert_threshold=3, failed_at=T1)
    assert result.state.status == "sold_out"
    assert result.state.remaining == 0
    assert (result.should_notify, result.reason) == (False, None)
    assert result.state.consecutive_errors == 1


def test_error_alerts_once_when_threshold_is_reached() -> None:
    slot: SlotState | None = None
    reasons = []
    for _ in range(5):
        result = evaluate_error(slot, "timeout", error_alert_threshold=3, failed_at=T1)
        slot = result.state
        reasons.append(result.reason)
    assert reasons == [None, None, "error_threshold_reached", None, None]


def test_error_alert_can_fire_again_after_recovery() -> None:
    slot: SlotState | None = None
    for _ in range(3):
        result = evaluate_error(slot, "timeout", error_alert_threshold=3, failed_at=T1)
        slot = result.state
    assert result.reason == "error_threshold_reached"

    slot = evaluate(slot, 0).state
    assert slot.consecutive_errors == 0

    reasons = []
    for _ in range(3):
        result = evaluate_error(slot, "timeout", error_alert_threshold=3, failed_at=T1)
        slot = result.state
        reasons.append(result.reason)
    assert reasons == [None, None, "error_threshold_reached"]


@pytest.mark.parametrize("threshold", [1, 2, 5])
def test_error_alert_threshold_is_configurable(threshold: int) -> None:
    slot: SlotState | None = None
    for attempt in range(1, threshold + 1):
        result = evaluate_error(slot, "timeout", error_alert_threshold=threshold, failed_at=T1)
        slot = result.state
        expected = "error_threshold_reached" if attempt == threshold else None
        assert result.reason == expected
