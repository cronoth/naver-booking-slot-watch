"""단계 8 — 장시간 반복 루프. 가짜 시계로 결정적으로 검증한다."""

import json
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path
from typing import Any

import pytest
import responses

from booking_slot_watch.config import ConfigError, load_config
from booking_slot_watch.models import Config
from booking_slot_watch.monitor import (
    DEFAULT_INTERVAL_SEC,
    DEFAULT_JITTER_SEC,
    DEFAULT_LOOP_MINUTES,
    MAX_LOOP_MINUTES,
    MIN_INTERVAL_SEC,
    MIN_LOOP_MINUTES,
    OUTAGE_ALERT_ITERATIONS,
    SLEEP_STEP_SEC,
    LoopSettings,
    loop_settings_from_env,
    next_interval,
    run_loop,
)
from booking_slot_watch.naver import (
    GRAPHQL_URL,
    KST,
    NaverApiError,
    NaverBookingClient,
    parse_hourly_schedule,
)
from booking_slot_watch.notifier import DEFAULT_SERVER_URL, Notifier, NtfyConfig
from booking_slot_watch.state import SlotState, State, StateError, load_state, save_state

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


class ScriptedClient:
    """회차별 실패·성공과 Session 초기화 순서를 기록하는 대역."""

    def __init__(self, failures: list[bool]) -> None:
        self.failures = failures
        self.calls = 0
        self.resets = 0
        self.events: list[str] = []

    def fetch_hourly_schedule(self, identifiers: Any, target_date: Any) -> Any:
        self.calls += 1
        self.events.append(f"fetch:{self.calls}")
        if self.failures.pop(0) if self.failures else False:
            raise NaverApiError("timed out", kind="timeout")
        return parse_hourly_schedule(payload(0), target_date)

    def reset_session(self) -> None:
        self.resets += 1
        self.events.append("reset")

    def close(self) -> None:
        pass


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


def ntfy_calls() -> list[Any]:
    return [c for c in responses.calls if c.request.url.startswith(DEFAULT_SERVER_URL)]


def _title_of(request: Any) -> str:
    """RFC 2047로 인코딩된 Title 헤더를 되돌린다."""
    text, charset = decode_header(request.headers.get("Title", ""))[0]
    return text.decode(charset or "utf-8") if isinstance(text, bytes) else text


def ntfy_titles() -> list[str]:
    return [_title_of(call.request) for call in ntfy_calls()]


def outage_alerts() -> list[str]:
    return [t for t in ntfy_titles() if "감시 실패" in t]


def recovery_alerts() -> list[str]:
    return [t for t in ntfy_titles() if "감시 복구" in t]


def settings(seconds: float, *, interval: float = 70.0, jitter: float = 0.0) -> LoopSettings:
    return LoopSettings(interval_sec=interval, jitter_sec=jitter, loop_minutes=seconds / 60)


# --- 주기와 지터 ----------------------------------------------------------


def test_interval_has_no_jitter_when_disabled() -> None:
    assert next_interval(LoopSettings(70.0, 0.0, 330.0), lambda: 0.5) == 70.0


@pytest.mark.parametrize(("draw", "expected"), [(0.0, 70.0), (0.5, 80.0), (0.999, 89.98)])
def test_jitter_is_added_on_top_of_the_interval(draw: float, expected: float) -> None:
    assert next_interval(LoopSettings(70.0, 20.0, 330.0), lambda: draw) == pytest.approx(expected)


def test_documented_defaults() -> None:
    """워크플로가 값을 다시 적지 않으므로 코드 기본값이 유일한 출처다."""
    assert (DEFAULT_INTERVAL_SEC, DEFAULT_JITTER_SEC, DEFAULT_LOOP_MINUTES) == (70.0, 20.0, 330.0)


def test_default_loop_length_fits_the_workflow_timeout() -> None:
    """timeout-minutes 350, 플랫폼 job 상한 360분. 설정·커밋·연결 시간도 남겨야 한다."""
    assert DEFAULT_LOOP_MINUTES <= MAX_LOOP_MINUTES
    assert MAX_LOOP_MINUTES < 360.0


# --- 환경변수 -------------------------------------------------------------


def test_loop_settings_defaults() -> None:
    assert loop_settings_from_env({}) == LoopSettings(70.0, 20.0, 330.0)


def test_loop_settings_are_read_from_env() -> None:
    parsed = loop_settings_from_env(
        {"CHECK_INTERVAL_SEC": "90", "CHECK_JITTER_SEC": "5", "LOOP_MINUTES": "300"}
    )
    assert parsed == LoopSettings(90.0, 5.0, 300.0)


@pytest.mark.parametrize("value", ["5.5", "5", "0.5", "9"])
def test_loop_minutes_rejects_values_below_the_lower_bound(value: str) -> None:
    """분 단위이므로 5.5는 5분 30초 루프다. 단위를 잘못 넣은 것으로 보고 거부한다."""
    with pytest.raises(ConfigError):
        loop_settings_from_env({"LOOP_MINUTES": value})
    assert MIN_LOOP_MINUTES == 10.0


@pytest.mark.parametrize("value", ["351", "360", "600"])
def test_loop_minutes_rejects_values_past_the_workflow_timeout(value: str) -> None:
    with pytest.raises(ConfigError):
        loop_settings_from_env({"LOOP_MINUTES": value})


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
        {"LOOP_MINUTES": "0"},
        {"LOOP_MINUTES": "많이"},
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

    첫 대기 시점에 수량까지 기록돼 있는지 본다. 회차 체크포인트만으로도 이 조건은
    만족하므로 이 테스트는 `on_notified`를 따로 검증하지 않는다 — 그 역할은
    `test_state_is_saved_before_the_next_request_group_starts`가 한다.
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
def test_state_is_saved_before_the_next_request_group_starts(tmp_path: Path) -> None:
    """그룹 1의 알림 기록이 그룹 2 조회를 기다리면 안 된다.

    두 번째 상품 조회가 재시도·timeout으로 길어지고 그 사이 작업이 취소되면
    첫 알림의 성공 기록이 남지 않아 다음 실행이 같은 알림을 다시 보낸다.
    """
    path = tmp_path / "state.json"
    observed: list[Any] = []

    def graphql(request: Any) -> tuple[int, dict[str, str], str]:
        params = json.loads(request.body or "")["variables"]["scheduleParams"]
        if params["startDateTime"].startswith("2026-08-30") and not observed:
            observed.append(load_state(path).slots.get("event:2026-08-29:14:30"))
        return 200, {}, json.dumps(payload(1))

    responses.add_callback(
        responses.POST, GRAPHQL_URL, callback=graphql, content_type="application/json"
    )
    responses.add(responses.POST, NTFY_URL, json={})
    config = write_config(
        tmp_path,
        targets=[
            {"date": "2026-08-29", "times": ["14:30"]},
            {"date": "2026-08-30", "times": ["14:30"]},
        ],
    )

    run(config, tmp_path, clock=FakeClock(START), settings=settings(60))

    assert observed and observed[0] is not None, "두 번째 그룹 시작 전에 저장돼 있어야 한다"
    assert observed[0].last_notified_remaining == 1


def notified_available(remaining: int) -> State:
    """원격에 저장돼 있던 상태: 예약 가능이고 그 수량까지 알림에 성공했다."""
    return State(
        slots={
            "event:2026-08-29:14:30": SlotState(
                status="available",
                remaining=remaining,
                last_checked_at=None,
                last_changed_at=None,
                last_notified_remaining=remaining,
                consecutive_errors=0,
                last_error=None,
            )
        }
    )


@responses.activate
def test_state_is_checkpointed_after_every_iteration(tmp_path: Path) -> None:
    """알림을 동반하지 않는 상태 전이도 회차마다 파일에 남아야 한다.

    GitHub이 취소할 때 프로세스를 강제 종료하면 루프 종료 저장이 실행되지 않는다.
    실제로 취소된 실행의 로그에 `루프 종료:` 줄이 없고 `Commit state`가
    '상태 변화 없음'으로 끝났다. 알림이 없는 전이는 그대로 유실됐다.
    """
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    path = tmp_path / "state.json"
    observed: list[Any] = []

    class ObservingClock(FakeClock):
        def sleep(self, seconds: float) -> None:
            if not observed:  # 첫 회차 직후, 루프 종료 저장 이전
                observed.append(load_state(path).slots.get("event:2026-08-29:14:30"))
            super().sleep(seconds)

    run(
        write_config(tmp_path),
        tmp_path,
        clock=ObservingClock(START),
        settings=settings(180),
        state=notified_available(1),
    )

    assert observed and observed[0] is not None, "회차 직후 파일이 있어야 한다"
    assert observed[0].status == "sold_out", "알림 없는 전이도 회차 직후 저장돼야 한다"


@responses.activate
def test_reopen_is_detected_after_the_process_is_killed(tmp_path: Path) -> None:
    """한 회차 완료 직후 강제 종료돼도 다음 실행이 매진 전이를 알고 재개방을 잡아야 한다.

    전이를 잃으면 새 실행이 원격의 `available`/`lnr=1`을 읽고, 다시 1석이 열려도
    `1 > 1`이 거짓이라 재개방 알림을 보내지 않는다.
    """
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    path = tmp_path / "state.json"
    snapshot: list[str] = []

    class KillAfterFirstIteration(FakeClock):
        def sleep(self, seconds: float) -> None:
            if not snapshot and path.exists():
                snapshot.append(path.read_text(encoding="utf-8"))
            super().sleep(seconds)

    run(
        write_config(tmp_path),
        tmp_path,
        clock=KillAfterFirstIteration(START),
        settings=settings(180),
        state=notified_available(1),
    )

    assert snapshot, "첫 회차 직후 체크포인트가 없으면 강제 종료로 전이가 사라진다"

    # 강제 종료된 프로세스가 남긴 파일만 가지고 새 실행이 시작한다.
    revived = tmp_path / "revived.json"
    revived.write_text(snapshot[0], encoding="utf-8")
    resumed = load_state(revived)
    assert resumed.slots["event:2026-08-29:14:30"].status == "sold_out"

    responses.reset()
    responses.add(responses.POST, GRAPHQL_URL, json=payload(1))
    responses.add(responses.POST, NTFY_URL, json={})
    run(
        write_config(tmp_path),
        tmp_path,
        clock=FakeClock(START),
        settings=settings(60),
        state=resumed,
    )

    assert any("예약 가능" in title for title in ntfy_titles()), "재개방을 알려야 한다"


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


def test_loop_stops_a_slow_iteration_from_eating_the_handoff_budget(tmp_path: Path) -> None:
    """조회가 느려 종료 시각을 넘기면 남은 그룹을 시작하지 않아야 한다."""
    clock = FakeClock(START)

    class SlowClient:
        """조회 한 번에 가상 시간 100초를 쓴다."""

        def __init__(self) -> None:
            self.dates: list[Any] = []

        def fetch_hourly_schedule(self, identifiers: Any, target_date: Any) -> Any:
            self.dates.append(target_date)
            clock.current += timedelta(seconds=100)
            raise NaverApiError("느림", kind="timeout")

        def close(self) -> None:
            pass

    entry = {
        "id": "event",
        "name": "8월 29일 예약",
        "enabled": True,
        "url": URL,
        "targets": [
            {"date": "2026-08-29", "times": ["14:30"]},
            {"date": "2026-08-30", "times": ["14:30"]},
            {"date": "2026-08-31", "times": ["14:30"]},
        ],
        "expires_at": "2099-01-01T17:00:00+09:00",
    }
    path = tmp_path / "monitors.json"
    path.write_text(json.dumps({"version": 1, "monitors": [entry]}, ensure_ascii=False), "utf-8")
    config = load_config(path)

    client = SlowClient()
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            config,
            State(),
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings(150),  # 150초 예산: 두 번째 조회 후 초과
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert len(client.dates) == 2, f"세 번째 그룹까지 돌면 예산을 넘긴다 (실제 {client.dates})"


def test_unexpected_exception_does_not_kill_the_loop(tmp_path: Path) -> None:
    """한 회차의 예상 못한 예외가 5시간 30분 job 전체를 죽이면 안 된다."""

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
def test_sustained_total_failure_alerts_once_and_keeps_the_chain(tmp_path: Path) -> None:
    """전면 장애를 알리되 job은 실패시키지 않는다.

    실패시키면 연결 실행이 끊기고 지연이 큰 cron에 복구를 맡기게 된다.
    """
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)

    result = run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(8 * 70))

    assert result.iterations == 8
    assert result.stopped_reason == "deadline", "장애가 나도 체인은 유지된다"
    assert result.consecutive_all_failed == 8

    assert len(outage_alerts()) == 1, f"임계값 도달 회차에만 한 번 (실제 {ntfy_titles()})"


@responses.activate
def test_outage_resets_session_once_after_alert_and_then_recovers(tmp_path: Path) -> None:
    client = ScriptedClient([True] * OUTAGE_ALERT_ITERATIONS + [False])
    clock = FakeClock(START)

    def ntfy(request: Any) -> tuple[int, dict[str, str], str]:
        title = _title_of(request)
        if title == "Naver Booking Slot Watch 감시 실패":
            client.events.append("outage_alert")
        if title == "Naver Booking Slot Watch 감시 복구":
            client.events.append("recovery_alert")
        return 200, {}, "{}"

    responses.add_callback(
        responses.POST, NTFY_URL, callback=ntfy, content_type="application/json"
    )
    state = State()
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        result = run_loop(
            write_config(tmp_path),
            state,
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings(6 * 70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert result.iterations == 6
    assert client.resets == 1
    assert client.events.index("outage_alert") < client.events.index("reset")
    assert client.events.index("reset") < client.events.index("fetch:6")
    assert len(outage_alerts()) == 1
    assert len(recovery_alerts()) == 1
    assert state.outage_alert_sent is False
    assert load_state(tmp_path / "state.json").outage_alert_sent is False


@responses.activate
def test_outage_notification_retry_does_not_reset_session_again(tmp_path: Path) -> None:
    client = ScriptedClient([True] * (OUTAGE_ALERT_ITERATIONS + 1))
    attempts = 0
    clock = FakeClock(START)

    def ntfy(request: Any) -> tuple[int, dict[str, str], str]:
        nonlocal attempts
        if _title_of(request) == "Naver Booking Slot Watch 감시 실패":
            attempts += 1
            return (500 if attempts == 1 else 200), {}, "{}"
        return 200, {}, "{}"

    responses.add_callback(
        responses.POST, NTFY_URL, callback=ntfy, content_type="application/json"
    )
    state = State()
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            write_config(tmp_path),
            state,
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings((OUTAGE_ALERT_ITERATIONS + 1) * 70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert attempts == 2
    assert client.resets == 1
    assert state.outage_alert_sent is True


@responses.activate
def test_persisted_outage_alert_sends_one_recovery_after_a_new_run(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert save_state(path, State(outage_alert_sent=True), START) is True
    state = load_state(path)
    client = ScriptedClient([False])
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            write_config(tmp_path),
            state,
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=path,
            settings=settings(70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert len(recovery_alerts()) == 1
    assert state.outage_alert_sent is False
    assert load_state(path).outage_alert_sent is False


@responses.activate
def test_persisted_outage_does_not_reset_the_fresh_session_again(tmp_path: Path) -> None:
    client = ScriptedClient([True] * OUTAGE_ALERT_ITERATIONS)
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            write_config(tmp_path),
            State(outage_alert_sent=True),
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings(OUTAGE_ALERT_ITERATIONS * 70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert client.resets == 0
    assert outage_alerts() == []


@responses.activate
def test_failed_recovery_notification_is_retried_on_the_next_success(tmp_path: Path) -> None:
    client = ScriptedClient([False, False])
    attempts = 0

    def ntfy(request: Any) -> tuple[int, dict[str, str], str]:
        nonlocal attempts
        if _title_of(request) == "Naver Booking Slot Watch 감시 복구":
            attempts += 1
            return (500 if attempts == 1 else 200), {}, "{}"
        return 200, {}, "{}"

    responses.add_callback(
        responses.POST, NTFY_URL, callback=ntfy, content_type="application/json"
    )
    state = State(outage_alert_sent=True)
    clock = FakeClock(START)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            write_config(tmp_path),
            state,
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings(2 * 70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert attempts == 2
    assert state.outage_alert_sent is False
    assert state.recovery_alert_pending is False


@responses.activate
def test_failed_recovery_notification_does_not_block_a_new_outage_alert(tmp_path: Path) -> None:
    client = ScriptedClient(
        [True] * OUTAGE_ALERT_ITERATIONS + [False] + [True] * OUTAGE_ALERT_ITERATIONS
    )
    recovery_attempts = 0

    def ntfy(request: Any) -> tuple[int, dict[str, str], str]:
        nonlocal recovery_attempts
        if _title_of(request) == "Naver Booking Slot Watch 감시 복구":
            recovery_attempts += 1
            return 500, {}, "{}"
        return 200, {}, "{}"

    responses.add_callback(
        responses.POST, NTFY_URL, callback=ntfy, content_type="application/json"
    )
    state = State()
    clock = FakeClock(START)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            write_config(tmp_path),
            state,
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings((2 * OUTAGE_ALERT_ITERATIONS + 1) * 70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert len(outage_alerts()) == 2
    assert recovery_attempts == 1
    assert client.resets == 2
    assert state.outage_alert_sent is True
    assert state.recovery_alert_pending is True


@responses.activate
def test_recovery_allows_a_new_outage_to_reset_the_session_again(tmp_path: Path) -> None:
    client = ScriptedClient(
        [True] * OUTAGE_ALERT_ITERATIONS + [False] + [True] * OUTAGE_ALERT_ITERATIONS + [False]
    )
    responses.add(responses.POST, NTFY_URL, json={})
    state = State()
    clock = FakeClock(START)
    notifier = Notifier(NtfyConfig(topic=TOPIC))
    try:
        run_loop(
            write_config(tmp_path),
            state,
            client=client,  # type: ignore[arg-type]
            notifier=notifier,
            state_path=tmp_path / "state.json",
            settings=settings(12 * 70),
            clock=clock.now,
            sleep=clock.sleep,
            random_fn=lambda: 0.0,
        )
    finally:
        notifier.close()

    assert client.resets == 2
    assert len(outage_alerts()) == 2
    assert len(recovery_alerts()) == 2
    assert state.outage_alert_sent is False


@responses.activate
def test_outage_alert_is_retried_until_the_send_succeeds(tmp_path: Path) -> None:
    """임계값 회차에만 보내면 그때 ntfy가 실패하면 감시가 멈춘 것을 아무도 모른다."""
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    attempts = {"n": 0}

    def ntfy(request: Any) -> tuple[int, dict[str, str], str]:
        if "감시 실패" not in _title_of(request):
            return 200, {}, "{}"
        attempts["n"] += 1
        return (500 if attempts["n"] == 1 else 200), {}, "{}"

    responses.add_callback(
        responses.POST, NTFY_URL, callback=ntfy, content_type="application/json"
    )
    clock = FakeClock(START)

    run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(8 * 70))

    assert attempts["n"] == 2, f"실패 1회 + 성공 1회여야 한다 (실제 {attempts['n']})"


@responses.activate
def test_repeated_unexpected_errors_are_reported_as_an_outage(tmp_path: Path) -> None:
    """코드 버그가 매 회차 반복되면 조회는 0건인데 job은 성공으로 끝나 아무도 모른다."""
    responses.add(responses.POST, NTFY_URL, json={})

    class ExplodingClient:
        def fetch_hourly_schedule(self, identifiers: Any, target_date: Any) -> Any:
            raise RuntimeError("예상 못한 오류")

        def reset_session(self) -> None:
            pass

        def close(self) -> None:
            pass

    clock = FakeClock(START)
    result = run_loop(
        write_config(tmp_path),
        State(),
        client=ExplodingClient(),  # type: ignore[arg-type]
        notifier=Notifier(NtfyConfig(topic=TOPIC)),
        state_path=tmp_path / "state.json",
        settings=settings(6 * 70),
        clock=clock.now,
        sleep=clock.sleep,
        random_fn=lambda: 0.0,
    )

    assert result.iterations == 6
    assert result.stopped_reason == "deadline", "장애를 알리되 체인은 유지한다"
    assert result.consecutive_all_failed == 6
    assert len(outage_alerts()) == 1


@responses.activate
def test_recovery_resets_the_outage_streak(tmp_path: Path) -> None:
    for _ in range(3):
        responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)

    result = run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(4 * 70))

    assert result.consecutive_all_failed == 0, "성공하면 연속 카운트가 초기화된다"


@responses.activate
def test_no_outage_alert_below_the_threshold(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, NTFY_URL, json={})
    clock = FakeClock(START)

    result = run(write_config(tmp_path), tmp_path, clock=clock, settings=settings(3 * 70))

    assert result.consecutive_all_failed == 3
    assert result.consecutive_all_failed < OUTAGE_ALERT_ITERATIONS
    assert outage_alerts() == [], "임계값 미달이면 전면 실패 알림은 없다"


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
