import logging
from datetime import date, datetime, time
from email.header import decode_header
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import requests
import responses

from booking_slot_watch.notifier import (
    DEFAULT_SERVER_URL,
    PRIORITY_AVAILABLE,
    PRIORITY_HEARTBEAT,
    Notifier,
    NotifierConfigError,
    NtfyConfig,
    encode_header_value,
    ntfy_config_from_env,
)

KST = ZoneInfo("Asia/Seoul")
CHECKED_AT = datetime(2026, 7, 28, 14, 30, 12, tzinfo=KST)
BOOKING_URL = "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183"

TOPIC = "s3cr3t-topic-xyz"
TOKEN = "tk_abcdef123456"
CONFIG = NtfyConfig(topic=TOPIC)


def decode_title(raw: str) -> str:
    text, charset = decode_header(raw)[0]
    return text.decode(charset) if isinstance(text, bytes) else text


def notify_available(notifier: Notifier, **overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "monitor_name": "8월 29일 예약",
        "url": BOOKING_URL,
        "target_date": date(2026, 8, 29),
        "target_time": time(14, 30),
        "remaining": 1,
        "previous_status": "sold_out",
        "checked_at": CHECKED_AT,
    }
    kwargs.update(overrides)
    return notifier.notify_available(**kwargs)


class CapturingSession:
    """post 호출 인자를 그대로 잡아두는 대역."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        response = requests.Response()
        response.status_code = 200
        return response

    def close(self) -> None:
        pass


# --- 환경변수 설정 --------------------------------------------------------


def test_topic_is_required() -> None:
    with pytest.raises(NotifierConfigError):
        ntfy_config_from_env({})


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_topic_is_rejected(blank: str) -> None:
    with pytest.raises(NotifierConfigError):
        ntfy_config_from_env({"NTFY_TOPIC": blank})


def test_server_url_defaults_to_ntfy_sh() -> None:
    config = ntfy_config_from_env({"NTFY_TOPIC": TOPIC})
    assert config.server_url == DEFAULT_SERVER_URL
    assert config.token is None
    assert config.heartbeat_topic is None


def test_optional_settings_are_read() -> None:
    config = ntfy_config_from_env(
        {
            "NTFY_TOPIC": TOPIC,
            "NTFY_SERVER_URL": "https://ntfy.example.com/",
            "NTFY_TOKEN": TOKEN,
            "NTFY_HEARTBEAT_TOPIC": "beat-topic",
        }
    )
    assert config.server_url == "https://ntfy.example.com", "끝의 슬래시를 제거해야 한다"
    assert config.token == TOKEN
    assert config.heartbeat_topic == "beat-topic"


def test_blank_optional_values_are_treated_as_unset() -> None:
    config = ntfy_config_from_env(
        {"NTFY_TOPIC": TOPIC, "NTFY_SERVER_URL": "", "NTFY_TOKEN": "  "}
    )
    assert config.server_url == DEFAULT_SERVER_URL
    assert config.token is None


# --- 예약 가능 알림 -------------------------------------------------------


@responses.activate
def test_available_notification_targets_the_topic_url() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    assert notify_available(Notifier(CONFIG)) is True
    assert responses.calls[0].request.url == f"{DEFAULT_SERVER_URL}/{TOPIC}"


@responses.activate
def test_korean_title_is_ascii_encoded_and_recoverable() -> None:
    """한글 원문을 그대로 넣으면 http.client의 latin-1 인코딩에서 죽는다."""
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    notify_available(Notifier(CONFIG))

    raw_title = responses.calls[0].request.headers["Title"]
    assert raw_title.isascii(), "헤더 값은 반드시 ASCII여야 한다"
    assert "\n" not in raw_title, "폴딩된 헤더는 requests가 거부한다"
    assert decode_title(raw_title) == "[8월 29일 예약] 예약 가능 — 2026-08-29 14:30"


@pytest.mark.parametrize("text", ["Concert A", "ntfy test", "2026-08-29 14:30"])
def test_ascii_text_is_not_encoded(text: str) -> None:
    assert encode_header_value(text) == text


def test_non_ascii_text_is_rfc2047_encoded() -> None:
    encoded = encode_header_value("예약 가능")
    assert encoded.startswith("=?utf-8?")
    assert decode_title(encoded) == "예약 가능"


@responses.activate
def test_available_notification_uses_documented_headers() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    notify_available(Notifier(CONFIG))

    headers = responses.calls[0].request.headers
    assert headers["Priority"] == "4"
    assert headers["Tags"] == "bell,calendar"
    assert headers["Click"] == BOOKING_URL


@responses.activate
def test_available_notification_body() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    notify_available(Notifier(CONFIG), remaining=2)

    body = (responses.calls[0].request.body or b"").decode("utf-8")
    assert body.splitlines() == [
        "잔여 수량: 2",
        "이전 상태: 매진",
        "확인 시각: 2026-07-28 14:30:12 KST",
    ]


@responses.activate
@pytest.mark.parametrize(
    ("previous_status", "label"),
    [(None, "첫 확인"), ("unknown", "확인 불가"), ("sold_out", "매진"), ("available", "예약 가능")],
)
def test_previous_status_is_shown_in_korean(previous_status: str | None, label: str) -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    notify_available(Notifier(CONFIG), previous_status=previous_status)

    body = (responses.calls[0].request.body or b"").decode("utf-8")
    assert f"이전 상태: {label}" in body


# --- 인증 ----------------------------------------------------------------


@responses.activate
def test_bearer_token_is_sent_when_configured() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    notify_available(Notifier(NtfyConfig(topic=TOPIC, token=TOKEN)))
    assert responses.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


@responses.activate
def test_no_authorization_header_without_token() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    notify_available(Notifier(CONFIG))
    assert "Authorization" not in responses.calls[0].request.headers


# --- 오류 알림 -----------------------------------------------------------


@responses.activate
def test_error_notification_content() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    sent = Notifier(CONFIG).notify_error(
        monitor_name="8월 29일 예약",
        target_date=date(2026, 8, 29),
        target_time=time(14, 30),
        error="rate_limited",
        consecutive_errors=3,
        failed_at=CHECKED_AT,
    )

    assert sent is True
    request = responses.calls[0].request
    assert decode_title(request.headers["Title"]) == "[8월 29일 예약] 조회 실패 — 3회 연속"
    assert request.headers["Tags"] == "warning"
    body = (request.body or b"").decode("utf-8")
    assert "대상: 2026-08-29 14:30" in body
    assert "오류: rate_limited" in body
    assert "이전 상태: 유지" in body


# --- Heartbeat -----------------------------------------------------------


@responses.activate
def test_heartbeat_uses_dedicated_topic_when_set() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/beat", body="{}")
    config = NtfyConfig(topic=TOPIC, heartbeat_topic="beat")
    assert Notifier(config).notify_heartbeat(1, 3, CHECKED_AT) is True
    assert responses.calls[0].request.url == f"{DEFAULT_SERVER_URL}/beat"


@responses.activate
def test_heartbeat_falls_back_to_the_main_topic() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    Notifier(CONFIG).notify_heartbeat(1, 3, CHECKED_AT)
    assert responses.calls[0].request.url == f"{DEFAULT_SERVER_URL}/{TOPIC}"


@responses.activate
def test_heartbeat_body_matches_spec() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    Notifier(CONFIG).notify_heartbeat(2, 5, CHECKED_AT)

    request = responses.calls[0].request
    assert decode_title(request.headers["Title"]) == "Naver Booking Slot Watch 정상 작동 중"
    assert (request.body or b"").decode("utf-8").splitlines() == [
        "활성 모니터 수: 2",
        "활성 회차 수: 5",
        "최근 정상 조회: 2026-07-28 14:30:12 KST",
    ]


@responses.activate
def test_heartbeat_without_any_successful_check() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    Notifier(CONFIG).notify_heartbeat(1, 1, None)
    assert "최근 정상 조회: 없음" in (responses.calls[0].request.body or b"").decode("utf-8")


@responses.activate
def test_heartbeat_tells_the_truth_when_every_check_is_failing() -> None:
    """전면 장애 중에 '정상 작동 중'이라고 알리면 감시 도구로서 최악이다."""
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    Notifier(CONFIG).notify_heartbeat(1, 3, CHECKED_AT, degraded=True)

    request = responses.calls[0].request
    title = decode_title(request.headers["Title"])
    body = (request.body or b"").decode("utf-8")
    assert "정상 작동 중" not in title
    assert title == "Naver Booking Slot Watch 조회 실패 중"
    assert "최근 정상 조회: 2026-07-28 14:30:12 KST" in body
    assert request.headers["Priority"] != PRIORITY_HEARTBEAT, "열화 상태는 우선순위를 올린다"


@responses.activate
def test_heartbeat_stays_normal_when_checks_succeed() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    Notifier(CONFIG).notify_heartbeat(1, 3, CHECKED_AT, degraded=False)
    title = decode_title(responses.calls[0].request.headers["Title"])
    assert title == "Naver Booking Slot Watch 정상 작동 중"


@responses.activate
def test_outage_notification_content() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    sent = Notifier(CONFIG).notify_outage(
        consecutive_iterations=5, slots=3, last_success_at=CHECKED_AT, detected_at=CHECKED_AT
    )

    assert sent is True
    request = responses.calls[0].request
    assert decode_title(request.headers["Title"]) == "Naver Booking Slot Watch 감시 중단"
    assert request.headers["Priority"] == PRIORITY_AVAILABLE, "감시가 멈춘 상태는 높은 우선순위"
    body = (request.body or b"").decode("utf-8")
    assert "연속 실패: 5회" in body
    assert "대상 회차: 3개" in body
    assert "최근 정상 조회: 2026-07-28 14:30:12 KST" in body


@responses.activate
def test_outage_notification_without_any_prior_success() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    Notifier(CONFIG).notify_outage(
        consecutive_iterations=5, slots=1, last_success_at=None, detected_at=CHECKED_AT
    )
    assert "최근 정상 조회: 없음" in (responses.calls[0].request.body or b"").decode("utf-8")


@responses.activate
def test_ops_alert_is_sent() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    assert Notifier(CONFIG).notify_ops("상태 푸시 실패 - 중복 알림 가능") is True
    request = responses.calls[0].request
    assert "운영 경고" in decode_title(request.headers["Title"])
    assert "상태 푸시 실패" in (request.body or b"").decode("utf-8")


@responses.activate
def test_test_notification_is_sent() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body="{}")
    assert Notifier(CONFIG).send_test() is True
    assert len(responses.calls) == 1


# --- 실패는 조회 상태 처리와 분리 ------------------------------------------


@responses.activate
def test_http_failure_returns_false_without_raising() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", json={}, status=500)
    assert notify_available(Notifier(CONFIG)) is False


@responses.activate
def test_network_failure_returns_false_without_raising() -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", body=requests.Timeout("t"))
    assert notify_available(Notifier(CONFIG)) is False


@responses.activate
def test_failure_log_never_leaks_topic_or_token(caplog: pytest.LogCaptureFixture) -> None:
    responses.add(responses.POST, f"{DEFAULT_SERVER_URL}/{TOPIC}", json={}, status=403)
    with caplog.at_level(logging.WARNING):
        notify_available(Notifier(NtfyConfig(topic=TOPIC, token=TOKEN)))

    assert caplog.records, "실패는 조용히 넘기지 않고 로그로 남겨야 한다"
    logged = caplog.text
    assert TOPIC not in logged
    assert TOKEN not in logged
    assert "403" in logged


# --- 요청 설정 -----------------------------------------------------------


def test_documented_timeout_is_passed_to_the_request() -> None:
    session = CapturingSession()
    notifier = Notifier(CONFIG, session=session)  # type: ignore[arg-type]
    notify_available(notifier)
    assert session.calls[0]["timeout"] == 10.0


def test_body_is_sent_as_utf8_bytes() -> None:
    session = CapturingSession()
    notify_available(Notifier(CONFIG, session=session))  # type: ignore[arg-type]
    assert isinstance(session.calls[0]["data"], bytes)
    assert "잔여 수량" in session.calls[0]["data"].decode("utf-8")
