#!/usr/bin/env python3
"""园区应急广播播发裁决服务 —— Python 调用示例（仅用标准库，无需安装第三方包）。

运行:
    python examples/python_examples.py
    BASE_URL=http://localhost:8080 python examples/python_examples.py
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.getenv("BASE_URL", "http://localhost:8080").rstrip("/")
API = f"{BASE_URL}/api/v1"


def request(method: str, path: str, body: dict | None = None,
            params: dict | None = None) -> tuple[int, dict]:
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} ===")
    print(f"HTTP {status}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    # 1) 发布告警（时间由服务端以受理时间计算，客户端无需传时间）
    publish_body = {
        "request_no": "REQ-PY-0001",          # 值守员请求号：幂等键
        "alert_no": 20001,                    # 不可复用的正整数
        "areas": ["北门", "科技园路口"],        # 非空、互不重复
        "level": 4,                           # 1~5
        "content": "强对流天气预警，请减少户外活动",
        "delay_seconds": 0,                   # 非负延迟
        "duration_seconds": 900,              # 正持续时长
    }
    st, receipt = request("POST", "/commands/publish", publish_body)
    show("发布（首次 201）", st, receipt)

    # 2) 网络超时后原样重试：相同请求号 + 相同载荷 → 200 + 首次回执
    st2, retry = request("POST", "/commands/publish", publish_body)
    show("同请求号重试（200，首次回执）", st2, retry)

    # 3) 查询当前时刻
    st, now_q = request("GET", "/broadcasts")
    show("当前时刻播发", st, now_q)

    # 4) 查询指定时刻、指定区域（历史/未来时刻均可）
    moment = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    st, q = request("GET", "/broadcasts",
                    params={"at": moment, "area": ["北门"]})
    show("5 分钟后北门应播发", st, q)

    # 5) 撤销
    st, revoked = request("POST", "/commands/revoke",
                          {"request_no": "REQ-PY-R1", "alert_no": 20001,
                           "reason": "天气预警解除"})
    show("撤销（201）", st, revoked)

    # 6) 撤销未知目标 → 404
    st, missing = request("POST", "/commands/revoke",
                          {"request_no": "REQ-PY-RX", "alert_no": 999999})
    show("撤销未知告警（404）", st, missing)

    # 7) 非法载荷 → 422
    st, bad = request("POST", "/commands/publish", {
        "request_no": "REQ-PY-BAD", "alert_no": 1, "areas": [],
        "level": 3, "content": "", "delay_seconds": 0, "duration_seconds": 0,
    })
    show("非法载荷（422）", st, bad)


if __name__ == "__main__":
    main()
