"""ntfy 알림 전송.

전송 실패는 예외로 올리지 않고 `False`를 돌려준다. 조회 상태 처리와 분리하기
위한 것이며, 호출자는 전송이 성공한 뒤에만 `state.mark_notified()`를 쓴다.

로그에 토픽과 토큰을 절대 남기지 않는다. 발행 URL에 토픽이 들어 있으므로
실패 로그에 URL을 넣어서도 안 된다.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from email.header import Header

import requests

from .state import SlotStatus

logger = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "https://ntfy.sh"
REQUEST_TIMEOUT_SEC = 10.0

#: ntfy 우선순위 숫자. 4 = high, 3 = default, 2 = low.
PRIORITY_AVAILABLE = "4"
PRIORITY_ERROR = "3"
PRIORITY_HEARTBEAT = "2"

TAGS_AVAILABLE = "bell,calendar"
TAGS_ERROR = "warning"
TAGS_HEARTBEAT = "heartbeat"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

_STATUS_LABELS: dict[SlotStatus, str] = {
    "unknown": "확인 불가",
    "sold_out": "매진",
    "available": "예약 가능",
}


class NotifierConfigError(Exception):
    """ntfy 설정이 없거나 잘못됐다."""


@dataclass(frozen=True)
class NtfyConfig:
    topic: str
    server_url: str = DEFAULT_SERVER_URL
    token: str | None = None
    heartbeat_topic: str | None = None


def ntfy_config_from_env(env: Mapping[str, str]) -> NtfyConfig:
    """환경변수에서 설정을 읽는다. `NTFY_TOPIC`만 필수다."""
    topic = (env.get("NTFY_TOPIC") or "").strip()
    if not topic:
        raise NotifierConfigError("NTFY_TOPIC이 설정되지 않았다")
    return NtfyConfig(
        topic=topic,
        server_url=(_optional(env, "NTFY_SERVER_URL") or DEFAULT_SERVER_URL).rstrip("/"),
        token=_optional(env, "NTFY_TOKEN"),
        heartbeat_topic=_optional(env, "NTFY_HEARTBEAT_TOPIC"),
    )


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = (env.get(key) or "").strip()
    return value or None


def encode_header_value(text: str) -> str:
    """HTTP 헤더에 안전한 값으로 만든다.

    `http.client`가 헤더를 latin-1로 인코딩하므로 한글을 그대로 넣으면
    `UnicodeEncodeError`가 난다. ntfy가 문서화한 RFC 2047 인코딩을 쓴다.
    폴딩되면 `requests`가 개행을 거부하므로 한 줄로 만든다.
    """
    if text.isascii():
        return text
    return Header(text, "utf-8").encode(maxlinelen=998)


class Notifier:
    def __init__(
        self,
        config: NtfyConfig,
        session: requests.Session | None = None,
        *,
        timeout: float = REQUEST_TIMEOUT_SEC,
    ) -> None:
        self.config = config
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout

    def close(self) -> None:
        self.session.close()

    def notify_available(
        self,
        *,
        monitor_name: str,
        url: str,
        target_date: date,
        target_time: time,
        remaining: int,
        previous_status: SlotStatus | None,
        checked_at: datetime,
    ) -> bool:
        slot = f"{target_date.isoformat()} {target_time.strftime('%H:%M')}"
        message = "\n".join(
            (
                f"잔여 수량: {remaining}",
                f"이전 상태: {_status_label(previous_status)}",
                f"확인 시각: {checked_at.strftime(_TIME_FORMAT)}",
            )
        )
        return self._publish(
            topic=self.config.topic,
            title=f"[{monitor_name}] 예약 가능 — {slot}",
            message=message,
            priority=PRIORITY_AVAILABLE,
            tags=TAGS_AVAILABLE,
            click=url,
        )

    def notify_error(
        self,
        *,
        monitor_name: str,
        target_date: date,
        target_time: time,
        error: str,
        consecutive_errors: int,
        failed_at: datetime,
    ) -> bool:
        slot = f"{target_date.isoformat()} {target_time.strftime('%H:%M')}"
        message = "\n".join(
            (
                f"대상: {slot}",
                f"오류: {error}",
                f"확인 시각: {failed_at.strftime(_TIME_FORMAT)}",
                "이전 상태: 유지",
            )
        )
        return self._publish(
            topic=self.config.topic,
            title=f"[{monitor_name}] 조회 실패 — {consecutive_errors}회 연속",
            message=message,
            priority=PRIORITY_ERROR,
            tags=TAGS_ERROR,
        )

    def notify_heartbeat(
        self,
        active_monitors: int,
        active_slots: int,
        last_success_at: datetime | None,
        *,
        degraded: bool = False,
    ) -> bool:
        """일일 생존 알림.

        `degraded`면 '정상 작동 중'이라고 말하지 않는다. 전면 장애 중에 정상이라고
        알리면 감시가 멈춘 것을 아무도 모른다.
        """
        message = "\n".join(
            (
                f"활성 모니터 수: {active_monitors}",
                f"활성 회차 수: {active_slots}",
                f"최근 정상 조회: {_format_time(last_success_at)}",
            )
        )
        if degraded:
            return self._publish(
                topic=self.config.heartbeat_topic or self.config.topic,
                title="Naver Booking Slot Watch 조회 실패 중",
                message=message,
                priority=PRIORITY_ERROR,
                tags=TAGS_ERROR,
            )
        return self._publish(
            topic=self.config.heartbeat_topic or self.config.topic,
            title="Naver Booking Slot Watch 정상 작동 중",
            message=message,
            priority=PRIORITY_HEARTBEAT,
            tags=TAGS_HEARTBEAT,
        )

    def notify_outage(
        self,
        *,
        consecutive_iterations: int,
        slots: int,
        last_success_at: datetime | None,
        detected_at: datetime,
    ) -> bool:
        """모든 회차가 연속으로 실패하면 Session을 교체한 뒤 계속 감시한다."""
        message = "\n".join(
            (
                f"모든 예약 조회가 {consecutive_iterations}회 연속 실패했습니다.",
                "요청 Session을 초기화하고 감시를 계속 재시도합니다.",
                f"대상 회차: {slots}개",
                f"최근 정상 조회: {_format_time(last_success_at)}",
                f"확인 시각: {detected_at.strftime(_TIME_FORMAT)}",
            )
        )
        return self._publish(
            topic=self.config.topic,
            title="Naver Booking Slot Watch 감시 실패",
            message=message,
            priority=PRIORITY_AVAILABLE,
            tags=TAGS_ERROR,
        )

    def notify_recovery(self, *, slots: int, recovered_at: datetime) -> bool:
        """전역 감시 실패 알림을 보낸 뒤 조회가 다시 성공했음을 알린다."""
        message = "\n".join(
            (
                "예약 조회가 다시 성공했습니다.",
                "감시는 계속 정상적으로 진행됩니다.",
                f"활성 대상 회차: {slots}개",
                f"복구 확인 시각: {recovered_at.strftime(_TIME_FORMAT)}",
            )
        )
        return self._publish(
            topic=self.config.topic,
            title="Naver Booking Slot Watch 감시 복구",
            message=message,
            priority=PRIORITY_ERROR,
            tags=TAGS_ERROR,
        )

    def notify_ops(self, message: str) -> bool:
        """운영 경고. 워크플로 셸에서 사용자에게 알려야 할 때 쓴다."""
        return self._publish(
            topic=self.config.topic,
            title="Naver Booking Slot Watch 운영 경고",
            message=message,
            priority=PRIORITY_ERROR,
            tags=TAGS_ERROR,
        )

    def send_test(self) -> bool:
        return self._publish(
            topic=self.config.topic,
            title="Naver Booking Slot Watch 테스트",
            message="ntfy 설정: 정상",
            priority=PRIORITY_ERROR,
            tags=TAGS_HEARTBEAT,
        )

    def _publish(
        self,
        *,
        topic: str,
        title: str,
        message: str,
        priority: str,
        tags: str,
        click: str | None = None,
    ) -> bool:
        headers = {
            "Title": encode_header_value(title),
            "Priority": priority,
            "Tags": tags,
        }
        if click is not None:
            headers["Click"] = click
        if self.config.token is not None:
            headers["Authorization"] = f"Bearer {self.config.token}"

        try:
            response = self.session.post(
                f"{self.config.server_url}/{topic}",
                data=message.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            # 토픽이 URL에 있으므로 예외 문자열을 그대로 남기지 않는다.
            logger.warning("ntfy 전송 실패: %s", type(exc).__name__)
            return False

        if response.status_code >= 400:
            logger.warning("ntfy 전송 실패: HTTP %d", response.status_code)
            return False
        return True


def _format_time(moment: datetime | None) -> str:
    return moment.strftime(_TIME_FORMAT) if moment is not None else "없음"


def _status_label(status: SlotStatus | None) -> str:
    if status is None:
        return "첫 확인"
    return _STATUS_LABELS[status]
