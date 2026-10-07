#!/usr/bin/env bash
# 园区应急广播播发裁决服务 —— cURL 调用示例
# 用法: BASE=http://localhost:8080 bash examples/curl_examples.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
API="$BASE/api/v1"

say() { printf '\n=== %s ===\n' "$1"; }

say "1) 健康检查"
curl -sS "$BASE/healthz"; echo
curl -sS "$BASE/readyz"; echo

say "2) 发布告警（请求号 REQ-20261007-001）"
curl -sS -X POST "$API/commands/publish" \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "REQ-20261007-001",
    "alert_no": 10001,
    "areas": ["东门", "南门", "中央广场"],
    "level": 4,
    "content": "发现火情，请附近人员沿指示牌有序撤离",
    "delay_seconds": 0,
    "duration_seconds": 1800
  }'; echo

say "3) 同一请求号重试（幂等：返回首次回执，HTTP 200，不新建告警）"
curl -sS -i -X POST "$API/commands/publish" \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "REQ-20261007-001",
    "alert_no": 10001,
    "areas": ["中央广场", "东门", "南门"],
    "level": 4,
    "content": "发现火情，请附近人员沿指示牌有序撤离",
    "delay_seconds": 0,
    "duration_seconds": 1800
  }' | sed -n '1p;/^{/,$p'; echo

say "4) 同请求号不同载荷 -> 409 冲突"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST "$API/commands/publish" \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-20261007-001","alert_no":10001,"areas":["东门"],
       "level":1,"content":"被篡改的文案","delay_seconds":0,"duration_seconds":60}'

say "5) 查询当前时刻各区域应播发内容"
curl -sS "$API/broadcasts"; echo

say "6) 查询指定时刻（ISO-8601；可查历史/未来）并限定区域"
curl -sS --get "$API/broadcasts" \
  --data-urlencode "at=2026-10-07T13:00:00+08:00" \
  --data-urlencode "area=东门" \
  --data-urlencode "area=中央广场"; echo

say "7) 发布一条更高级别告警覆盖 东门（级别高者胜出）"
curl -sS -X POST "$API/commands/publish" \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "REQ-20261007-002",
    "alert_no": 10002,
    "areas": ["东门"],
    "level": 5,
    "content": "东门区域立即撤离！",
    "delay_seconds": 0,
    "duration_seconds": 300
  }'; echo
curl -sS --get "$API/broadcasts" --data-urlencode "area=东门"; echo

say "8) 撤销告警 10002（按受理时间终止后续播发，历史时刻仍可查）"
curl -sS -X POST "$API/commands/revoke" \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-20261007-R1","alert_no":10002,"reason":"现场确认安全"}'; echo

say "9) 撤销未知告警 -> 404"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$API/commands/revoke" \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-20261007-RX","alert_no":999999}'

say "10) 非法载荷示例（空区域/非正持续时长 -> 422，不落任何状态）"
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$API/commands/publish" \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"REQ-BAD","alert_no":1,"areas":[],"level":3,
       "content":"","delay_seconds":0,"duration_seconds":0}'
