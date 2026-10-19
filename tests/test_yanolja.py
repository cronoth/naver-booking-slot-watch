"""야놀자 레저 상품 페이지 파싱과 HTML 조회 클라이언트."""

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import requests
import responses

from booking_slot_watch.yanolja import (
    YanoljaApiError,
    YanoljaClient,
    parse_product_page,
    product_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
PRODUCT_ID = 10374449
PAGE_URL = product_url(PRODUCT_ID)

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def fixture_html() -> str:
    return (FIXTURES / "yanolja_product.html").read_text(encoding="utf-8")


def next_data() -> dict[str, Any]:
    """fixture 안 `__NEXT_DATA__` JSON의 깊은 복사본."""
    match = _NEXT_DATA.search(fixture_html())
    assert match is not None
    data: dict[str, Any] = json.loads(match.group(1))
    return copy.deepcopy(data)


def product_of(data: dict[str, Any]) -> dict[str, Any]:
    product: dict[str, Any] = data["props"]["pageProps"]["product"]
    return product


def render(data: dict[str, Any]) -> str:
    """fixture의 HTML 골격에 수정한 JSON을 다시 끼운다."""
    return _NEXT_DATA.sub(
        lambda _: (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(data, ensure_ascii=False)
            + "</script>"
        ),
        fixture_html(),
    )


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class CapturingSession:
    """get 호출 인자를 그대로 잡아두는 대역."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        response = requests.Response()
        response.status_code = 200
        response._content = self.body.encode("utf-8")
        response.encoding = "utf-8"
        return response

    def close(self) -> None:
        self.closed = True


# --- 페이지 파싱 (순수 함수) ------------------------------------------------


def test_parses_variants_from_real_page_shape() -> None:
    product = parse_product_page(fixture_html(), PRODUCT_ID)

    assert product.product_id == PRODUCT_ID
    assert product.status_code == "IN_SALE"
    assert sorted(product.variants) == [15375318, 15375320, 15375322, 15375323]

    premium = product.variants[15375322]
    assert premium.name == "리프트 시즌패스(1인) + V300락커(1~2인용)+프리미엄혜택"
    assert premium.status_code == "SOLD_OUT"
    assert premium.group_name == "프리미엄"
    assert premium.group_status_code == "SOLD_OUT"
    assert premium.on_sale is False
    assert product.variants[15375318].on_sale is True


@pytest.mark.parametrize("status", ["SOLD_OUT", "SALE_END", "HIDDEN", ""])
def test_only_in_sale_counts_as_on_sale(status: str) -> None:
    """수량 필드는 보지 않는다. 품절 옵션에도 18 같은 숫자가 남아 있다."""
    data = next_data()
    variant = product_of(data)["productOptionGroups"][0]["variants"][0]
    variant["variantStatusCode"] = status
    variant["quantityPerPerson"] = 18

    product = parse_product_page(render(data), PRODUCT_ID)

    assert product.variants[15375318].on_sale is False


def test_empty_option_groups_parse_to_no_variants() -> None:
    data = next_data()
    product_of(data)["productOptionGroups"] = []
    assert parse_product_page(render(data), PRODUCT_ID).variants == {}


def test_null_variants_in_a_group_contribute_nothing() -> None:
    """`productOptions: null`과 같은 관례. 그룹 하나가 비어도 다른 그룹은 그대로 읽는다."""
    data = next_data()
    product_of(data)["productOptionGroups"][0]["variants"] = None
    product = parse_product_page(render(data), PRODUCT_ID)
    assert sorted(product.variants) == [15375322, 15375323]


def test_group_without_variants_key_is_malformed() -> None:
    """키가 사라진 것은 구조 변경이다. 빈 목록으로 읽으면 variant_not_found로 오진한다."""
    data = next_data()
    del product_of(data)["productOptionGroups"][0]["variants"]
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(render(data), PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>일시적으로 서비스를 이용하실 수 없습니다.</body></html>",
        "",
        '<script id="__NEXT_DATA__" type="application/json">{not json</script>',
    ],
)
def test_page_without_usable_next_data_is_malformed(html: str) -> None:
    """Cloudflare 챌린지·대기열 페이지가 여기로 온다. 매진으로 오판하면 안 된다."""
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(html, PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


def test_missing_product_is_malformed() -> None:
    data = next_data()
    del data["props"]["pageProps"]["product"]
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(render(data), PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


def test_product_id_mismatch_is_malformed() -> None:
    """다른 상품으로 리다이렉트된 페이지를 그 상품의 상태로 읽으면 안 된다."""
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(fixture_html(), 1)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("field", ["variantId", "variantName", "variantStatusCode"])
def test_missing_variant_field_is_malformed(field: str) -> None:
    data = next_data()
    del product_of(data)["productOptionGroups"][0]["variants"][0][field]
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(render(data), PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("bad_id", ["15375318", None, 1.5, True])
def test_non_integer_variant_id_is_malformed(bad_id: Any) -> None:
    data = next_data()
    product_of(data)["productOptionGroups"][0]["variants"][0]["variantId"] = bad_id
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(render(data), PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("field", ["productOptionGroupName", "productOptionGroupStatusCode"])
def test_missing_group_field_is_malformed(field: str) -> None:
    data = next_data()
    del product_of(data)["productOptionGroups"][1][field]
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(render(data), PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


@pytest.mark.parametrize("bad_groups", [None, {}, "x"])
def test_option_groups_that_are_not_a_list_are_malformed(bad_groups: Any) -> None:
    data = next_data()
    product_of(data)["productOptionGroups"] = bad_groups
    with pytest.raises(YanoljaApiError) as exc:
        parse_product_page(render(data), PRODUCT_ID)
    assert exc.value.kind == "malformed_response"


# --- HTTP 클라이언트 ------------------------------------------------------


@responses.activate
def test_fetches_the_product_page_like_a_browser() -> None:
    responses.add(responses.GET, PAGE_URL, body=fixture_html(), status=200)

    YanoljaClient(sleep=SleepRecorder()).fetch_product(PRODUCT_ID)

    (call,) = responses.calls
    assert call.request.url == PAGE_URL
    assert "Mozilla/5.0" in call.request.headers["User-Agent"]
    assert "text/html" in call.request.headers["Accept"]
    assert call.request.headers["Accept-Language"].startswith("ko")


@responses.activate
def test_returns_parsed_product_on_success() -> None:
    responses.add(responses.GET, PAGE_URL, body=fixture_html(), status=200)
    product = YanoljaClient(sleep=SleepRecorder()).fetch_product(PRODUCT_ID)
    assert product.variants[15375322].on_sale is False


@pytest.mark.parametrize("status", [403, 429])
@responses.activate
def test_rate_limited_status_is_retried_then_reported(status: int) -> None:
    responses.add(responses.GET, PAGE_URL, body="blocked", status=status)
    sleeper = SleepRecorder()

    with pytest.raises(YanoljaApiError) as exc:
        YanoljaClient(sleep=sleeper).fetch_product(PRODUCT_ID)

    assert exc.value.kind == "rate_limited"
    assert exc.value.status == status
    assert len(responses.calls) == 3
    assert sleeper.delays == [2.0, 5.0]


@responses.activate
def test_server_error_is_retried_then_succeeds() -> None:
    responses.add(responses.GET, PAGE_URL, body="", status=503)
    responses.add(responses.GET, PAGE_URL, body="", status=502)
    responses.add(responses.GET, PAGE_URL, body=fixture_html(), status=200)

    product = YanoljaClient(sleep=SleepRecorder()).fetch_product(PRODUCT_ID)

    assert len(responses.calls) == 3
    assert product.product_id == PRODUCT_ID


@responses.activate
def test_challenge_page_with_200_is_malformed_and_not_retried() -> None:
    responses.add(responses.GET, PAGE_URL, body="<html>잠시 후 다시</html>", status=200)
    sleeper = SleepRecorder()

    with pytest.raises(YanoljaApiError) as exc:
        YanoljaClient(sleep=sleeper).fetch_product(PRODUCT_ID)

    assert exc.value.kind == "malformed_response"
    assert len(responses.calls) == 1
    assert sleeper.delays == []


@responses.activate
def test_timeout_is_retried_then_reported() -> None:
    responses.add(responses.GET, PAGE_URL, body=requests.Timeout("timed out"))
    sleeper = SleepRecorder()

    with pytest.raises(YanoljaApiError) as exc:
        YanoljaClient(sleep=sleeper).fetch_product(PRODUCT_ID)

    assert exc.value.kind == "timeout"
    assert len(responses.calls) == 3
    assert sleeper.delays == [2.0, 5.0]


def test_request_carries_the_documented_timeout() -> None:
    session = CapturingSession(fixture_html())
    client = YanoljaClient(session=session, sleep=SleepRecorder())  # type: ignore[arg-type]

    client.fetch_product(PRODUCT_ID)

    assert session.calls[0]["timeout"] == 15.0


def test_reset_session_logs_its_own_label(caplog: pytest.LogCaptureFixture) -> None:
    client = YanoljaClient(sleep=SleepRecorder())
    with caplog.at_level("INFO"):
        client.reset_session()
    client.close()
    assert "Yanolja requests.Session 초기화" in caplog.text
