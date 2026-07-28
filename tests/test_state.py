import json
import os
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from booking_slot_watch.state import (
    STATE_VERSION,
    SlotState,
    State,
    StateError,
    load_state,
    record_error,
    record_success,
    save_state,
    slot_key,
)

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 7, 28, 13, 0, 0, tzinfo=KST)
T1 = datetime(2026, 7, 28, 14, 30, 10, tzinfo=KST)
T2 = datetime(2026, 7, 28, 14, 31, 20, tzinfo=KST)


def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "availability.json"


def read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# --- 상태 키 --------------------------------------------------------------


def test_slot_key_matches_documented_format() -> None:
    assert slot_key("event-20260829", date(2026, 8, 29), time(14, 0)) == (
        "event-20260829:2026-08-29:14:00"
    )


def test_slot_keys_are_independent_per_time() -> None:
    keys = {
        slot_key("a", date(2026, 8, 29), time(11, 0)),
        slot_key("a", date(2026, 8, 29), time(14, 0)),
        slot_key("b", date(2026, 8, 29), time(11, 0)),
    }
    assert len(keys) == 3


# --- 읽기 ----------------------------------------------------------------


def test_missing_file_starts_from_empty_state(tmp_path: Path) -> None:
    state = load_state(state_path(tmp_path))
    assert state.slots == {}
    assert state.heartbeat_last_sent is None


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    original = State(
        slots={
            "event:2026-08-29:11:00": SlotState(
                status="sold_out",
                remaining=0,
                last_checked_at=T1,
                last_changed_at=T0,
                last_notified_remaining=None,
                consecutive_errors=0,
                last_error=None,
            ),
            "event:2026-08-29:14:00": SlotState(
                status="available",
                remaining=2,
                last_checked_at=T1,
                last_changed_at=T1,
                last_notified_remaining=1,
                consecutive_errors=3,
                last_error="rate_limited",
            ),
        },
        heartbeat_last_sent=date(2026, 7, 28),
    )

    save_state(path, original, T2)

    assert load_state(path).slots == original.slots
    assert load_state(path).heartbeat_last_sent == date(2026, 7, 28)


def test_saved_file_matches_documented_shape(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    state = State(slots={"event:2026-08-29:11:00": record_error(None, "timeout", T1)})

    save_state(path, state, T2)

    raw = read_json(path)
    assert raw["version"] == STATE_VERSION
    assert raw["updated_at"] == T2.isoformat()
    assert raw["heartbeat"] == {"last_sent_date": None}
    assert set(raw["slots"]["event:2026-08-29:11:00"]) == {
        "status",
        "remaining",
        "last_checked_at",
        "last_changed_at",
        "last_notified_remaining",
        "consecutive_errors",
        "last_error",
    }


def test_corrupt_file_is_a_fatal_error(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_unsupported_version_is_a_fatal_error(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 99, "slots": {}}), encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_unknown_status_value_is_a_fatal_error(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    payload = {
        "version": STATE_VERSION,
        "slots": {"k": {"status": "탈출", "remaining": 0, "consecutive_errors": 0}},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_missing_optional_fields_load_as_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    payload = {
        "version": STATE_VERSION,
        "slots": {"k": {"status": "unknown", "remaining": None, "consecutive_errors": 1}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    slot = load_state(path).slots["k"]
    assert slot.status == "unknown"
    assert slot.last_checked_at is None
    assert slot.last_notified_remaining is None


# --- 원자적 저장 ----------------------------------------------------------


def test_save_creates_missing_parent_directory(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    save_state(path, State(), T2)
    assert path.exists()


def test_save_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    save_state(path, State(), T2)
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_failed_save_keeps_the_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = state_path(tmp_path)
    save_state(path, State(slots={"k": record_error(None, "timeout", T1)}), T1)
    before = path.read_text(encoding="utf-8")

    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StateError):
        save_state(path, State(), T2)

    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]


# --- 정상 조회 기록 -------------------------------------------------------


def test_first_successful_check_with_stock_is_available() -> None:
    slot = record_success(None, remaining=1, threshold=1, checked_at=T1)
    assert (slot.status, slot.remaining) == ("available", 1)
    assert slot.last_checked_at == T1
    assert slot.last_changed_at == T1
    assert slot.consecutive_errors == 0
    assert slot.last_error is None
    assert slot.last_notified_remaining is None


def test_remaining_below_threshold_is_sold_out() -> None:
    slot = record_success(None, remaining=1, threshold=2, checked_at=T1)
    assert slot.status == "sold_out"


def test_unchanged_check_keeps_last_changed_at() -> None:
    first = record_success(None, remaining=0, threshold=1, checked_at=T0)
    second = record_success(first, remaining=0, threshold=1, checked_at=T1)
    assert second.last_checked_at == T1
    assert second.last_changed_at == T0


def test_changed_remaining_updates_last_changed_at() -> None:
    first = record_success(None, remaining=1, threshold=1, checked_at=T0)
    second = record_success(first, remaining=2, threshold=1, checked_at=T1)
    assert second.last_changed_at == T1


def test_status_change_updates_last_changed_at() -> None:
    first = record_success(None, remaining=0, threshold=1, checked_at=T0)
    second = record_success(first, remaining=1, threshold=1, checked_at=T1)
    assert (second.status, second.last_changed_at) == ("available", T1)


def test_success_preserves_notified_remaining() -> None:
    notified = replace(
        record_success(None, remaining=1, threshold=1, checked_at=T0),
        last_notified_remaining=1,
    )
    later = record_success(notified, remaining=1, threshold=1, checked_at=T1)
    assert later.last_notified_remaining == 1


def test_success_clears_accumulated_errors() -> None:
    failed = record_error(record_error(None, "timeout", T0), "http_error", T0)
    assert failed.consecutive_errors == 2

    recovered = record_success(failed, remaining=0, threshold=1, checked_at=T1)
    assert recovered.consecutive_errors == 0
    assert recovered.last_error is None


# --- 조회 실패 기록 -------------------------------------------------------


def test_error_never_overwrites_a_known_status() -> None:
    known = record_success(None, remaining=1, threshold=1, checked_at=T0)
    failed = record_error(known, "rate_limited", T1)

    assert failed.status == "available", "조회 실패를 매진으로 바꾸면 안 된다"
    assert failed.remaining == 1
    assert failed.last_changed_at == T0
    assert failed.last_error == "rate_limited"
    assert failed.consecutive_errors == 1


def test_error_does_not_advance_last_checked_at() -> None:
    known = record_success(None, remaining=1, threshold=1, checked_at=T0)
    assert record_error(known, "timeout", T1).last_checked_at == T0


def test_errors_accumulate() -> None:
    slot = None
    for expected in (1, 2, 3):
        slot = record_error(slot, "timeout", T1)
        assert slot.consecutive_errors == expected


def test_first_ever_check_failing_is_unknown_with_no_remaining() -> None:
    slot = record_error(None, "malformed_response", T1)
    assert (slot.status, slot.remaining) == ("unknown", None)
    assert slot.last_checked_at is None


def test_error_preserves_notified_remaining() -> None:
    slot = SlotState(
        status="available",
        remaining=1,
        last_checked_at=T0,
        last_changed_at=T0,
        last_notified_remaining=1,
        consecutive_errors=0,
        last_error=None,
    )
    assert record_error(slot, "timeout", T1).last_notified_remaining == 1
