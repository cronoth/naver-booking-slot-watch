"""야놀자 레저 상품 페이지(Next.js SSR) 클라이언트.

재고 API가 따로 없다. 옵션(variant)별 판매 상태는 상품 페이지 HTML 안
`<script id="__NEXT_DATA__">` JSON에 통째로 들어 있다.

수량처럼 보이는 필드(`quantityPerPerson`, `quantityPerPurchase`)는 구매 수량
선택 상한이지 재고가 아니다. 품절 옵션에도 18 같은 숫자가 남아 있다. 그래서
읽지 않는다. 판단 기준은 `variantStatusCode == "IN_SALE"` 하나다.

공식 공개 구조가 아니므로 페이지 구조가 바뀌면 `YanoljaApiError`로 끝난다.
조회 실패를 매진으로 바꾸지 않는 것은 네이버 모듈과 같은 계약이다.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from .http_client import FetchError, RetryingClient

PRODUCT_URL_TEMPLATE = "https://leisure-web.yanolja.com/leisure/{product_id}"

#: 이 값일 때만 구매할 수 있다. 그 외(`SOLD_OUT` 등)는 모두 매진으로 다룬다.
ON_SALE_STATUS = "IN_SALE"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class YanoljaApiError(FetchError):
    """야놀자 조회 실패. `kind`로 실패 종류를 구분해 로그와 백오프에 쓴다."""


@dataclass(frozen=True)
class VariantAvailability:
    variant_id: int
    name: str
    status_code: str
    group_name: str
    group_status_code: str

    @property
    def on_sale(self) -> bool:
        return self.status_code == ON_SALE_STATUS


@dataclass(frozen=True)
class ProductAvailability:
    """한 상품 페이지의 정상 응답. `variants`가 비어 있으면 옵션이 아예 없는 상품이다."""

    product_id: int
    status_code: str
    variants: dict[int, VariantAvailability]


def product_url(product_id: int) -> str:
    return PRODUCT_URL_TEMPLATE.format(product_id=product_id)


def _malformed(message: str) -> YanoljaApiError:
    """페이지 구조가 예상과 다를 때 쓰는 오류. 매진으로 오판하지 않도록 항상 예외다."""
    return YanoljaApiError(message, kind="malformed_response")


def parse_product_page(html: str, product_id: int) -> ProductAvailability:
    """상품 페이지 HTML을 옵션 상태 목록으로 변환한다. 순수 함수.

    Cloudflare 챌린지·대기열 페이지에는 `__NEXT_DATA__`가 없거나 `product`가 없다.
    둘 다 `malformed_response`다.
    """
    match = _NEXT_DATA.search(html)
    if match is None:
        raise _malformed("페이지에 __NEXT_DATA__가 없다")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise _malformed(f"__NEXT_DATA__가 JSON이 아니다: {exc}") from exc

    node: Any = data
    for key in ("props", "pageProps", "product"):
        if not isinstance(node, dict) or not isinstance(node.get(key), dict):
            raise _malformed(f"__NEXT_DATA__에 {key}가 없다")
        node = node[key]

    found_id = _required_int(node, "productId")
    if found_id != product_id:
        raise _malformed(f"다른 상품 페이지다: productId={found_id} (요청 {product_id})")

    groups = node.get("productOptionGroups")
    if not isinstance(groups, list):
        raise _malformed(f"productOptionGroups가 배열이 아니다: {type(groups).__name__}")

    variants: dict[int, VariantAvailability] = {}
    for group in groups:
        for variant in _parse_group(group):
            variants[variant.variant_id] = variant

    return ProductAvailability(
        product_id=product_id,
        status_code=_required_str(node, "productStatusCode"),
        variants=variants,
    )


def _parse_group(raw: Any) -> list[VariantAvailability]:
    if not isinstance(raw, dict):
        raise _malformed(f"옵션 그룹이 객체가 아니다: {type(raw).__name__}")
    group_name = _required_str(raw, "productOptionGroupName")
    group_status = _required_str(raw, "productOptionGroupStatusCode")
    if "variants" not in raw:
        # 키 자체가 없으면 구조가 바뀐 것이다. 빈 목록으로 읽으면 variant_not_found로 오진한다.
        raise _malformed(f"옵션 그룹 {group_name!r}에 variants가 없다")
    raw_variants = raw["variants"]
    if raw_variants is None:
        # `productOptions: null`과 같은 관례. 옵션이 없는 그룹이다.
        raw_variants = []
    if not isinstance(raw_variants, list):
        raise _malformed(f"variants가 배열이 아니다: {type(raw_variants).__name__}")
    return [_parse_variant(item, group_name, group_status) for item in raw_variants]


def _parse_variant(raw: Any, group_name: str, group_status: str) -> VariantAvailability:
    if not isinstance(raw, dict):
        raise _malformed(f"옵션이 객체가 아니다: {type(raw).__name__}")
    return VariantAvailability(
        variant_id=_required_int(raw, "variantId"),
        name=_required_str(raw, "variantName"),
        status_code=_required_str(raw, "variantStatusCode"),
        group_name=group_name,
        group_status_code=group_status,
    )


def _required_int(raw: dict[str, Any], key: str) -> int:
    value: object = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _malformed(f"{key}가 정수가 아니다: {value!r}")
    return value


def _required_str(raw: dict[str, Any], key: str) -> str:
    """누락도 오류로 다룬다. 상태 코드에 기본값을 주면 매진 여부를 지어내는 셈이다."""
    value = raw.get(key)
    if not isinstance(value, str):
        raise _malformed(f"{key}가 문자열이 아니다: {value!r}")
    return value


class YanoljaClient(RetryingClient):
    """상품 페이지 HTML 조회 전용 클라이언트."""

    error_class = YanoljaApiError
    session_label = "Yanolja"

    def fetch_product(self, product_id: int) -> ProductAvailability:
        return self._run_with_retry(lambda: self._attempt(product_id))

    def _attempt(self, product_id: int) -> ProductAvailability:
        response = self._send("get", product_url(product_id), headers=DEFAULT_HEADERS)
        return parse_product_page(response.text, product_id)
