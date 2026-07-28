import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from booking_slot_watch.config import (
    ConfigError,
    active_monitors,
    group_schedule_requests,
    load_config,
    parse_booking_url,
)
from booking_slot_watch.models import SlotTarget

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_URL = (
    "https://m.booking.naver.com/booking/12/bizes/472710/items/7804183"
    "?area=ple&lang=ko&startDateTime=2026-08-29T00%3A00%3A00%2B09%3A00&tab=book&theme=place"
)


def monitor_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "event-20260829",
        "name": "8월 29일 예약",
        "enabled": True,
        "url": VALID_URL,
        "targets": [{"date": "2026-08-29", "times": ["11:00", "14:00"]}],
        "expires_at": "2026-08-29T17:00:00+09:00",
    }
    base.update(overrides)
    return base


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    payload: dict[str, Any] = {"version": 1, "monitors": [monitor_dict()]}
    payload.update(overrides)
    path = tmp_path / "monitors.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# --- URL 파싱 -------------------------------------------------------------


def test_parses_identifiers_from_booking_url() -> None:
    identifiers = parse_booking_url(VALID_URL)
    assert identifiers.business_type_id == 12
    assert identifiers.business_id == "472710"
    assert identifiers.biz_item_id == "7804183"


def test_parses_booking_url_without_query_string() -> None:
    identifiers = parse_booking_url("https://m.booking.naver.com/booking/6/bizes/333/items/444")
    assert identifiers.business_type_id == 6
    assert identifiers.business_id == "333"
    assert identifiers.biz_item_id == "444"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/booking/12/bizes/472710/items/7804183",
        "https://m.booking.naver.com/booking/12/bizes/472710",
        "https://m.booking.naver.com/graphql",
        "https://m.booking.naver.com/booking/ab/bizes/472710/items/7804183",
        "not-a-url",
    ],
)
def test_rejects_url_that_is_not_a_naver_booking_item(url: str) -> None:
    with pytest.raises(ConfigError):
        parse_booking_url(url)


# --- 설정 로딩과 기본값 병합 ----------------------------------------------


def test_loads_monitor_with_builtin_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.error_alert_threshold == 3
    (monitor,) = config.monitors
    assert monitor.id == "event-20260829"
    assert monitor.threshold == 1
    assert monitor.notify_on_initial_available is True
    assert monitor.notify_on_increase is True
    assert monitor.expires_at == datetime(2026, 8, 29, 17, 0, tzinfo=KST)
    assert monitor.targets == (
        SlotTarget(date=date(2026, 8, 29), times=(time(11, 0), time(14, 0))),
    )


def test_root_defaults_override_builtin_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        defaults={
            "notify_when_remaining_at_least": 2,
            "notify_on_initial_available": False,
            "notify_on_increase": False,
            "error_alert_threshold": 5,
        },
    )
    config = load_config(path)
    assert config.error_alert_threshold == 5
    (monitor,) = config.monitors
    assert monitor.threshold == 2
    assert monitor.notify_on_initial_available is False
    assert monitor.notify_on_increase is False


def test_monitor_fields_override_root_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        defaults={"notify_when_remaining_at_least": 2, "notify_on_increase": False},
        monitors=[monitor_dict(notify_when_remaining_at_least=4, notify_on_increase=True)],
    )
    (monitor,) = load_config(path).monitors
    assert monitor.threshold == 4
    assert monitor.notify_on_increase is True


def test_multiple_monitors_are_loaded_independently(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[
            monitor_dict(),
            monitor_dict(
                id="restaurant-20260905",
                name="식당 예약",
                url="https://m.booking.naver.com/booking/6/bizes/333/items/444",
                targets=[
                    {"date": "2026-09-05", "times": ["18:00", "19:00"]},
                    {"date": "2026-09-06", "times": ["18:00"]},
                ],
                expires_at="2026-09-06T18:00:00+09:00",
            ),
        ],
    )
    first, second = load_config(path).monitors
    assert first.identifiers.biz_item_id == "7804183"
    assert second.identifiers.biz_item_id == "444"
    assert len(second.targets) == 2


# --- 유효성 검증 ----------------------------------------------------------


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")


def test_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "monitors.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_unsupported_version(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, version=2))


def test_rejects_duplicate_monitor_id(tmp_path: Path) -> None:
    path = write_config(tmp_path, monitors=[monitor_dict(), monitor_dict()])
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("bad_id", ["", "   "])
def test_rejects_blank_monitor_id(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, monitors=[monitor_dict(id=bad_id)]))


@pytest.mark.parametrize("bad_time", ["11:0", "25:00", "1100", "11:00:00", ""])
def test_rejects_invalid_time_format(tmp_path: Path, bad_time: str) -> None:
    path = write_config(
        tmp_path,
        monitors=[monitor_dict(targets=[{"date": "2026-08-29", "times": [bad_time]}])],
    )
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "bad_date",
    ["2026-13-01", "20260829", "2026/08/29", "not-a-date", "2026-8-29"],
)
def test_rejects_invalid_date_format(tmp_path: Path, bad_date: str) -> None:
    path = write_config(
        tmp_path,
        monitors=[monitor_dict(targets=[{"date": bad_date, "times": ["11:00"]}])],
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_expires_at_without_timezone_offset(tmp_path: Path) -> None:
    path = write_config(tmp_path, monitors=[monitor_dict(expires_at="2026-08-29T17:00:00")])
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("threshold", [0, -1])
def test_rejects_threshold_below_one(tmp_path: Path, threshold: int) -> None:
    path = write_config(
        tmp_path,
        monitors=[monitor_dict(notify_when_remaining_at_least=threshold)],
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_enabled_monitor_without_any_slot(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, monitors=[monitor_dict(targets=[])]))


def test_rejects_enabled_monitor_whose_target_has_no_times(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[monitor_dict(targets=[{"date": "2026-08-29", "times": []}])],
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_allows_disabled_monitor_without_targets(tmp_path: Path) -> None:
    path = write_config(tmp_path, monitors=[monitor_dict(enabled=False, targets=[])])
    (monitor,) = load_config(path).monitors
    assert monitor.enabled is False
    assert monitor.targets == ()


def test_rejects_duplicate_time_within_one_target(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[monitor_dict(targets=[{"date": "2026-08-29", "times": ["11:00", "11:00"]}])],
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_duplicate_date_within_one_monitor(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[
            monitor_dict(
                targets=[
                    {"date": "2026-08-29", "times": ["11:00"]},
                    {"date": "2026-08-29", "times": ["14:00"]},
                ]
            )
        ],
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_rejects_non_ascii_url(tmp_path: Path) -> None:
    """HTTP 헤더는 latin-1로 인코딩되므로 non-ASCII URL은 ntfy Click에서 터진다."""
    bad = "https://m.booking.naver.com/booking/12/bizes/1/items/2?theme=플레이스"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, monitors=[monitor_dict(url=bad)]))


# --- 알 수 없는 키 거부 ----------------------------------------------------
#
# monitors.json은 사람이 GitHub 웹 편집기로 고치는 주 인터페이스다. 오타를
# 조용히 무시하면 expires_at → expire_at 하나로 만료가 사라져 영구히 돌아간다.


def test_rejects_unknown_root_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="monitorss"):
        load_config(write_config(tmp_path, monitorss=[]))


def test_rejects_unknown_defaults_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="notify_on_increse"):
        load_config(write_config(tmp_path, defaults={"notify_on_increse": True}))


@pytest.mark.parametrize(
    "bad_key",
    ["expire_at", "notify_on_increse", "notify_when_remaining_atleast", "enable", "junk"],
)
def test_rejects_unknown_monitor_key(tmp_path: Path, bad_key: str) -> None:
    entry = monitor_dict(**{bad_key: "값"})
    with pytest.raises(ConfigError, match=bad_key):
        load_config(write_config(tmp_path, monitors=[entry]))


def test_typo_in_expires_at_does_not_silently_disable_expiry(tmp_path: Path) -> None:
    entry = monitor_dict()
    entry["expire_at"] = entry.pop("expires_at")
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, monitors=[entry]))


def test_rejects_unknown_target_key(tmp_path: Path) -> None:
    entry = monitor_dict(
        targets=[{"date": "2026-08-29", "times": ["11:00"], "time": ["12:00"]}]
    )
    with pytest.raises(ConfigError, match="time"):
        load_config(write_config(tmp_path, monitors=[entry]))


def test_accepts_every_documented_monitor_key(tmp_path: Path) -> None:
    entry = monitor_dict(
        notify_when_remaining_at_least=2,
        notify_on_initial_available=False,
        notify_on_increase=False,
    )
    assert load_config(write_config(tmp_path, monitors=[entry])).monitors


def test_rejects_missing_required_monitor_field(tmp_path: Path) -> None:
    incomplete = monitor_dict()
    del incomplete["url"]
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, monitors=[incomplete]))


def test_config_error_message_names_the_monitor(tmp_path: Path) -> None:
    path = write_config(tmp_path, monitors=[monitor_dict(notify_when_remaining_at_least=0)])
    with pytest.raises(ConfigError, match="event-20260829"):
        load_config(path)


# --- 활성 대상 계산 -------------------------------------------------------


def test_active_monitors_skips_disabled(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, monitors=[monitor_dict(enabled=False)]))
    assert active_monitors(config, datetime(2026, 7, 28, 14, 0, tzinfo=KST)) == ()


def test_active_monitors_skips_expired(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert active_monitors(config, datetime(2026, 8, 29, 17, 0, 1, tzinfo=KST)) == ()


def test_active_monitors_keeps_monitor_before_expiry(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    active = active_monitors(config, datetime(2026, 8, 29, 16, 59, tzinfo=KST))
    assert [m.id for m in active] == ["event-20260829"]


def test_active_monitors_keeps_monitor_without_expiry(tmp_path: Path) -> None:
    """expires_at이 없으면 대상 날짜가 남아 있는 동안 계속 활성이다."""
    monitor = monitor_dict(targets=[{"date": "2099-06-01", "times": ["11:00"]}])
    del monitor["expires_at"]
    config = load_config(write_config(tmp_path, monitors=[monitor]))
    active = active_monitors(config, datetime(2099, 1, 1, tzinfo=KST))
    assert [m.id for m in active] == ["event-20260829"]


def test_active_monitors_mixes_active_and_inactive(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[
            monitor_dict(id="expired", expires_at="2026-07-01T00:00:00+09:00"),
            monitor_dict(id="alive", expires_at="2026-12-01T00:00:00+09:00"),
            monitor_dict(id="off", enabled=False),
        ],
    )
    config = load_config(path)
    active = active_monitors(config, datetime(2026, 7, 28, 14, 0, tzinfo=KST))
    assert [m.id for m in active] == ["alive"]


def test_past_target_dates_are_dropped(tmp_path: Path) -> None:
    """지난 날짜는 예약할 수 없다. 계속 조회하면 empty_schedule 오류만 쌓인다."""
    entry = monitor_dict(
        targets=[
            {"date": "2026-07-20", "times": ["11:00"]},
            {"date": "2026-08-29", "times": ["14:00"]},
        ],
    )
    config = load_config(write_config(tmp_path, monitors=[entry]))

    (monitor,) = active_monitors(config, datetime(2026, 7, 28, 14, 0, tzinfo=KST))
    assert [target.date for target in monitor.targets] == [date(2026, 8, 29)]


def test_today_is_not_treated_as_past(tmp_path: Path) -> None:
    entry = monitor_dict(targets=[{"date": "2026-07-28", "times": ["11:00"]}])
    config = load_config(write_config(tmp_path, monitors=[entry]))

    active = active_monitors(config, datetime(2026, 7, 28, 23, 0, tzinfo=KST))
    assert [t.date for m in active for t in m.targets] == [date(2026, 7, 28)]


def test_monitor_with_only_past_dates_becomes_inactive(tmp_path: Path) -> None:
    entry = monitor_dict(targets=[{"date": "2026-07-20", "times": ["11:00"]}])
    config = load_config(write_config(tmp_path, monitors=[entry]))

    assert active_monitors(config, datetime(2026, 7, 28, 14, 0, tzinfo=KST)) == ()


def test_past_date_filtering_uses_korean_time(tmp_path: Path) -> None:
    """UTC 기준으로 판단하면 한국 날짜가 바뀐 직후 하루를 잘못 버린다."""
    entry = monitor_dict(targets=[{"date": "2026-07-28", "times": ["11:00"]}])
    config = load_config(write_config(tmp_path, monitors=[entry]))

    # UTC 2026-07-27 16:00 == KST 2026-07-28 01:00. 대상 날짜는 '오늘'이다.
    utc_now = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
    assert len(active_monitors(config, utc_now)) == 1


# --- 요청 그룹화 ----------------------------------------------------------


def test_groups_same_product_and_date_into_one_request(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    (request,) = group_schedule_requests(config.monitors)
    assert request.identifiers.biz_item_id == "7804183"
    assert request.date == date(2026, 8, 29)
    assert [t for _, t in request.slots] == [time(11, 0), time(14, 0)]


def test_groups_one_request_per_date(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[
            monitor_dict(
                targets=[
                    {"date": "2026-08-29", "times": ["11:00"]},
                    {"date": "2026-08-30", "times": ["14:00", "17:00"]},
                ]
            )
        ],
    )
    requests = group_schedule_requests(load_config(path).monitors)
    assert [(r.date, len(r.slots)) for r in requests] == [
        (date(2026, 8, 29), 1),
        (date(2026, 8, 30), 2),
    ]


def test_groups_across_monitors_sharing_product_and_date(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[
            monitor_dict(id="a", targets=[{"date": "2026-08-29", "times": ["11:00"]}]),
            monitor_dict(id="b", targets=[{"date": "2026-08-29", "times": ["14:00"]}]),
        ],
    )
    (request,) = group_schedule_requests(load_config(path).monitors)
    assert [(m.id, t) for m, t in request.slots] == [("a", time(11, 0)), ("b", time(14, 0))]


def test_different_products_are_not_grouped(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        monitors=[
            monitor_dict(id="a", targets=[{"date": "2026-08-29", "times": ["11:00"]}]),
            monitor_dict(
                id="b",
                url="https://m.booking.naver.com/booking/6/bizes/333/items/444",
                targets=[{"date": "2026-08-29", "times": ["11:00"]}],
            ),
        ],
    )
    requests = group_schedule_requests(load_config(path).monitors)
    assert len(requests) == 2


def test_grouping_ignores_monitors_without_targets(tmp_path: Path) -> None:
    path = write_config(tmp_path, monitors=[monitor_dict(enabled=False, targets=[])])
    assert group_schedule_requests(load_config(path).monitors) == ()


# --- 저장소에 커밋된 설정 파일 --------------------------------------------


@pytest.mark.parametrize("filename", ["monitors.json", "monitors.example.json"])
def test_committed_config_files_are_valid(filename: str) -> None:
    config = load_config(REPO_ROOT / filename)
    assert config.monitors
