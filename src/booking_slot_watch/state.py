"""상태 파일 읽기·원자적 저장과 회차 상태 기록.

형식은 docs/CONFIG_AND_STATE.md 5·6절을 따른다.

핵심 계약: 조회 실패(`record_error`)는 기존 `status`/`remaining`/`last_changed_at`을
절대 덮어쓰지 않는다. API 오류와 `0석`은 다른 사건이다.
"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal, cast, get_args

STATE_VERSION = 1

SlotStatus = Literal["unknown", "sold_out", "available"]
_VALID_STATUSES = frozenset(get_args(SlotStatus))

NotificationReason = Literal[
    "initial_available",
    "became_available",
    "remaining_increased",
    "error_threshold_reached",
    "heartbeat",
]


class StateError(Exception):
    """상태 파일을 읽거나 쓸 수 없다. 치명적 오류로 다룬다."""


@dataclass(frozen=True)
class SlotState:
    status: SlotStatus
    remaining: int | None
    last_checked_at: datetime | None
    last_changed_at: datetime | None
    last_notified_remaining: int | None
    consecutive_errors: int
    last_error: str | None


@dataclass
class State:
    """메모리에서 갱신하다가 종료 직전에 한 번 저장한다."""

    slots: dict[str, SlotState] = field(default_factory=dict)
    heartbeat_last_sent: date | None = None


def slot_key(monitor_id: str, target_date: date, target_time: time) -> str:
    """`{monitor_id}:{date}:{HH:MM}` 형식의 독립 상태 키."""
    return f"{monitor_id}:{target_date.isoformat()}:{target_time.strftime('%H:%M')}"


def record_success(
    previous: SlotState | None, *, remaining: int, threshold: int, checked_at: datetime
) -> SlotState:
    """정상 조회 결과를 반영한다. 누적 오류는 초기화된다."""
    status: SlotStatus = "available" if remaining >= threshold else "sold_out"
    last_changed_at: datetime | None = checked_at
    if previous is not None and previous.status == status and previous.remaining == remaining:
        last_changed_at = previous.last_changed_at
    return SlotState(
        status=status,
        remaining=remaining,
        last_checked_at=checked_at,
        last_changed_at=last_changed_at,
        last_notified_remaining=previous.last_notified_remaining if previous else None,
        consecutive_errors=0,
        last_error=None,
    )


def record_error(previous: SlotState | None, error: str, failed_at: datetime) -> SlotState:
    """조회 실패를 기록한다. 이전 정상 상태는 그대로 유지한다."""
    if previous is None:
        return SlotState(
            status="unknown",
            remaining=None,
            last_checked_at=None,
            last_changed_at=failed_at,
            last_notified_remaining=None,
            consecutive_errors=1,
            last_error=error,
        )
    return replace(
        previous,
        consecutive_errors=previous.consecutive_errors + 1,
        last_error=error,
    )


@dataclass(frozen=True)
class EvaluationResult:
    """새 상태와 알림 여부. 순수 함수의 출력이므로 전송은 하지 않는다."""

    state: SlotState
    should_notify: bool
    reason: NotificationReason | None


def evaluate_slot(
    previous: SlotState | None,
    *,
    remaining: int,
    threshold: int,
    notify_on_initial_available: bool,
    notify_on_increase: bool,
    checked_at: datetime,
) -> EvaluationResult:
    """정상 조회 결과로 상태 전이와 알림 여부를 판단한다. 순수 함수."""
    state = record_success(
        previous, remaining=remaining, threshold=threshold, checked_at=checked_at
    )
    reason = _notification_reason(
        previous, state, notify_on_initial_available, notify_on_increase
    )
    if reason is None and previous is None and state.status == "available":
        # 설정으로 첫 알림을 끈 경우다. 이 수량은 알린 것으로 간주해야
        # 다음 루프에서 '전송 실패 재시도'로 오해하지 않는다.
        state = replace(state, last_notified_remaining=remaining)
    return EvaluationResult(state=state, should_notify=reason is not None, reason=reason)


def _notification_reason(
    previous: SlotState | None,
    state: SlotState,
    notify_on_initial_available: bool,
    notify_on_increase: bool,
) -> NotificationReason | None:
    if state.status != "available":
        return None
    if previous is None:
        return "initial_available" if notify_on_initial_available else None
    if previous.status != "available":
        return "became_available"
    if previous.last_notified_remaining is None:
        # 이전에 알려야 했는데 전송이 확인되지 않았다. 다시 알린다.
        return "became_available"
    if notify_on_increase and state.remaining is not None:
        if state.remaining > previous.last_notified_remaining:
            return "remaining_increased"
    return None


def evaluate_error(
    previous: SlotState | None, error: str, *, error_alert_threshold: int, failed_at: datetime
) -> EvaluationResult:
    """조회 실패를 기록한다. 연속 오류가 임계값에 도달한 그 회차에만 운영 알림을 낸다."""
    state = record_error(previous, error, failed_at)
    reached = state.consecutive_errors == error_alert_threshold
    return EvaluationResult(
        state=state,
        should_notify=reached,
        reason="error_threshold_reached" if reached else None,
    )


def mark_notified(slot: SlotState) -> SlotState:
    """알림 전송에 성공한 뒤에만 호출한다."""
    return replace(slot, last_notified_remaining=slot.remaining)


def load_state(path: Path) -> State:
    """상태 파일을 읽는다. 파일이 없으면 빈 상태로 시작한다."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    except OSError as exc:
        raise StateError(f"상태 파일을 읽을 수 없다: {path} ({exc.strerror})") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(f"상태 파일이 손상됐다: {path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise StateError(f"상태 최상위가 객체가 아니다: {path}")
    if raw.get("version") != STATE_VERSION:
        raise StateError(f"지원하지 않는 상태 version: {raw.get('version')!r}")

    slots_raw = raw.get("slots", {})
    if not isinstance(slots_raw, dict):
        raise StateError("slots가 객체가 아니다")
    heartbeat = raw.get("heartbeat") or {}
    if not isinstance(heartbeat, dict):
        raise StateError("heartbeat가 객체가 아니다")

    return State(
        slots={key: _parse_slot_state(key, value) for key, value in slots_raw.items()},
        heartbeat_last_sent=_parse_date(heartbeat.get("last_sent_date")),
    )


def save_state(path: Path, state: State, updated_at: datetime) -> bool:
    """임시 파일에 쓴 뒤 `os.replace`로 원자적으로 교체한다.

    실질 상태가 그대로면 쓰지 않고 `False`를 돌려준다. `updated_at`과
    `last_checked_at`은 조회마다 바뀌므로, 그대로 저장하면 파일이 늘 달라
    보여서 Actions가 변화 없는 커밋을 계속 쌓는다.
    """
    payload = {
        "version": STATE_VERSION,
        "updated_at": updated_at.isoformat(),
        "slots": {key: _dump_slot_state(slot) for key, slot in sorted(state.slots.items())},
        "heartbeat": {
            "last_sent_date": (
                state.heartbeat_last_sent.isoformat() if state.heartbeat_last_sent else None
            )
        },
    }
    if _material(payload) == _material(_read_existing(path)):
        return False

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name, suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise StateError(f"상태 파일을 쓸 수 없다: {path} ({exc})") from exc
    return True


#: 조회마다 바뀌어서 "변화 있음" 판단에 넣으면 안 되는 필드.
_VOLATILE_SLOT_FIELDS = frozenset({"last_checked_at"})


def _material(payload: Any) -> Any:
    """변화 판단에 쓸 부분만 남긴다. 읽을 수 없는 파일은 `None`이 되어 항상 다르다."""
    if not isinstance(payload, dict):
        return None
    raw_slots = payload.get("slots")
    slots: dict[str, Any] = {}
    if isinstance(raw_slots, dict):
        for key, slot in raw_slots.items():
            if isinstance(slot, dict):
                slots[key] = {k: v for k, v in slot.items() if k not in _VOLATILE_SLOT_FIELDS}
            else:
                slots[key] = slot
    return {"slots": slots, "heartbeat": payload.get("heartbeat")}


def _read_existing(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _dump_slot_state(slot: SlotState) -> dict[str, Any]:
    dumped = asdict(slot)
    for key in ("last_checked_at", "last_changed_at"):
        value = dumped[key]
        dumped[key] = value.isoformat() if value is not None else None
    return dumped


def _parse_slot_state(key: str, raw: Any) -> SlotState:
    if not isinstance(raw, dict):
        raise StateError(f"슬롯 {key}가 객체가 아니다")
    raw_status = raw.get("status")
    if not isinstance(raw_status, str) or raw_status not in _VALID_STATUSES:
        raise StateError(f"슬롯 {key}의 status를 모른다: {raw_status!r}")
    return SlotState(
        status=cast(SlotStatus, raw_status),
        remaining=_parse_optional_int(key, raw, "remaining"),
        last_checked_at=_parse_datetime(key, raw.get("last_checked_at")),
        last_changed_at=_parse_datetime(key, raw.get("last_changed_at")),
        last_notified_remaining=_parse_optional_int(key, raw, "last_notified_remaining"),
        consecutive_errors=_parse_optional_int(key, raw, "consecutive_errors") or 0,
        last_error=_parse_optional_str(key, raw, "last_error"),
    )


def _parse_optional_int(key: str, raw: dict[str, Any], name: str) -> int | None:
    value: object = raw.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise StateError(f"슬롯 {key}의 {name}이 정수가 아니다: {value!r}")
    return value


def _parse_optional_str(key: str, raw: dict[str, Any], name: str) -> str | None:
    value = raw.get(name)
    if value is None or isinstance(value, str):
        return value
    raise StateError(f"슬롯 {key}의 {name}이 문자열이 아니다: {value!r}")


def _parse_datetime(key: str, value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"슬롯 {key}의 시각이 문자열이 아니다: {value!r}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise StateError(f"슬롯 {key}의 시각 형식이 잘못됐다: {value!r}") from exc


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"heartbeat.last_sent_date가 문자열이 아니다: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise StateError(f"heartbeat.last_sent_date 형식이 잘못됐다: {value!r}") from exc
