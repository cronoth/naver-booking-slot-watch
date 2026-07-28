"""설정 자료형.

docs/IMPLEMENTATION_PLAN.md 단계 2의 권장 자료형을 따른다.
"""

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class BookingIdentifiers:
    """네이버 예약 URL에서 뽑아낸 상품 식별자."""

    business_type_id: int
    business_id: str
    biz_item_id: str


@dataclass(frozen=True)
class SlotTarget:
    """한 날짜에서 감시할 시간 회차 목록."""

    date: date
    times: tuple[time, ...]


@dataclass(frozen=True)
class MonitorConfig:
    id: str
    name: str
    enabled: bool
    url: str
    identifiers: BookingIdentifiers
    targets: tuple[SlotTarget, ...]
    threshold: int
    notify_on_initial_available: bool
    notify_on_increase: bool
    expires_at: datetime | None


@dataclass(frozen=True)
class Config:
    monitors: tuple[MonitorConfig, ...]
    error_alert_threshold: int


#: 어느 모니터의 어느 시간 회차인지 나타내는 쌍.
SlotRequest = tuple[MonitorConfig, time]


@dataclass(frozen=True)
class ScheduleRequest:
    """GraphQL 한 번으로 처리할 상품·날짜 묶음."""

    identifiers: BookingIdentifiers
    date: date
    slots: tuple[SlotRequest, ...]
