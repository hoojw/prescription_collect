#!/usr/bin/env python3
"""Authorized JD prescription acquisition primitives.

This module deliberately stops at the order-detail boundary.  It uses the
already-authorized browser page for same-origin requests, parses the returned
server-rendered HTML, and exposes the raw order-level ``var vo`` object.  It
does not download prescription images, run OCR, mutate collector state, upload
to COS, or submit interface JSON.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qs, urljoin, urlparse


LIST_PATH = "/rx/rxInfo_list.action"
DETAIL_PATH = "/rx/rxInfo_auditView.action"


class JdApiError(RuntimeError):
    """Base class for JD acquisition failures."""


class JdAuthenticationError(JdApiError):
    """The authorized browser session is missing or expired."""


class JdHttpError(JdApiError):
    """JD returned a non-success HTTP status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = int(status_code)


class JdParseError(JdApiError):
    """The returned HTML did not contain the expected structure."""


class BrowserPage(Protocol):
    def run_js(self, expression: str) -> Any:
        """Evaluate JavaScript in the authorized JD browser page."""


@dataclass(frozen=True)
class BrowserResponse:
    status_code: int
    url: str
    text: str


@dataclass(frozen=True)
class JdOrderDetail:
    """One JD order-level detail response.

    The collector intentionally decides later whether the order contains one
    prescription or the known platform-bug case with multiple prescriptions.
    """

    rx_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class JdDrugItem:
    """Raw drug fields at the acquisition/normalization boundary."""

    sku_id: str
    sku_name: str
    buy_num: Any
    unit_price: Any
    source_index: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "sku_id": self.sku_id,
            "sku_name": self.sku_name,
            "buy_num": self.buy_num,
            "unit_price": self.unit_price,
            "source_index": self.source_index,
        }


def _json_object(value: Any, description: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise JdParseError(f"{description} did not return JSON") from exc
    if not isinstance(value, dict):
        raise JdParseError(
            f"{description} returned {type(value).__name__}, expected object"
        )
    return value


class JdBrowserTransport:
    """Same-origin browser transport with narrowly-scoped transient retries."""

    def __init__(self, page: BrowserPage, retries: int = 3):
        self.page = page
        self.retries = max(1, int(retries))

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        form: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> BrowserResponse:
        request = {
            "method": str(method or "GET").upper(),
            "path": str(path),
            "form": {str(key): str(value) for key, value in (form or {}).items()},
            "query": {str(key): str(value) for key, value in (query or {}).items()},
        }
        literal = json.dumps(request, ensure_ascii=False)
        script = f"""
        (async () => {{
          const request = {literal};
          const url = new URL(request.path, window.location.origin);
          for (const [name, value] of Object.entries(request.query)) {{
            url.searchParams.set(name, value);
          }}
          const options = {{
            method: request.method,
            credentials: 'include',
            cache: 'no-store',
            headers: {{}}
          }};
          if (request.method !== 'GET' && request.method !== 'HEAD') {{
            const body = new URLSearchParams();
            for (const [name, value] of Object.entries(request.form)) {{
              body.append(name, value);
            }}
            options.headers['Content-Type'] =
              'application/x-www-form-urlencoded;charset=UTF-8';
            options.body = body.toString();
          }}
          try {{
            const response = await fetch(url.toString(), options);
            const text = await response.text();
            return JSON.stringify({{
              transport_ok: true,
              http_status: response.status,
              response_url: response.url,
              text: text
            }});
          }} catch (error) {{
            return JSON.stringify({{
              transport_ok: false,
              error: String(error && error.message || error)
            }});
          }}
        }})()
        """
        result = _json_object(self.page.run_js(script), "browser request")
        if not result.get("transport_ok"):
            raise ConnectionError(str(result.get("error") or "browser fetch failed"))
        return BrowserResponse(
            status_code=int(result.get("http_status") or 0),
            url=str(result.get("response_url") or ""),
            text=str(result.get("text") or ""),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        form: Optional[Mapping[str, Any]] = None,
        query: Optional[Mapping[str, Any]] = None,
    ) -> BrowserResponse:
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._request_once(
                    method,
                    path,
                    form=form,
                    query=query,
                )
            except (ConnectionError, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
            else:
                if response.status_code != 429 and not (
                    500 <= response.status_code <= 599
                ):
                    return response
                last_error = JdHttpError(
                    response.status_code,
                    f"JD HTTP {response.status_code}",
                )
                if attempt >= self.retries:
                    raise last_error
            time.sleep(min(2**attempt, 8))
        if last_error is not None:
            raise last_error
        raise ConnectionError("JD browser request failed")


class _PrescriptionListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.form_query_found = False
        self._rx_container_tag = ""
        self._rx_container_nesting = 0
        self.hrefs: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: Sequence[tuple[str, Optional[str]]],
    ) -> None:
        attributes = {str(key): str(value or "") for key, value in attrs}
        if attributes.get("id") == "formQuery":
            self.form_query_found = True
        if self._rx_container_tag:
            if tag == self._rx_container_tag:
                self._rx_container_nesting += 1
        elif attributes.get("id") == "rxInfoTable":
            self._rx_container_tag = tag
            self._rx_container_nesting = 1
        if tag == "a" and self._rx_container_tag:
            href = attributes.get("href", "")
            if "rxInfo_auditView.action" in href and "rxId=" in href:
                self.hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._rx_container_tag and self._rx_container_nesting:
            self._rx_container_nesting -= 1
            if not self._rx_container_nesting:
                self._rx_container_tag = ""

    def handle_data(self, data: str) -> None:
        if data:
            self.text_parts.append(data)


class JdHtmlParser:
    """Parse JD server-rendered list and detail HTML without persisting it."""

    @staticmethod
    def parse_list(html: str, response_url: str) -> dict[str, Any]:
        parser = _PrescriptionListParser()
        parser.feed(str(html or ""))
        body_text = " ".join(parser.text_parts)
        if not parser.form_query_found or "处方管理" not in body_text:
            raise JdAuthenticationError(
                "JD login is missing or expired; manual re-login is required"
            )

        rx_ids: list[str] = []
        seen: set[str] = set()
        for href in parser.hrefs:
            query = parse_qs(urlparse(urljoin(response_url, href)).query)
            rx_id = str((query.get("rxId") or [""])[0]).strip()
            if rx_id and rx_id not in seen:
                seen.add(rx_id)
                rx_ids.append(rx_id)
        total_match = re.search(r"共\s*(\d+)\s*页", body_text)
        return {
            "total_pages": (
                int(total_match.group(1))
                if total_match
                else (1 if rx_ids else 0)
            ),
            "rx_ids": rx_ids,
        }

    @staticmethod
    def parse_detail(html: str) -> dict[str, Any]:
        source = str(html or "")
        marker = re.search(r"\bvar\s+vo\s*=", source)
        if not marker:
            raise JdParseError("JD detail var vo marker was not found")
        start = source.find("{", marker.end())
        if start < 0:
            raise JdParseError("JD detail var vo object start was not found")

        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(source)):
            character = source[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
                continue
            if character == "{":
                depth += 1
                continue
            if character != "}":
                continue
            depth -= 1
            if depth:
                continue
            try:
                value = json.loads(source[start : index + 1])
            except json.JSONDecodeError as exc:
                raise JdParseError(
                    "JD detail var vo was not JSON-compatible"
                ) from exc
            if not isinstance(value, dict):
                raise JdParseError("JD detail var vo was not an object")
            return value
        raise JdParseError("JD detail var vo object end was not found")


def build_list_form(
    start_date: str,
    end_date: str,
    page: int,
    page_size: int,
    rx_status_type: str = "2",
) -> dict[str, str]:
    """Build the approved JD audit-time query.

    ``rxStatusType=2`` includes approved prescriptions while excluding
    cancelled and audit-failed orders.  ``diagnosisStatus`` remains empty and
    is not used as a filter.
    """

    return {
        "index": str(page),
        "pageSize": str(page_size),
        "rxStatusType": str(rx_status_type or "2"),
        "userPin": "",
        "userPhone": "",
        "payType": "",
        "rxId": "",
        "orderId": "",
        "auditPin": "",
        "startDate": "",
        "endDate": "",
        "startAuditDate": str(start_date or ""),
        "endAuditDate": str(end_date or ""),
        "diagnosisStatus": "",
        "diagId": "",
        "rxType": "",
        "excess": "",
        "presaleType": "",
        "keplerSceneType": "",
    }


def _looks_like_login(response: BrowserResponse) -> bool:
    parsed = urlparse(response.url)
    location = f"{parsed.netloc}{parsed.path}".lower()
    if (
        "passport.jd.com" in location
        or "passport.shop.jd.com" in location
        or "/login" in location
    ):
        return True
    sample = response.text[:10000].lower()
    return (
        'id="loginname"' in sample
        or "passport.jd.com" in sample
        or "请登录" in sample
    )


class JdPrescriptionApi:
    """Order-level JD prescription list/detail client."""

    def __init__(
        self,
        transport: JdBrowserTransport,
        parser: Optional[JdHtmlParser] = None,
    ):
        self.transport = transport
        self.parser = parser or JdHtmlParser()

    @staticmethod
    def _require_success(response: BrowserResponse, operation: str) -> None:
        if _looks_like_login(response):
            raise JdAuthenticationError(
                "JD login is missing or expired; manual re-login is required"
            )
        if response.status_code != 200:
            raise JdHttpError(
                response.status_code,
                f"JD {operation} HTTP {response.status_code}",
            )

    def list_page(
        self,
        start_date: str,
        end_date: str,
        page: int,
        page_size: int,
        rx_status_type: str = "2",
    ) -> dict[str, Any]:
        response = self.transport.request(
            "POST",
            LIST_PATH,
            form=build_list_form(
                start_date,
                end_date,
                page,
                page_size,
                rx_status_type,
            ),
        )
        self._require_success(response, "list")
        return self.parser.parse_list(response.text, response.url)

    def enumerate_rx_ids(
        self,
        start_date: str,
        end_date: str,
        max_pages: int,
        page_size: int,
        rx_status_type: str = "2",
    ) -> tuple[list[str], dict[str, int]]:
        output: list[str] = []
        seen: set[str] = set()
        total_pages = 0
        pages_scanned = 0
        for page in range(1, max(1, int(max_pages)) + 1):
            result = self.list_page(
                start_date,
                end_date,
                page,
                page_size,
                rx_status_type,
            )
            pages_scanned += 1
            current_total = int(result.get("total_pages") or 0)
            if total_pages and current_total and current_total != total_pages:
                raise JdParseError(
                    f"JD pagination total changed from {total_pages} to {current_total}"
                )
            total_pages = current_total or total_pages
            page_ids = [str(item) for item in (result.get("rx_ids") or []) if str(item)]
            if page > 1 and page_ids and all(rx_id in seen for rx_id in page_ids):
                raise JdParseError("JD pagination repeated a previously scanned page")
            for rx_id in page_ids:
                if rx_id not in seen:
                    seen.add(rx_id)
                    output.append(rx_id)
            if not page_ids:
                break
            if total_pages and page >= total_pages:
                break
            if len(page_ids) < int(page_size):
                break
        return output, {
            "pages_scanned": pages_scanned,
            "total_pages": total_pages,
        }

    def detail(self, rx_id: str) -> JdOrderDetail:
        response = self.transport.request(
            "GET",
            DETAIL_PATH,
            query={"rxId": str(rx_id)},
        )
        self._require_success(response, "detail")
        return JdOrderDetail(
            rx_id=str(rx_id),
            raw=self.parser.parse_detail(response.text),
        )


def extract_drug_items(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return raw, unmerged drug fields from the effective JD drug list.

    The original list is used only when the whole effective ``rxSkuList`` is
    missing or empty.  A malformed item in a present effective list never
    triggers per-item fallback to ``originRxSkuList``.
    """

    raw_items = detail.get("rxSkuList")
    if not raw_items:
        raw_items = detail.get("originRxSkuList")
    if isinstance(raw_items, Mapping):
        raw_items = [raw_items]
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items,
        (str, bytes, bytearray),
    ):
        return []

    output: list[dict[str, Any]] = []
    for index, source in enumerate(raw_items):
        if not isinstance(source, Mapping):
            output.append(
                JdDrugItem("", "", None, None, index).as_dict()
            )
            continue
        output.append(
            JdDrugItem(
                sku_id=str(source.get("skuId") or "").strip(),
                sku_name=str(source.get("skuName") or "").strip(),
                buy_num=source.get("buyNum"),
                unit_price=(
                    source.get("unitPrice")
                    if source.get("unitPrice") is not None
                    else source.get("price")
                ),
                source_index=index,
            ).as_dict()
        )
    return output
