import json
from typing import Any


def list_jd_prescriptions_page(
    tab: Any,
    start_date: str,
    end_date: str,
    page: int,
    page_size: int = 10,
) -> dict[str, Any]:
    parameters = {
        "index": str(page),
        "pageSize": str(page_size),
        "rxStatusType": "0",
        "userPin": "",
        "userPhone": "",
        "payType": "",
        "rxId": "",
        "orderId": "",
        "auditPin": "",
        "startDate": "",
        "endDate": "",
        "startAuditDate": start_date,
        "endAuditDate": end_date,
        "diagnosisStatus": "",
        "diagId": "",
    }

    parameter_literal = json.dumps(
        parameters,
        ensure_ascii=False,
    )

    script = f"""
    (async () => {{
        const parameters = {parameter_literal};
        const form = new URLSearchParams();

        for (const [name, value] of Object.entries(parameters)) {{
            form.append(name, value);
        }}

        const response = await fetch(
            '/rx/rxInfo_list.action',
            {{
                method: 'POST',
                credentials: 'include',
                cache: 'no-store',
                headers: {{
                    'Content-Type':
                        'application/x-www-form-urlencoded;charset=UTF-8'
                }},
                body: form.toString()
            }}
        );

        const html = await response.text();

        return JSON.stringify({{
            http_status: response.status,
            content_type:
                response.headers.get('content-type') || '',
            html: html
        }});
    }})()
    """

    result = tab.run_js(script)

    if isinstance(result, str):
        result = json.loads(result)

    if not isinstance(result, dict):
        raise RuntimeError("JD list result is not an object")

    if result.get("http_status") != 200:
        raise RuntimeError(
            f"JD list HTTP {result.get('http_status')}"
        )

    return result