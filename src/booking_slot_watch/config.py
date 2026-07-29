"""monitors.json 읽기·검증과 활성 대상 계산.

검증 규칙은 docs/CONFIG_AND_STATE.md 3절을 따른다.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .models import (
    BookingIdentifiers,
    Config,
    MonitorConfig,
    ScheduleRequest,
    SlotRequest,
    SlotTarget,
)

CONFIG_VERSION = 1
DEFAULT_THRESHOLD = 1
DEFAULT_ERROR_ALERT_THRESHOLD = 3

_BOOKING_PATH = re.compile(r"^/booking/(\d+)/bizes/(\d+)/items/(\d+)/?$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}$")

KST = ZoneInfo("Asia/Seoul")

# 오타를 조용히 무시하면 안 된다. `expires_at`을 `expire_at`으로 잘못 쓰면
# 만료가 사라져 감시가 영구히 이어진다.
_ROOT_KEYS = frozenset({"version", "defaults", "monitors"})
_DEFAULTS_KEYS = frozenset(
    {
        "notify_when_remaining_at_least",
        "notify_on_initial_available",
        "notify_on_increase",
        "error_alert_threshold",
    }
)
_MONITOR_KEYS = frozenset(
    {
        "id",
        "name",
        "enabled",
        "url",
        "targets",
        "expires_at",
        "notify_when_remaining_at_least",
        "notify_on_initial_available",
        "notify_on_increase",
    }
)
_TARGET_KEYS = frozenset({"date", "times"})


class ConfigError(Exception):
    """설정이 유효하지 않다. 이 예외는 실행을 시작하기 전에 종료시킨다."""


def parse_booking_url(url: str) -> BookingIdentifiers:
    """예약 URL에서 businessTypeId·businessId·bizItemId를 추출한다."""
    if not url.isascii():
        # 이 URL은 ntfy Click 헤더로 나간다. HTTP 헤더는 latin-1로 인코딩되므로
        # non-ASCII가 섞이면 알림 전송이 UnicodeEncodeError로 죽는다.
        raise ConfigError(f"URL에 ASCII가 아닌 문자가 있다(퍼센트 인코딩 필요): {url!r}")
    host = urlparse(url).hostname or ""
    if host != "booking.naver.com" and not host.endswith(".booking.naver.com"):
        raise ConfigError(f"네이버 예약 URL이 아니다: {url!r}")
    match = _BOOKING_PATH.match(urlparse(url).path)
    if match is None:
        raise ConfigError(f"/booking/{{type}}/bizes/{{biz}}/items/{{item}} 경로가 아니다: {url!r}")
    business_type_id, business_id, biz_item_id = match.groups()
    return BookingIdentifiers(
        business_type_id=int(business_type_id),
        business_id=business_id,
        biz_item_id=biz_item_id,
    )


def load_config(path: Path) -> Config:
    """설정 파일을 읽고 검증한다. 오류가 하나라도 있으면 ConfigError를 던진다."""
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"설정 최상위는 객체여야 한다: {path}")
    _reject_unknown_keys(raw, _ROOT_KEYS, "설정 최상위")
    if raw.get("version") != CONFIG_VERSION:
        raise ConfigError(f"지원하지 않는 version: {raw.get('version')!r} (필요: {CONFIG_VERSION})")

    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("defaults는 객체여야 한다")
    _reject_unknown_keys(defaults, _DEFAULTS_KEYS, "defaults")

    entries = raw.get("monitors")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("monitors는 비어 있지 않은 배열이어야 한다")

    monitors = tuple(_parse_monitor(entry, defaults) for entry in entries)
    seen: set[str] = set()
    for monitor in monitors:
        if monitor.id in seen:
            raise ConfigError(f"monitor id 중복: {monitor.id}")
        seen.add(monitor.id)

    return Config(
        monitors=monitors,
        error_alert_threshold=_merged_int(
            {}, defaults, "error_alert_threshold", DEFAULT_ERROR_ALERT_THRESHOLD, minimum=1
        ),
    )


def active_monitors(config: Config, now: datetime) -> tuple[MonitorConfig, ...]:
    """지금 조회할 가치가 있는 모니터만, 지난 회차를 걷어낸 상태로 돌려준다."""
    now_kst = now.astimezone(KST)
    active: list[MonitorConfig] = []
    for monitor in config.monitors:
        if not monitor.enabled:
            continue
        if monitor.expires_at is not None and now > monitor.expires_at:
            continue
        targets = tuple(
            remaining
            for target in monitor.targets
            if (remaining := _remaining_target(target, now_kst)) is not None
        )
        if not targets:
            continue
        active.append(replace(monitor, targets=targets))
    return tuple(active)


def _remaining_target(target: SlotTarget, now: datetime) -> SlotTarget | None:
    """지난 회차를 걷어낸 대상. 남은 시각이 없으면 None.

    지난 날짜는 예약할 수 없고, 계속 조회하면 빈 응답으로 오류만 쌓인다. 오늘도
    이미 시작된 회차는 같다 — 판매 시스템이 지난 회차 재고를 이상하게 돌려주면
    참여할 수 없는 회차에 예약 가능 알림이 나간다. 유예 시간은 두지 않는다.
    """
    if target.date < now.date():
        return None
    if target.date > now.date():
        return target
    times = tuple(
        target_time
        for target_time in target.times
        if datetime.combine(target.date, target_time, tzinfo=KST) > now
    )
    return SlotTarget(date=target.date, times=times) if times else None


def group_schedule_requests(monitors: Iterable[MonitorConfig]) -> tuple[ScheduleRequest, ...]:
    """상품 + 날짜가 같은 회차를 GraphQL 호출 한 번 단위로 묶는다."""
    grouped: dict[tuple[BookingIdentifiers, date], list[SlotRequest]] = {}
    for monitor in monitors:
        for target in monitor.targets:
            slots = grouped.setdefault((monitor.identifiers, target.date), [])
            slots.extend((monitor, target_time) for target_time in target.times)
    return tuple(
        ScheduleRequest(identifiers=identifiers, date=target_date, slots=tuple(slots))
        for (identifiers, target_date), slots in grouped.items()
    )


def _reject_unknown_keys(raw: dict[str, Any], known: frozenset[str], where: str) -> None:
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(f"{where}에 알 수 없는 키가 있다: {', '.join(unknown)}")


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없다: {path} ({exc.strerror})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"설정 JSON 파싱 실패: {path} ({exc})") from exc


def _parse_monitor(entry: Any, defaults: dict[str, Any]) -> MonitorConfig:
    if not isinstance(entry, dict):
        raise ConfigError(f"monitors 항목은 객체여야 한다: {entry!r}")
    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ConfigError(f"monitor id가 비어 있다: {raw_id!r}")
    monitor_id = raw_id.strip()
    try:
        return _build_monitor(monitor_id, entry, defaults)
    except ConfigError as exc:
        raise ConfigError(f"monitor {monitor_id}: {exc}") from exc


def _build_monitor(
    monitor_id: str, entry: dict[str, Any], defaults: dict[str, Any]
) -> MonitorConfig:
    _reject_unknown_keys(entry, _MONITOR_KEYS, "monitor")
    enabled = entry.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigError(f"enabled는 true 또는 false여야 한다: {enabled!r}")
    url = _required_str(entry, "url")
    targets = _parse_targets(entry.get("targets"))
    if enabled and not targets:
        raise ConfigError("활성 모니터에는 최소 하나의 대상 회차가 필요하다")
    return MonitorConfig(
        id=monitor_id,
        name=_required_str(entry, "name"),
        enabled=enabled,
        url=url,
        identifiers=parse_booking_url(url),
        targets=targets,
        threshold=_merged_int(
            entry, defaults, "notify_when_remaining_at_least", DEFAULT_THRESHOLD, minimum=1
        ),
        notify_on_initial_available=_merged_bool(
            entry, defaults, "notify_on_initial_available", True
        ),
        notify_on_increase=_merged_bool(entry, defaults, "notify_on_increase", True),
        expires_at=_parse_expires_at(entry.get("expires_at")),
    )


def _parse_targets(raw: Any) -> tuple[SlotTarget, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"targets는 배열이어야 한다: {raw!r}")
    targets: list[SlotTarget] = []
    seen_dates: set[date] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError(f"targets 항목은 객체여야 한다: {item!r}")
        _reject_unknown_keys(item, _TARGET_KEYS, "targets 항목")
        target_date = _parse_date(item.get("date"))
        if target_date in seen_dates:
            raise ConfigError(f"date 중복: {target_date.isoformat()}")
        seen_dates.add(target_date)
        targets.append(
            SlotTarget(date=target_date, times=_parse_times(item.get("times"), target_date))
        )
    return tuple(targets)


def _parse_times(raw: Any, target_date: date) -> tuple[time, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{target_date.isoformat()}: times는 비어 있지 않은 배열이어야 한다")
    times: list[time] = []
    for value in raw:
        parsed = _parse_time(value, target_date)
        if parsed in times:
            raise ConfigError(f"{target_date.isoformat()}: time 중복 {value!r}")
        times.append(parsed)
    return tuple(times)


def _parse_date(value: Any) -> date:
    if not isinstance(value, str) or not _DATE_PATTERN.match(value):
        raise ConfigError(f"date는 YYYY-MM-DD 형식이어야 한다: {value!r}")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigError(f"date가 실제 날짜가 아니다: {value!r}") from exc


def _parse_time(value: Any, target_date: date) -> time:
    if not isinstance(value, str) or not _TIME_PATTERN.match(value):
        raise ConfigError(f"{target_date.isoformat()}: time은 HH:MM 형식이어야 한다: {value!r}")
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(f"{target_date.isoformat()}: 없는 시각 {value!r}") from exc


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"expires_at는 ISO 8601 문자열이어야 한다: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"expires_at 형식 오류: {value!r}") from exc
    if parsed.utcoffset() is None:
        raise ConfigError(f"expires_at에 timezone offset이 필요하다: {value!r}")
    return parsed


def _required_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key}가 비어 있다: {value!r}")
    return value.strip()


def _merged_int(
    entry: dict[str, Any], defaults: dict[str, Any], key: str, fallback: int, *, minimum: int
) -> int:
    value: object = entry.get(key, defaults.get(key, fallback))
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key}는 정수여야 한다: {value!r}")
    if value < minimum:
        raise ConfigError(f"{key}는 {minimum} 이상이어야 한다: {value}")
    return value


def _merged_bool(
    entry: dict[str, Any], defaults: dict[str, Any], key: str, fallback: bool
) -> bool:
    value = entry.get(key, defaults.get(key, fallback))
    if not isinstance(value, bool):
        raise ConfigError(f"{key}는 true 또는 false여야 한다: {value!r}")
    return value
