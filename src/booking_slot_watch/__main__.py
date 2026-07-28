"""CLI 진입점.

exit code 규약은 docs/IMPLEMENTATION_PLAN.md 3절과 같다.
"""

import argparse
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import FrameType
from zoneinfo import ZoneInfo

from .config import ConfigError, active_monitors, load_config
from .models import Config
from .monitor import check_once, loop_settings_from_env, run_loop
from .naver import NaverBookingClient
from .notifier import Notifier, NotifierConfigError, ntfy_config_from_env
from .state import StateError, load_state, save_state

KST = ZoneInfo("Asia/Seoul")

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_NO_ACTIVE_TARGETS = 3
EXIT_ALL_CHECKS_FAILED = 4

DEFAULT_CONFIG_PATH = Path("monitors.json")
DEFAULT_STATE_PATH = Path("state/availability.json")

COMMANDS: dict[str, str] = {
    "validate-config": "monitors.json 검증",
    "check-once": "활성 대상을 한 번 조회하고 필요하면 알림",
    "monitor": "장시간 반복 감시 루프",
    "has-active-targets": "활성 대상 존재 여부를 exit code로 반환",
    "send-test-notification": "ntfy 테스트 알림 전송",
}

logger = logging.getLogger("booking_slot_watch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="booking-slot-watch",
        description="네이버 예약 회차 잔여석 감시 및 ntfy 알림",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="설정 파일 경로")
        sub.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="상태 파일 경로")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging()
    try:
        return _dispatch(args, os.environ)
    except ConfigError as exc:
        logger.error("설정 오류: %s", exc)
        return EXIT_CONFIG_ERROR
    except NotifierConfigError as exc:
        logger.error("알림 설정 오류: %s", exc)
        return EXIT_CONFIG_ERROR
    except StateError as exc:
        logger.error("상태 파일 오류: %s", exc)
        return EXIT_RUNTIME_ERROR


def _dispatch(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.command == "validate-config":
        return _validate_config(args.config)
    if args.command == "has-active-targets":
        return _has_active_targets(args.config)
    if args.command == "send-test-notification":
        return _send_test_notification(env)
    if args.command == "check-once":
        return _check_once(args.config, args.state, env)
    if args.command == "monitor":
        return _monitor(args.config, args.state, env)
    logger.error("%s: 아직 구현되지 않았습니다", args.command)
    return EXIT_RUNTIME_ERROR


def _validate_config(config_path: Path) -> int:
    config = load_config(config_path)
    active = active_monitors(config, _now())
    logger.info(
        "설정 정상: monitors=%d active=%d slots=%d",
        len(config.monitors),
        len(active),
        _count_slots(config),
    )
    return EXIT_OK


def _has_active_targets(config_path: Path) -> int:
    config = load_config(config_path)
    active = active_monitors(config, _now())
    slots = sum(len(target.times) for monitor in active for target in monitor.targets)
    if not slots:
        logger.info("활성 대상 없음")
        return EXIT_NO_ACTIVE_TARGETS
    logger.info("활성 대상 있음: monitors=%d slots=%d", len(active), slots)
    return EXIT_OK


def _send_test_notification(env: Mapping[str, str]) -> int:
    notifier = Notifier(ntfy_config_from_env(env))
    try:
        if not notifier.send_test():
            logger.error("테스트 알림 전송 실패")
            return EXIT_RUNTIME_ERROR
    finally:
        notifier.close()
    logger.info("테스트 알림 전송 성공")
    return EXIT_OK


def _check_once(config_path: Path, state_path: Path, env: Mapping[str, str]) -> int:
    config = load_config(config_path)
    notifier = Notifier(ntfy_config_from_env(env))
    state = load_state(state_path)
    now = _now()

    client = NaverBookingClient()
    try:
        outcome = check_once(config, state, client=client, notifier=notifier, now=now)
    finally:
        client.close()
        notifier.close()

    save_state(state_path, state, now)

    if outcome.slots_active == 0:
        logger.info("활성 대상 없음")
        return EXIT_NO_ACTIVE_TARGETS
    logger.info(
        "조회 완료: slots=%d failed=%d notified=%d",
        outcome.slots_checked,
        outcome.slots_failed,
        outcome.notifications_sent,
    )
    if outcome.slots_failed == outcome.slots_checked:
        logger.error("모든 조회 실패")
        return EXIT_ALL_CHECKS_FAILED
    return EXIT_OK


def _monitor(config_path: Path, state_path: Path, env: Mapping[str, str]) -> int:
    config = load_config(config_path)
    settings = loop_settings_from_env(env)
    notifier = Notifier(ntfy_config_from_env(env))
    state = load_state(state_path)
    should_stop = _install_stop_handlers()

    client = NaverBookingClient()
    try:
        result = run_loop(
            config,
            state,
            client=client,
            notifier=notifier,
            state_path=state_path,
            settings=settings,
            should_stop=should_stop,
        )
    finally:
        client.close()
        notifier.close()

    if result.stopped_reason == "no_active_targets":
        return EXIT_NO_ACTIVE_TARGETS
    return EXIT_OK


def _install_stop_handlers() -> Callable[[], bool]:
    """SIGTERM/SIGINT를 받으면 루프가 정리 후 끝낼 수 있게 표시만 남긴다."""
    stopped = False

    def handler(signum: int, frame: FrameType | None) -> None:
        nonlocal stopped
        logger.info("종료 신호 수신: %s", signal.Signals(signum).name)
        stopped = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)
    return lambda: stopped


def _count_slots(config: Config) -> int:
    return sum(len(target.times) for monitor in config.monitors for target in monitor.targets)


def _now() -> datetime:
    return datetime.now(KST)


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
