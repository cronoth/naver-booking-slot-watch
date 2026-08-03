"""1회 조회 오케스트레이션과 장시간 반복 루프.

로그 형식은 docs/PROJECT_SPEC.md 13절을 따른다. 토픽·토큰은 남기지 않는다.
"""

import logging
import random
import time as time_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigError, active_monitors, group_schedule_requests
from .models import BookingIdentifiers, Config, MonitorConfig, SlotRequest
from .naver import HourlySchedule, NaverApiError, NaverBookingClient
from .notifier import Notifier
from .state import (
    SlotState,
    State,
    StateError,
    evaluate_error,
    evaluate_slot,
    mark_error_alert_sent,
    mark_notified,
    mark_send_failed,
    save_state,
    slot_key,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

#: 이 시각(KST) 이후 그날 첫 루프에서 Heartbeat를 보낸다.
HEARTBEAT_AFTER_HOUR = 7

DEFAULT_INTERVAL_SEC = 70.0
DEFAULT_JITTER_SEC = 20.0
#: 루프 길이(분) = 5시간 30분. 워크플로는 이 값을 다시 적지 않는다.
DEFAULT_LOOP_MINUTES = 330.0
#: 허용 하한(분). 이보다 작은 값은 단위를 잘못 넣은 것으로 보고 거부한다.
MIN_LOOP_MINUTES = 10.0
#: 허용 상한(분). 워크플로 timeout-minutes(350)를 넘으면 루프가 끝나기 전에 job이 죽는다.
MAX_LOOP_MINUTES = 350.0
#: 비공식 API를 짧은 주기로 두드리지 않기 위한 하한.
MIN_INTERVAL_SEC = 30.0
#: 종료 신호에 빠르게 반응하려고 대기를 이 간격으로 쪼갠다.
SLEEP_STEP_SEC = 1.0
#: 모든 회차가 이만큼 연속으로 실패하면 감시가 멈춘 것으로 보고 알린다.
#:
#: 조회 간격이 70~90초이므로 5회는 약 6~8분이다. 일시적 오류는 넘기고 지속되는
#: 장애만 잡는 값이다. 알림은 임계값에 도달한 회차에만 한 번 보낸다.
OUTAGE_ALERT_ITERATIONS = 5


@dataclass(frozen=True)
class CheckOutcome:
    monitors_active: int
    slots_active: int
    slots_checked: int
    slots_failed: int
    notifications_sent: int
    #: 종료 시각이 임박해 시작하지 않은 회차. 조회 실패가 아니다.
    slots_skipped: int = 0


def should_send_heartbeat(last_sent: date | None, now: datetime) -> bool:
    """KST 기준으로 하루 한 번, 정해진 시각 이후 첫 호출에만 True."""
    if now.hour < HEARTBEAT_AFTER_HOUR:
        return False
    return last_sent != now.date()


def latest_success(state: State) -> datetime | None:
    """마지막 정상 조회 시각."""
    seen = [slot.last_checked_at for slot in state.slots.values() if slot.last_checked_at]
    return max(seen) if seen else None


def check_once(
    config: Config,
    state: State,
    *,
    client: NaverBookingClient,
    notifier: Notifier,
    now: datetime,
    should_continue: Callable[[], bool] | None = None,
    on_notified: Callable[[], None] | None = None,
) -> CheckOutcome:
    """활성 대상을 한 번 조회하고 상태를 갱신한다. `state`를 제자리에서 바꾼다.

    `should_continue`가 False를 돌려주면 남은 그룹을 시작하지 않는다. 대상이 많을 때
    한 회차가 종료 시각을 넘겨 상태 저장과 연결 실행 예산을 먹는 것을 막는다.

    `on_notified`는 알림 전송에 성공한 직후 호출한다. 상태 저장을 이 함수가 끝날
    때까지 미루면, 뒤 그룹 조회가 길어지는 동안 작업이 취소돼 기록이 사라진다.
    """
    monitors = active_monitors(config, now)
    schedule_requests = group_schedule_requests(monitors)
    slots_active = sum(len(request.slots) for request in schedule_requests)

    checked = failed = notified = skipped = 0

    for index, request in enumerate(schedule_requests):
        if should_continue is not None and not should_continue():
            skipped = sum(len(r.slots) for r in schedule_requests[index:])
            logger.error("종료 시각이 임박해 남은 회차 %d건을 시작하지 않는다", skipped)
            break
        schedule, error = _fetch(client, request.identifiers, request.date, request.slots)
        for monitor, target_time in request.slots:
            checked += 1
            slot_failed, sent = _apply_slot(
                state,
                monitor,
                target_date=request.date,
                target_time=target_time,
                schedule=schedule,
                fetch_error=error,
                config=config,
                notifier=notifier,
                now=now,
                on_notified=on_notified,
            )
            failed += int(slot_failed)
            notified += int(sent)

    if slots_active and should_send_heartbeat(state.heartbeat_last_sent, now):
        # 이번 회차에서 조회한 것이 전부 실패했으면 '정상 작동 중'이라고 말하지 않는다.
        degraded = checked > 0 and failed == checked
        if notifier.notify_heartbeat(
            len(monitors), slots_active, latest_success(state), degraded=degraded
        ):
            state.heartbeat_last_sent = now.date()

    return CheckOutcome(
        monitors_active=len(monitors),
        slots_active=slots_active,
        slots_checked=checked,
        slots_failed=failed,
        notifications_sent=notified,
        slots_skipped=skipped,
    )


def _fetch(
    client: NaverBookingClient,
    identifiers: BookingIdentifiers,
    target_date: date,
    slots: tuple[SlotRequest, ...],
) -> tuple[HourlySchedule | None, str | None]:
    try:
        return client.fetch_hourly_schedule(identifiers, target_date), None
    except NaverApiError as exc:
        logger.error(
            "monitor=%s date=%s error=%s previous_state_preserved=true",
            ",".join(sorted({monitor.id for monitor, _ in slots})),
            target_date.isoformat(),
            exc.kind,
        )
        return None, exc.kind


def _apply_slot(
    state: State,
    monitor: MonitorConfig,
    *,
    target_date: date,
    target_time: time,
    schedule: HourlySchedule | None,
    fetch_error: str | None,
    config: Config,
    notifier: Notifier,
    now: datetime,
    on_notified: Callable[[], None] | None = None,
) -> tuple[bool, bool]:
    """한 회차를 판정하고 필요하면 알린다. `(조회 실패 여부, 전송 성공 여부)`."""
    key = slot_key(monitor.id, target_date, target_time)
    fingerprint = _fingerprint(monitor.identifiers)
    previous = state.slots.get(key)
    if previous is not None and previous.fingerprint not in (None, fingerprint):
        # 같은 id로 URL만 바꾼 경우다. 이전 상품의 알림 기록을 새 상품에 쓰면
        # 새 상품이 처음부터 예약 가능해도 알림이 누락된다.
        logger.warning("monitor=%s 상품이 바뀌었다 - 회차 상태를 초기화한다", monitor.id)
        previous = None

    def store(slot: SlotState) -> None:
        state.slots[key] = replace(slot, fingerprint=fingerprint)

    availability = schedule.find(target_time) if schedule is not None else None

    if availability is None:
        failure = fetch_error or _missing_reason(schedule)
        result = evaluate_error(
            previous, failure, error_alert_threshold=config.error_alert_threshold, failed_at=now
        )
        store(result.state)
        logger.info(
            "monitor=%s date=%s time=%s status=%s error=%s consecutive_errors=%d",
            monitor.id,
            target_date.isoformat(),
            target_time.strftime("%H:%M"),
            result.state.status,
            failure,
            result.state.consecutive_errors,
        )
        if not result.should_notify:
            return True, False
        sent = notifier.notify_error(
            monitor_name=monitor.name,
            target_date=target_date,
            target_time=target_time,
            error=failure,
            consecutive_errors=result.state.consecutive_errors,
            failed_at=now,
        )
        # 전송이 확인되지 않으면 표시를 남기지 않는다. 다음 조회에서 다시 시도한다.
        if sent:
            store(mark_error_alert_sent(result.state))
            if on_notified is not None:
                on_notified()
        return True, sent

    # 판매하지 않는 회차는 재고가 남아 있어도 예약할 수 없다.
    remaining = availability.remaining if availability.sale_enabled else 0
    result = evaluate_slot(
        previous,
        remaining=remaining,
        threshold=monitor.threshold,
        notify_on_initial_available=monitor.notify_on_initial_available,
        notify_on_increase=monitor.notify_on_increase,
        checked_at=now,
    )

    sent = False
    if result.should_notify:
        sent = notifier.notify_available(
            monitor_name=monitor.name,
            url=monitor.url,
            target_date=target_date,
            target_time=target_time,
            remaining=remaining,
            previous_status=previous.status if previous is not None else None,
            checked_at=now,
        )
    # 전송이 확인되지 않으면 알린 것으로 기록하지 않는다. 다음 루프에서 다시 시도한다.
    if sent:
        store(mark_notified(result.state))
        if on_notified is not None:
            on_notified()
    elif result.reason == "became_available":
        # 낡은 수량이 남으면 재개방 알림이 영구히 묻힌다. 지워서 다시 시도하게 한다.
        store(mark_send_failed(result.state))
    else:
        # 증가 알림 실패에는 과거 알림 수량을 남긴다. 지우면 다음 조회에서 수량이
        # 줄어도 '재개방'으로 오판해, 이미 알린 것보다 적은 수량을 또 알린다.
        # 수량이 계속 높으면 증가 조건으로 다시 시도한다.
        store(result.state)

    logger.info(
        "monitor=%s date=%s time=%s status=%s remaining=%d notified=%s",
        monitor.id,
        target_date.isoformat(),
        target_time.strftime("%H:%M"),
        result.state.status,
        remaining,
        "true" if sent else "false",
    )
    return False, sent


def _fingerprint(identifiers: BookingIdentifiers) -> str:
    """상태가 어느 상품에서 나왔는지 알아보는 지문."""
    return (
        f"{identifiers.business_type_id}/{identifiers.business_id}/{identifiers.biz_item_id}"
    )


def _missing_reason(schedule: HourlySchedule | None) -> str:
    """회차가 아예 없는 날짜와 지정 시간만 사라진 경우를 구분한다."""
    if schedule is not None and not schedule.slots:
        return "empty_schedule"
    return "slot_not_found"


@dataclass(frozen=True)
class LoopSettings:
    interval_sec: float = DEFAULT_INTERVAL_SEC
    jitter_sec: float = DEFAULT_JITTER_SEC
    loop_minutes: float = DEFAULT_LOOP_MINUTES


@dataclass(frozen=True)
class LoopResult:
    iterations: int
    stopped_reason: str
    #: 종료 시점의 연속 전면 실패 회차 수. 0이면 마지막 조회는 일부라도 성공했다.
    consecutive_all_failed: int = 0


def loop_settings_from_env(env: Mapping[str, str]) -> LoopSettings:
    interval = _float_setting(env, "CHECK_INTERVAL_SEC", DEFAULT_INTERVAL_SEC, minimum=None)
    if interval < MIN_INTERVAL_SEC:
        raise ConfigError(
            f"CHECK_INTERVAL_SEC는 {MIN_INTERVAL_SEC:.0f}초 이상이어야 한다: {interval}"
        )
    loop_minutes = _float_setting(
        env, "LOOP_MINUTES", DEFAULT_LOOP_MINUTES, minimum=None, positive=True
    )
    if not MIN_LOOP_MINUTES <= loop_minutes <= MAX_LOOP_MINUTES:
        raise ConfigError(
            f"LOOP_MINUTES는 분 단위로 {MIN_LOOP_MINUTES:.0f}~{MAX_LOOP_MINUTES:.0f} "
            f"사이여야 한다: {loop_minutes}"
        )
    return LoopSettings(
        interval_sec=interval,
        jitter_sec=_float_setting(env, "CHECK_JITTER_SEC", DEFAULT_JITTER_SEC, minimum=0.0),
        loop_minutes=loop_minutes,
    )


def _float_setting(
    env: Mapping[str, str],
    key: str,
    default: float,
    *,
    minimum: float | None,
    positive: bool = False,
) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key}는 숫자여야 한다: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key}는 {minimum} 이상이어야 한다: {value}")
    if positive and value <= 0:
        raise ConfigError(f"{key}는 0보다 커야 한다: {value}")
    return value


def next_interval(settings: LoopSettings, random_fn: Callable[[], float]) -> float:
    """다음 조회까지의 대기 시간. 지터는 기본 간격 위에 더한다."""
    return settings.interval_sec + settings.jitter_sec * random_fn()


def run_loop(
    config: Config,
    state: State,
    *,
    client: NaverBookingClient,
    notifier: Notifier,
    state_path: Path,
    settings: LoopSettings,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    random_fn: Callable[[], float] = random.random,
    should_stop: Callable[[], bool] = lambda: False,
) -> LoopResult:
    """종료 시각까지 반복 조회한다. 활성 대상이 사라지면 즉시 끝낸다."""
    now_fn = clock if clock is not None else _now
    sleep_fn = sleep if sleep is not None else time_module.sleep

    started = now_fn()
    deadline = started + timedelta(minutes=settings.loop_minutes)
    logger.info(
        "루프 시작: 종료 예정 %s 간격 %.0f~%.0f초",
        deadline.strftime("%Y-%m-%d %H:%M:%S %Z"),
        settings.interval_sec,
        settings.interval_sec + settings.jitter_sec,
    )

    iterations = 0
    reason = "deadline"
    all_failed_streak = 0
    # 다음 Actions 실행은 이미 새 Session으로 시작한다. 저장된 장애 알림이 있으면
    # 같은 장애에서 또 교체하지 않는다.
    session_reset_for_outage = state.outage_alert_sent
    #: 전면 장애 알림에 넣을 대상 수. 조회에 성공한 마지막 회차의 값이다.
    last_slots_active = 0

    def saver(at: datetime) -> Callable[[], None]:
        """알림 직후 저장용 콜백. 회차 시각을 인자로 묶어 루프 변수 포획을 피한다."""

        def save() -> None:
            save_state(state_path, state, at)

        return save

    def note_blackout(at: datetime) -> None:
        """이 회차에서 결과를 하나도 얻지 못했다.

        조회가 전부 실패한 경우와 예상 못한 예외로 회차가 통째로 날아간 경우를
        같이 센다. 둘 다 감시가 사실상 멈춘 상태이고 사용자에게 할 말도 같다.
        job은 실패시키지 않는다 — 실패시키면 연결 실행이 끊기고 지연이 큰 cron에
        복구를 맡기게 된다. 대신 알려서 보이게 만든다.
        """
        nonlocal all_failed_streak, session_reset_for_outage
        all_failed_streak += 1
        if all_failed_streak < OUTAGE_ALERT_ITERATIONS:
            return
        if all_failed_streak == OUTAGE_ALERT_ITERATIONS:
            logger.error("모든 회차가 %d회 연속 실패했다", all_failed_streak)
        if not state.outage_alert_sent:
            logger.info("감시 실패 알림 전송 시도")
            if notifier.notify_outage(
                consecutive_iterations=all_failed_streak,
                slots=last_slots_active,
                last_success_at=latest_success(state),
                detected_at=at,
            ):
                state.outage_alert_sent = True
                save_state(state_path, state, at)
        if not session_reset_for_outage:
            client.reset_session()
            session_reset_for_outage = True

    while True:
        if should_stop():
            reason = "signal"
            break

        now = now_fn()
        if now >= deadline:
            break

        iterations += 1
        try:
            outcome = check_once(
                config,
                state,
                client=client,
                notifier=notifier,
                now=now,
                should_continue=lambda: now_fn() < deadline,
                # 작업이 취소돼도 '이미 알렸다'는 기록이 남아야 중복 알림을 막는다.
                on_notified=saver(now),
            )
        except StateError:
            raise  # 상태 파일을 쓸 수 없는 것은 치명적 오류다.
        except Exception:
            # 예상 못한 예외 하나로 5시간 30분 job을 잃지 않는다. 상태는 그대로 남는다.
            # 다만 이 경로가 반복되면 조회가 0건인데 job은 성공으로 끝나므로,
            # 전면 실패와 같이 세서 장애로 승격시킨다.
            logger.exception("조회 회차에서 예상 못한 오류 - 루프를 계속한다")
            note_blackout(now)
            # 예외 전에 일부 그룹은 끝났을 수 있다. 거기까지는 남긴다.
            save_state(state_path, state, now)
            self_sleep = min(next_interval(settings, random_fn), _remaining(deadline, now_fn))
            if self_sleep <= 0:
                break
            _sleep_in_steps(self_sleep, sleep_fn, should_stop)
            continue

        if outcome.slots_active == 0:
            logger.info("활성 대상 없음 — 루프를 종료한다")
            reason = "no_active_targets"
            break

        last_slots_active = outcome.slots_active
        if outcome.slots_checked > 0 and outcome.slots_failed == outcome.slots_checked:
            note_blackout(now)
        else:
            if state.outage_alert_sent and outcome.slots_checked > outcome.slots_failed:
                logger.info("전면 실패 상태 해소")
                state.outage_alert_sent = False
                state.recovery_alert_pending = True
                save_state(state_path, state, now)

            if state.recovery_alert_pending and outcome.slots_checked > outcome.slots_failed:
                if notifier.notify_recovery(slots=outcome.slots_active, recovered_at=now):
                    state.recovery_alert_pending = False
                    save_state(state_path, state, now)
                    logger.info("감시 복구 알림 전송 성공")
                else:
                    logger.warning("감시 복구 알림 전송 실패 - 다음 성공 회차에서 재시도")
            all_failed_streak = 0
            session_reset_for_outage = False

        # 회차마다 체크포인트한다. GitHub이 취소할 때 프로세스를 강제 종료하면
        # 루프 종료 저장이 실행되지 않아, 알림을 동반하지 않는 전이가 통째로 사라진다.
        # available → sold_out을 잃으면 새 실행이 원격의 available을 읽고, 다시
        # 같은 수량이 열려도 재개방으로 보지 않아 알림이 누락된다.
        #
        # 비용은 회차마다 기존 파일을 읽어 비교하는 것뿐이다(save_state의
        # _read_existing). 실질 상태가 같으면 임시 파일 작성과 os.replace는 하지
        # 않으므로 쓰기와 커밋은 늘지 않는다.
        #
        # 알림 직후 저장(on_notified)은 그대로 둔다 — 한 회차 안에서 뒤 그룹이
        # 길어지는 동안 취소되면 이 체크포인트까지 오지 못한다.
        save_state(state_path, state, now)

        remaining = _remaining(deadline, now_fn)
        if remaining <= 0:
            break
        _sleep_in_steps(min(next_interval(settings, random_fn), remaining), sleep_fn, should_stop)

    written = save_state(state_path, state, now_fn())
    logger.info(
        "루프 종료: iterations=%d reason=%s state=%s all_failed_streak=%d",
        iterations,
        reason,
        "saved" if written else "unchanged",
        all_failed_streak,
    )
    return LoopResult(
        iterations=iterations,
        stopped_reason=reason,
        consecutive_all_failed=all_failed_streak,
    )


def _remaining(deadline: datetime, now_fn: Callable[[], datetime]) -> float:
    return (deadline - now_fn()).total_seconds()


def _sleep_in_steps(
    seconds: float, sleep_fn: Callable[[float], None], should_stop: Callable[[], bool]
) -> None:
    """종료 신호에 빠르게 반응하려고 긴 대기를 잘게 쪼갠다."""
    remaining = seconds
    while remaining > 0 and not should_stop():
        step = min(SLEEP_STEP_SEC, remaining)
        sleep_fn(step)
        remaining -= step


def _now() -> datetime:
    return datetime.now(KST)
