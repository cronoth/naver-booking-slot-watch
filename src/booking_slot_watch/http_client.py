"""HTTP 조회 공통 정책: 실패 분류, 재시도·백오프, Session 교체.

네이버 GraphQL 조회와 야놀자 HTML 조회가 같은 정책을 쓴다.

조회 실패를 `0석`으로 바꾸지 않는 것이 이 모듈의 핵심 계약이다.
실패는 반드시 `FetchError`로 나가고, 상태 판단은 호출자가 한다.
"""

import logging
import time as time_module
from collections.abc import Callable
from typing import Any, ClassVar

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SEC = 15.0
#: 같은 조회에서 최대 2회 재시도하며 그 사이 대기 시간.
RETRY_DELAYS_SEC = (2.0, 5.0)
RATE_LIMITED_STATUSES = frozenset({403, 429})
#: 403/429가 이어질 때 대기 배수의 상한.
#:
#: 이 sleep은 종료 신호로 끊을 수 없다. 상한이 없으면 5시간 30분 job에서 연속
#: 실패가 쌓여 한 번의 조회가 수십 분 잠들고, 작업이 취소될 때 유예 시간을
#: 넘겨 종료 직전 상태 저장을 잃는다.
MAX_RATE_LIMIT_BACKOFF = 6


class FetchError(Exception):
    """조회 실패. `kind`로 실패 종류를 구분해 로그와 백오프에 쓴다."""

    def __init__(self, message: str, *, kind: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class RetryingClient:
    """재시도·백오프·Session 교체를 담당하는 베이스. 연결은 하나의 Session으로 재사용한다.

    서브클래스는 `error_class`로 자기 예외형을, `session_label`로 로그에 쓸 이름을 정하고,
    `_send()`로 요청을 보낸 뒤 `_run_with_retry()`로 한 번의 조회를 감싼다.
    """

    error_class: ClassVar[type[FetchError]] = FetchError
    session_label: ClassVar[str] = "HTTP"

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] | None = None,
        timeout: float = REQUEST_TIMEOUT_SEC,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SEC,
    ) -> None:
        self._session_factory = session_factory
        self.session = session if session is not None else session_factory()
        self.timeout = timeout
        # 기본값을 정의 시점에 묶지 않는다. 테스트가 time.sleep을 대체할 수 있어야 한다.
        self._sleep = sleep if sleep is not None else time_module.sleep
        self._retry_delays = retry_delays
        #: 403/429가 연속된 조회 횟수. 대기 시간을 늘리는 데 쓴다.
        self._rate_limit_streak = 0
        self._session_was_reset = False

    def close(self) -> None:
        self.session.close()

    def reset_session(self) -> None:
        """연결 풀만 비우고, 다음 정규 조회부터 새 Session을 쓴다."""
        self.session.close()
        self.session = self._session_factory()
        self._session_was_reset = True
        logger.info("%s requests.Session 초기화", self.session_label)

    def _run_with_retry[T](self, attempt: Callable[[], T]) -> T:
        """한 번의 조회를 재시도 정책으로 감싼다. 마지막 실패를 그대로 올린다."""
        if self._session_was_reset:
            logger.info("초기화된 Session으로 조회 재개")
            self._session_was_reset = False
        backoff = min(1 + self._rate_limit_streak, MAX_RATE_LIMIT_BACKOFF)
        last_error: FetchError | None = None

        for index in range(len(self._retry_delays) + 1):
            try:
                result = attempt()
            except FetchError as error:
                last_error = error
                if not _is_retryable(error) or index == len(self._retry_delays):
                    break
                self._sleep(self._retry_delays[index] * backoff)
            else:
                self._rate_limit_streak = 0
                return result

        assert last_error is not None
        if last_error.kind == "rate_limited":
            self._rate_limit_streak += 1
        raise last_error

    def _send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """요청을 보내고 전송·상태 코드 실패를 `error_class`로 바꾼다.

        `session.request()`가 아니라 메서드 이름으로 호출한다. 테스트 대역이 `post()`만
        구현하는 경우가 있고, 어느 메서드를 썼는지 호출 기록에 그대로 남는다.
        """
        try:
            response: requests.Response = getattr(self.session, method)(
                url, timeout=self.timeout, **kwargs
            )
        except requests.Timeout as exc:
            raise self.error_class(f"요청 시간 초과: {exc}", kind="timeout") from exc
        except requests.RequestException as exc:
            raise self.error_class(f"네트워크 오류: {exc}", kind="network_error") from exc

        if response.status_code in RATE_LIMITED_STATUSES:
            raise self.error_class(
                f"요청이 차단됐다: HTTP {response.status_code}",
                kind="rate_limited",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise self.error_class(
                f"HTTP {response.status_code}", kind="http_error", status=response.status_code
            )
        return response


def _is_retryable(error: FetchError) -> bool:
    if error.kind in ("timeout", "network_error", "rate_limited"):
        return True
    return error.kind == "http_error" and error.status is not None and error.status >= 500
