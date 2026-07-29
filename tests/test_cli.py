import json
import time
from pathlib import Path
from typing import Any

import pytest
import responses

from booking_slot_watch.__main__ import (
    COMMANDS,
    EXIT_ALL_CHECKS_FAILED,
    EXIT_CONFIG_ERROR,
    EXIT_NO_ACTIVE_TARGETS,
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    build_parser,
    main,
)
from booking_slot_watch.naver import GRAPHQL_URL
from booking_slot_watch.notifier import DEFAULT_SERVER_URL

TOPIC = "cli-test-topic"
NTFY_URL = f"{DEFAULT_SERVER_URL}/{TOPIC}"
URL = "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183"


def write_config(tmp_path: Path, **overrides: Any) -> Path:
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
    return path


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


def cli(tmp_path: Path, command: str, config: Path) -> int:
    return main([command, "--config", str(config), "--state", str(tmp_path / "state.json")])


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI는 실제 재시도 대기(2초·5초)를 타므로 테스트에서는 없앤다."""
    monkeypatch.setattr(time, "sleep", lambda _: None)


@pytest.fixture(autouse=True)
def _ntfy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.delenv("NTFY_SERVER_URL", raising=False)
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    monkeypatch.delenv("NTFY_HEARTBEAT_TOPIC", raising=False)


# --- 파서 ----------------------------------------------------------------


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


def test_missing_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([])
    assert exc.value.code == 2


def test_default_paths_match_the_documented_layout() -> None:
    args = build_parser().parse_args(["validate-config"])
    assert args.config == Path("monitors.json")
    assert args.state == Path("state/availability.json")


# --- validate-config ------------------------------------------------------


def test_validate_config_accepts_a_good_file(tmp_path: Path) -> None:
    assert cli(tmp_path, "validate-config", write_config(tmp_path)) == EXIT_OK


def test_validate_config_rejects_a_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "monitors.json"
    bad.write_text(json.dumps({"version": 2, "monitors": []}), encoding="utf-8")
    assert cli(tmp_path, "validate-config", bad) == EXIT_CONFIG_ERROR


def test_validate_config_reports_a_missing_file(tmp_path: Path) -> None:
    assert cli(tmp_path, "validate-config", tmp_path / "nope.json") == EXIT_CONFIG_ERROR


def test_validate_config_does_not_need_ntfy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert cli(tmp_path, "validate-config", write_config(tmp_path)) == EXIT_OK


# --- has-active-targets ---------------------------------------------------


def test_has_active_targets_succeeds_when_something_is_active(tmp_path: Path) -> None:
    assert cli(tmp_path, "has-active-targets", write_config(tmp_path)) == EXIT_OK


def test_has_active_targets_signals_none_left_for_expired(tmp_path: Path) -> None:
    config = write_config(tmp_path, expires_at="2020-01-01T00:00:00+09:00")
    assert cli(tmp_path, "has-active-targets", config) == EXIT_NO_ACTIVE_TARGETS


def test_has_active_targets_signals_none_left_for_disabled(tmp_path: Path) -> None:
    config = write_config(tmp_path, enabled=False, targets=[])
    assert cli(tmp_path, "has-active-targets", config) == EXIT_NO_ACTIVE_TARGETS


def test_has_active_targets_reports_config_errors_separately(tmp_path: Path) -> None:
    bad = tmp_path / "monitors.json"
    bad.write_text("{ broken", encoding="utf-8")
    assert cli(tmp_path, "has-active-targets", bad) == EXIT_CONFIG_ERROR


# --- check-once -----------------------------------------------------------


@responses.activate
def test_check_once_succeeds_and_writes_state(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json=payload(0))
    responses.add(responses.POST, NTFY_URL, json={})

    assert cli(tmp_path, "check-once", write_config(tmp_path)) == EXIT_OK

    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["slots"]["event:2026-08-29:14:30"]["status"] == "sold_out"


@responses.activate
def test_check_once_reports_when_every_check_failed(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, NTFY_URL, json={})

    assert cli(tmp_path, "check-once", write_config(tmp_path)) == EXIT_ALL_CHECKS_FAILED


@responses.activate
def test_check_once_still_saves_state_when_checks_fail(tmp_path: Path) -> None:
    responses.add(responses.POST, GRAPHQL_URL, json={}, status=500)
    responses.add(responses.POST, NTFY_URL, json={})

    cli(tmp_path, "check-once", write_config(tmp_path))

    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["slots"]["event:2026-08-29:14:30"]["consecutive_errors"] == 1


def test_check_once_signals_no_active_targets(tmp_path: Path) -> None:
    config = write_config(tmp_path, expires_at="2020-01-01T00:00:00+09:00")
    assert cli(tmp_path, "check-once", config) == EXIT_NO_ACTIVE_TARGETS


def test_check_once_requires_ntfy_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert cli(tmp_path, "check-once", write_config(tmp_path)) == EXIT_CONFIG_ERROR


@responses.activate
def test_check_once_resumes_from_the_saved_state(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    responses.add(responses.POST, GRAPHQL_URL, json=payload(1))
    responses.add(responses.POST, NTFY_URL, json={})
    cli(tmp_path, "check-once", config)

    responses.reset()
    responses.add(responses.POST, GRAPHQL_URL, json=payload(1))
    cli(tmp_path, "check-once", config)

    # 두 번째 실행은 같은 수량이므로 알림을 다시 보내지 않는다.
    assert not [c for c in responses.calls if c.request.url.startswith(DEFAULT_SERVER_URL)]


def test_check_once_fails_on_a_corrupt_state_file(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{ broken", encoding="utf-8")
    assert cli(tmp_path, "check-once", write_config(tmp_path)) == EXIT_RUNTIME_ERROR


# --- send-test-notification ----------------------------------------------


@responses.activate
def test_send_test_notification_succeeds(tmp_path: Path) -> None:
    responses.add(responses.POST, NTFY_URL, json={})
    assert cli(tmp_path, "send-test-notification", write_config(tmp_path)) == EXIT_OK
    assert len(responses.calls) == 1


@responses.activate
def test_send_test_notification_fails_on_rejection(tmp_path: Path) -> None:
    responses.add(responses.POST, NTFY_URL, json={}, status=403)
    assert cli(tmp_path, "send-test-notification", write_config(tmp_path)) == EXIT_RUNTIME_ERROR


# --- send-ops-alert -------------------------------------------------------
#
# 워크플로 셸이 사용자에게 알릴 통로. 상태 푸시가 최종 실패하면 워크플로 경고만
# 남아 아무도 모르므로, 이 명령으로 ntfy까지 보낸다.


@responses.activate
def test_send_ops_alert_sends_the_message(tmp_path: Path) -> None:
    responses.add(responses.POST, NTFY_URL, json={})

    code = main(
        ["send-ops-alert", "--message", "상태 푸시 실패 — 중복 알림 가능",
         "--config", str(write_config(tmp_path)), "--state", str(tmp_path / "state.json")]
    )

    assert code == EXIT_OK
    body = (responses.calls[0].request.body or b"").decode("utf-8")
    assert body == "상태 푸시 실패 — 중복 알림 가능"


@responses.activate
def test_send_ops_alert_fails_when_ntfy_rejects(tmp_path: Path) -> None:
    responses.add(responses.POST, NTFY_URL, json={}, status=403)
    code = main(["send-ops-alert", "--message", "x", "--config", str(write_config(tmp_path))])
    assert code == EXIT_RUNTIME_ERROR


def test_send_ops_alert_needs_a_topic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    code = main(["send-ops-alert", "--message", "x", "--config", str(write_config(tmp_path))])
    assert code == EXIT_CONFIG_ERROR


def test_send_ops_alert_requires_a_message() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["send-ops-alert"])
    assert exc.value.code == 2


def test_message_option_is_only_on_the_ops_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["check-once", "--message", "x"])


def test_send_test_notification_needs_a_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert cli(tmp_path, "send-test-notification", write_config(tmp_path)) == EXIT_CONFIG_ERROR
