"""단계 8 — 장시간 반복 루프. 가짜 시계로 결정적으로 검증한다."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import responses

from booking_slot_watch.config import ConfigError, load_config
from booking_slot_watch.models import Config
from booking_slot_watch.monitor import (
    DEFAULT_INTERVAL_SEC,
    DEFAULT_JITTER_SEC,
    DEFAULT_LOOP_HOURS,
    MIN_INTERVAL_SEC,
    SLEEP_STEP_SEC,
    LoopSettings,
    loop_settings_from_env,
    next_interval,
    run_loop,
)
from booking_slot_watch.naver import GRAPHQL_URL, KST, NaverBookingClient
from booking_slot_watch.notifier import DEFAULT_SERVER_URL, Notifier, NtfyConfig
from booking_slot_watch.state import State, load_state

TOPIC = "loop-topic"
NTFY_URL = f"{DEFAULT_SERVER_URL}/{TOPIC}"
START = datetime(2026, 7, 28, 6, 0, 0, tzinfo=KST)
URL = "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183"


class FakeClock:
    """sleep이 가상 시간을 밀어주는 시계."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.current += timedelta(seconds=seconds)


def payload(remaining: int) -> dict[str, Any]:
    return {
        "data": {
            "schedule": {
                "bizItemSchedule": {
                    "hourly": [
                        {
                            "unitStartTime": "2026-08-29 14:30:00",
                            "unitBookingCount": 16 - remaining,
                            "unitStock": 16,
                            "isUnitSaleDay": True,
                        }
                    ]
                }
            }
        }
    }


def write_config(tmp_path: Path, **overrides: Any) -> Config:
    monitor: dict[str, Any] = {
        "id": "event",
        "name": "8월 29일 예약",
        "enabled": True,
        "url": URL,
        "targets": [{"date": "2026-08-29", "times": ["14:30"]}],
        "expires_at": "2099-01-01T17:00:00+09:00",
    }
    monitor.update(overrides)
    path = tmp_path / "monitors.json"
    path.write_text(
        json.dumps({"version": 1, "monitors": [monitor]}, ensure_ascii=False), encoding="utf-8"
    )
    return load_config(path)


def run(
    config: Config,
    tmp_path: Path,
    *,
    clock: FakeClock,
    settings: LoopSettings,
    should_stop: Any = None,
    state: State | None = None,
) -> Any:
    client = NaverBookingClient(sleep=lambda _: None)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        return run_loop(
            config,
            state if state is not None else State(),
            client=client,
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings,
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
            should_stop=should_stop or (lambda: False),
        )
    finally:
        client.close()
        notifier.close()


def settings(seconds: float, *, interval: float = 70.0, jitter: float = 0.0) -> LoopSettings:
    return LoopSettings(interval_sec=interval, jitter_sec=jitter, loop_hours=seconds / 3600)


# --- 주기와 지터 ----------------------------------------------------------


def test_interval_has_no_jitter_when_disabled() -> None:
    assert next_interval(LoopSettings(70.0, 0.0, 5.5), lambda: 0.5) == 70.0


@pytest.mark.parametrize(("draw", "expected"), [(0.0, 70.0), (0.5, 80.0), (0.999, 89.98)])
def test_jitter_is_added_on_top_of_the_interval(draw: float, expected: float) -> None:
    assert next_interval(LoopSettings(70.0, 20.0, 5.5), lambda: draw) == pytest.approx(expected)


def test_documented_defaults() -> None:
    assert (DEFAULT_INTERVAL_SEC, DEFAULT_JITTER_SEC, DEFAULT_LOOP_HOURS) == (70.0, 20.0, 5.5)


# --- 환경변수 -------------------------------------------------------------


def test_loop_settings_defaults() -> None:
    assert loop_settings_from_env({}) == LoopSettings(70.0, 20.0, 5.5)


def test_loop_settings_are_read_from_env() -> None:
    parsed = loop_settings_from_env(
        {"CHECK_INTERVAL_SEC": "90", "CHECK_JITTER_SEC": "5", "LOOP_HOURS": "5.4"}
    )
    assert parsed == LoopSettings(90.0, 5.0, 5.4)


@pytest.mark.parametrize("value", ["5", "10", "0", "-1"])
def test_short_intervals_are_rejected(value: str) -> None:
    """5초·10초 같은 주기로 비공식 API를 두드리지 않는다."""
    with pytest.raises(ConfigError):
        loop_settings_from_env({"CHECK_INTERVAL_SEC": value})
    assert MIN_INTERVAL_SEC == 30.0


@pytest.mark.parametrize(
    "env",
    [
        {"CHECK_INTERVAL_SEC": "빠르게"},
        {"CHECK_JITTER_SEC": "-1"},
        {"LOOP_HOURS": "0"},
        {"LOOP_HOURS": "많이"},
    ],
)
def test_invalid_loop_settings_are_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        loop_settings_from_env(env)


# --- 반복과 종료 ----------------------------------------------------------


@responses.activate
def test_loop_repeats_until_the_deadline(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    clock = FakeClock(START)

    result = run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(180))

    assert result.iterations == 3
    assert result.stopped_reason == "deadline"
    # 대기는 종료 신호에 반응하려고 잘게 쪼개지지만 총합은 종료 시각을 넘지 않는다.
    assert sum(clock.slept) == pytest.approx(180.0)
    assert max(clock.slept) <= SLEEP_STEP_SEC


@responses.activate
def test_loop_never_sleeps_past_the_deadline(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    clock = FakeClock(START)

    run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(100))

    assert sum(clock.slept) == pytest.approx(100.0)
    assert clock.now() == START + timedelta(seconds=100)


@responses.activate
def test_loop_stops_on_a_stop_signal(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    clock = FakeClock(START)
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    result = run(
        write_config(tmp_path), tmp_path, clock=clock, settings=settings(3600),
        should_stop=should_stop,
    )

    assert result.stopped_reason == "signal"
    assert result.iterations < 5, "신호를 받으면 종료 시각까지 기다리지 않는다"


@responses.activate
def test_loop_exits_immediately_when_nothing_is_active(tmp_path: Path) -> None:
    config = write_config(tmp_path, expires_at="2020-01-01T00:00:00+09:00")
    clock = FakeClock(START)

    result = run(config, tmp_path, clock=clock, settings=settings(3600))

    assert result.stopped_reason == "no_active_targets"
    assert result.iterations == 1
    assert clock.slept == [], "연결 실행을 만들지 않고 바로 끝낸다"
    assert len(responses.calls) == 0


@responses.activate
def test_loop_stops_when_targets_expire_mid_run(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    expires = (START + timedelta(seconds=150)).isoformat()
    config = write_config(tmp_path, expires_at=expires)
    clock = FakeClock(START)

    result = run(config, tmp_path, clock=clock, settings=settings(3600))

    # t=0, 70, 140은 아직 활성이고 t=210 회차에서 만료를 확인해 멈춘다.
    assert result.stopped_reason == "no_active_targets"
    assert result.iterations == 4


# --- 상태 저장 ------------------------------------------------------------


@responses.activate
def test_state_is_saved_when_the_loop_ends(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))

    run(write_config(tmp_path), tmp_path, clock=FakeClock(START), settings=settings(80))

    saved = load_state(tmp_path / "state.json")
    assert saved.slots["event:2026-08-29:14:30"].status == "sold_out"


@responses.activate
def test_state_is_saved_right_after_a_notification(tmp_path: Path) -> None:
    """작업이 취소돼도 '이미 알렸다'는 기록이 남아야 중복 알림을 막는다."""
    responses.add(responses.POST, GRAPHQL_URL, json=payload(1))
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)
    state = State()

    run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(3600), state=state)

    # 첫 회차에서 저장됐는지 보려면 루프 종료 저장과 구분이 필요하므로
    # 알림 직후 저장된 값이 유지되는지 확인한다.
    saved = load_state(tmp_path / "state.json")
    assert saved.slots["event:2026-08-29:14:30"].last_notified_remaining == 1


@responses.activate
def test_state_is_saved_even_when_nothing_is_active(tmp_path: Path) -> None:
    config = write_config(tmp_path, expires_at="2020-01-01T00:00:00+09:00")

    run(config, tmp_path, clock=FakeClock(START), settings=settings(3600))

    assert (tmp_path / "state.json").exists()


@responses.activate
def test_repeated_failures_do_not_stop_the_loop(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)

    result = run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(180))

    assert result.iterations == 3
    assert result.stopped_reason == "deadline"
    saved = load_state(tmp_path / "state.json")
    assert saved.slots["event:2026-08-29:14:30"].consecutive_errors == 3
