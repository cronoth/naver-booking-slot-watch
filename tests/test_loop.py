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
from booking_slot_watch.state import State, StateError, load_state

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
    assert next_interval(LoopSettings(70.0, 0.0, 5.4), lambda: 0.5) == 70.0


@pytest.mark.parametrize(("draw", "expected"), [(0.0, 70.0), (0.5, 80.0), (0.999, 89.98)])
def test_jitter_is_added_on_top_of_the_interval(draw: float, expected: float) -> None:
    assert next_interval(LoopSettings(70.0, 20.0, 5.4), lambda: draw) == pytest.approx(expected)


def test_documented_defaults() -> None:
    """워크플로가 값을 다시 적지 않으므로 코드 기본값이 유일한 출처다."""
    assert (DEFAULT_INTERVAL_SEC, DEFAULT_JITTER_SEC, DEFAULT_LOOP_HOURS) == (70.0, 20.0, 5.4)


def test_loop_hours_stays_under_the_runner_job_limit() -> None:
    """GitHub-hosted 러너의 job 실행 상한은 6시간이다. 설정·커밋·연결 시간도 필요하다."""
    assert DEFAULT_LOOP_HOURS < 5.75


# --- 환경변수 -------------------------------------------------------------


def test_loop_settings_defaults() -> None:
    assert loop_settings_from_env({}) == LoopSettings(70.0, 20.0, 5.4)


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
    """작업이 취소돼도 '이미 알렸다'는 기록이 남아야 중복 알림을 막는다.

    루프 종료 저장과 구분하려면 루프가 끝나기 **전에** 파일을 확인해야 한다.
    첫 대기 시점에 이미 기록돼 있어야 한다.
    """
    responses.add(responses.POST, GRAPHQL_URL, json=payload(1))
    responses.add(responses.POST, NTFY_URL, json={})
    path = tmp_path / "state.json"
    observed: list[int | None] = []

    class ObservingClock(FakeClock):
        def sleep(self, seconds: float) -> None:
            if not observed:  # 첫 회차 직후, 루프 종료 저장 이전
                slot = load_state(path).slots["event:2026-08-29:14:30"]
                observed.append(slot.last_notified_remaining)
            super().sleep(seconds)

    run(write_config(tmp_path), tmp_path, clock=ObservingClock(START), settings=settings(180))

    assert observed == [1], "알림 직후 곧바로 저장돼 있어야 한다"


@responses.activate
def test_state_is_saved_even_when_nothing_is_active(tmp_path: Path) -> None:
    config = write_config(tmp_path, expires_at="2020-01-01T00:00:00+09:00")

    run(config, tmp_path, clock=FakeClock(START), settings=settings(3600))

    assert (tmp_path / "state.json").exists()


@responses.activate
def test_unchanged_state_is_not_rewritten_across_runs(tmp_path: Path) -> None:
    """변화가 없으면 파일을 다시 쓰지 않는다. Actions가 빈 커밋을 쌓지 않게 하는 조건."""
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    config = write_config(tmp_path)
    path = tmp_path / "state.json"

    run(config, tmp_path, clock=FakeClock(START), settings=settings(80))
    after_first = path.read_text(encoding="utf-8")

    later = FakeClock(START + timedelta(hours=6))
    run(config, tmp_path, clock=later, settings=settings(80), state=load_state(path))

    assert path.read_text(encoding="utf-8") == after_first


@responses.activate
def test_changed_remaining_is_written(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    config = write_config(tmp_path)
    path = tmp_path / "state.json"
    run(config, tmp_path, clock=FakeClock(START), settings=settings(80))
    after_first = path.read_text(encoding="utf-8")

    responses.reset()
    responses.add(responses.POST, GRAPHQL_URL, json=payload(1))
    responses.add(responses.POST, NTFY_URL, json={})
    later = FakeClock(START + timedelta(hours=6))
    run(config, tmp_path, clock=later, settings=settings(80), state=load_state(path))

    assert path.read_text(encoding="utf-8") != after_first
    assert load_state(path).slots["event:2026-08-29:14:30"].status == "available"


def test_unexpected_exception_does_not_kill_the_loop(tmp_path: Path) -> None:
    """한 회차의 예상 못한 예외가 5.4시간 job 전체를 죽이면 안 된다."""

    class ExplodingClient:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_hourly_schedule(self, identifiers: Any, target_date: Any) -> Any:
            self.calls += 1
            raise RuntimeError("예상 못한 오류")

        def close(self) -> None:
            pass

    client = ExplodingClient()
    clock = FakeClock(START)
    result = run_loop(
        write_config(tmp_path),
        State(),
        client=client,  # type: ignore[arg-type]
        notifier=Notifier(NtfyConfig(topic=TOPIC)),
        state_path=tmp_path / "state.json",
        settings=settings(180),
        clock=clock.now,
        sleep=clock.sleep,
        random_fn=lambda: 0.0,
    )

    assert result.iterations == 3
    assert result.stopped_reason == "deadline"
    assert client.calls == 3


def test_state_write_failure_stays_fatal(tmp_path: Path) -> None:
    """상태 파일 쓰기 실패는 치명적 오류다. 루프 가드가 이걸 삼키면 안 된다."""

    class ExplodingClient:
        def fetch_hourly_schedule(self, identifiers: Any, target_date: Any) -> Any:
            raise RuntimeError("조회 실패")

        def close(self) -> None:
            pass

    clock = FakeClock(START)
    # 부모가 파일이므로 mkdir 자체가 실패한다.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(StateError):
        run_loop(
            write_config(tmp_path),
            State(),
            client=ExplodingClient(),  # type: ignore[arg-type]
            notifier=Notifier(NtfyConfig(topic=TOPIC)),
            state_path=blocker / "state.json",
            settings=settings(180),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
            should_stop=lambda: True,  # 즉시 종료 저장으로 간다
        )


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
