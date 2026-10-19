"""설정 자료형.

docs/IMPLEMENTATION_PLAN.md 단계 2의 권장 자료형을 따른다.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

Provider = Literal["naver", "yanolja"]


@dataclass(frozen=True)
class BookingIdentifiers:
    """네이버 예약 URL에서 뽑아낸 상품 식별자."""

    business_type_id: int
    business_id: str
    biz_item_id: str


@dataclass(frozen=True)
class YanoljaProduct:
    """야놀자 레저 상품 URL에서 뽑아낸 상품 식별자."""

    product_id: int


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
    identifiers: BookingIdentifiers | YanoljaProduct
    #: 네이버 전용. 야놀자 모니터는 비어 있다.
    targets: tuple[SlotTarget, ...]
    threshold: int
    notify_on_initial_available: bool
    notify_on_increase: bool
    expires_at: datetime | None
    #: 야놀자 전용 옵션(variant) ID 목록. 네이버 모니터는 비어 있다.
    variants: tuple[int, ...] = ()

    @property
    def provider(self) -> Provider:
        """로그·알림 표시용. 타입 좁히기에는 `isinstance(identifiers, ...)`를 쓴다."""
        return "yanolja" if isinstance(self.identifiers, YanoljaProduct) else "naver"


@dataclass(frozen=True)
class Config:
    monitors: tuple[MonitorConfig, ...]
    error_alert_threshold: int


#: 어느 모니터의 어느 시간 회차인지 나타내는 쌍.
SlotRequest = tuple[MonitorConfig, time]


@dataclass(frozen=True)
class ScheduleRequest:
    """GraphQL 한 번으로 처리할 네이버 상품·날짜 묶음."""

    identifiers: BookingIdentifiers
    date: date
    slots: tuple[SlotRequest, ...]


#: 어느 모니터의 어느 옵션(variant)인지 나타내는 쌍.
VariantSlotRequest = tuple[MonitorConfig, int]


@dataclass(frozen=True)
class VariantRequest:
    """상품 페이지 HTML 한 번으로 처리할 야놀자 상품 묶음."""

    product: YanoljaProduct
    slots: tuple[VariantSlotRequest, ...]


FetchRequest = ScheduleRequest | VariantRequest
