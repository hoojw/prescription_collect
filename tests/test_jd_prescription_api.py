import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jd_prescription_api import (
    BrowserResponse,
    JdAuthenticationError,
    JdBrowserTransport,
    JdHtmlParser,
    JdParseError,
    JdPrescriptionApi,
    build_list_form,
    extract_drug_items,
)


class FakeBrowserPage:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def run_js(self, _expression):
        self.calls += 1
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        return self.responses.pop(0)


class JdPrescriptionApiTests(unittest.TestCase):
    def test_list_form_uses_approved_status_without_diagnosis_filter(self):
        form = build_list_form("2026-07-21", "2026-07-23", 2, 40)
        self.assertEqual(form["rxStatusType"], "2")
        self.assertEqual(form["diagnosisStatus"], "")
        self.assertEqual(form["startAuditDate"], "2026-07-21")
        self.assertEqual(form["endAuditDate"], "2026-07-23")

    def test_list_html_is_parsed_without_persisting_raw_html(self):
        html = """
        <html><body>
          <form id="formQuery">处方管理</form>
          <table><tbody id="rxInfoTable">
            <tr><td><a href="/rx/rxInfo_auditView.action?rxId=rx-2">查看</a></td></tr>
            <tr><td><a href="/rx/rxInfo_auditView.action?rxId=rx-1">查看</a></td></tr>
          </tbody></table>
          共 4 页
        </body></html>
        """
        result = JdHtmlParser.parse_list(
            html,
            "https://rx.shop.jd.com/rx/rxInfo_list.action",
        )
        self.assertEqual(result["rx_ids"], ["rx-2", "rx-1"])
        self.assertEqual(result["total_pages"], 4)

    def test_list_html_without_authorized_form_is_login_failure(self):
        with self.assertRaises(JdAuthenticationError):
            JdHtmlParser.parse_list(
                "<html><body>请登录</body></html>",
                "https://rx.shop.jd.com/rx/rxInfo_list.action",
            )

    def test_passport_shop_redirect_is_login_failure_not_http_404(self):
        transport = FakeTransport(
            [
                BrowserResponse(
                    404,
                    "https://passport.shop.jd.com/rx/rxInfo_list.action",
                    "<html><title>出错啦</title></html>",
                )
            ]
        )
        with self.assertRaises(JdAuthenticationError):
            JdPrescriptionApi(transport).list_page(
                "2026-08-02",
                "2026-08-03",
                1,
                1,
            )

    def test_detail_parser_keeps_order_level_object(self):
        html = """
        <script>
        var vo = {"rxId":"rx-1","orderId":"order-1",
                  "rxExtendsInfo":{"note":"brace } in string"},
                  "rxSkuList":[{"skuId":"sku-1","skuName":"药品甲","buyNum":1}]};
        </script>
        """
        result = JdHtmlParser.parse_detail(html)
        self.assertEqual(result["orderId"], "order-1")
        self.assertEqual(len(result["rxSkuList"]), 1)

    def test_detail_parser_rejects_missing_var_vo(self):
        with self.assertRaises(JdParseError):
            JdHtmlParser.parse_detail("<html><body>处方详情</body></html>")

    def test_transport_retries_only_transient_http_status(self):
        page = FakeBrowserPage(
            [
                {
                    "transport_ok": True,
                    "http_status": 500,
                    "response_url": "https://rx.shop.jd.com/rx/rxInfo_list.action",
                    "text": "temporary",
                },
                {
                    "transport_ok": True,
                    "http_status": 200,
                    "response_url": "https://rx.shop.jd.com/rx/rxInfo_list.action",
                    "text": "ok",
                },
            ]
        )
        with mock.patch("jd_prescription_api.time.sleep"):
            response = JdBrowserTransport(page, retries=2).request(
                "POST",
                "/rx/rxInfo_list.action",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(page.calls, 2)

    def test_api_detail_returns_order_not_flattened_prescriptions(self):
        transport = FakeTransport(
            [
                BrowserResponse(
                    200,
                    "https://rx.shop.jd.com/rx/rxInfo_auditView.action?rxId=rx-1",
                    '<script>var vo={"rxId":"rx-1","orderId":"order-1"};</script>',
                )
            ]
        )
        detail = JdPrescriptionApi(transport).detail("rx-1")
        self.assertEqual(detail.rx_id, "rx-1")
        self.assertEqual(detail.raw["orderId"], "order-1")

    def test_effective_drug_list_is_not_merged_with_original_list(self):
        drugs = extract_drug_items(
            {
                "rxSkuList": [
                    {
                        "skuId": "effective",
                        "skuName": "当前药品",
                        "buyNum": 1,
                        "price": 12.3,
                    }
                ],
                "originRxSkuList": [
                    {
                        "skuId": "original",
                        "skuName": "原始药品",
                        "buyNum": 2,
                    }
                ],
            }
        )
        self.assertEqual([drug["sku_id"] for drug in drugs], ["effective"])
        self.assertEqual(drugs[0]["source_index"], 0)

    def test_original_drug_list_is_whole_list_fallback_only(self):
        drugs = extract_drug_items(
            {
                "rxSkuList": [],
                "originRxSkuList": [
                    {
                        "skuId": "original",
                        "skuName": "原始药品",
                        "buyNum": 2,
                    }
                ],
            }
        )
        self.assertEqual([drug["sku_id"] for drug in drugs], ["original"])

    def test_repeated_list_page_is_treated_as_pagination_failure(self):
        page_html = """
        <form id="formQuery">处方管理</form>
        <table id="rxInfoTable">
          <a href="/rx/rxInfo_auditView.action?rxId=rx-1">查看</a>
        </table>
        共 3 页
        """
        transport = FakeTransport(
            [
                BrowserResponse(200, "https://rx.shop.jd.com/rx/rxInfo_list.action", page_html),
                BrowserResponse(200, "https://rx.shop.jd.com/rx/rxInfo_list.action", page_html),
            ]
        )
        with self.assertRaises(JdParseError):
            JdPrescriptionApi(transport).enumerate_rx_ids(
                "2026-07-21",
                "2026-07-23",
                3,
                1,
            )


if __name__ == "__main__":
    unittest.main()
