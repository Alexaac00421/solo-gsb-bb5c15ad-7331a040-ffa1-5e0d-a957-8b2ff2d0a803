from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, text

from app.db import SessionLocal, engine
from app.main import app
from app.models import Alert, AlertRegion, Base, Command

PUBLISH_URL = "/api/v1/commands/publish"
REVOKE_URL = "/api/v1/commands/revoke"
BROADCASTS_URL = "/api/v1/broadcasts"


@pytest.fixture(autouse=True)
async def reset_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("TRUNCATE TABLE commands, alerts, alert_regions RESTART IDENTITY CASCADE")
        )
    yield
    # 每个测试使用独立事件循环，释放连接池避免跨循环复用连接
    await engine.dispose()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


def publish_payload(**overrides) -> dict:
    p = {
        "request_no": "req-0001",
        "alert_no": 1001,
        "regions": ["东区", "西区"],
        "level": 3,
        "message": "测试告警：请立即疏散",
        "delay_seconds": 0,
        "duration_seconds": 600,
    }
    p.update(overrides)
    return p


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


async def db_counts() -> tuple[int, int, int]:
    async with SessionLocal() as s:
        alerts = await s.scalar(select(func.count(Alert.alert_no)))
        regions = await s.scalar(select(func.count(AlertRegion.alert_no)))
        commands = await s.scalar(select(func.count(Command.request_no)))
    return alerts, regions, commands


# ---------- 发布：正常与校验 ----------


async def test_publish_success_receipt(client):
    resp = await client.post(PUBLISH_URL, json=publish_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["command"] == "publish"
    assert body["request_no"] == "req-0001"
    alert = body["alert"]
    assert alert["alert_no"] == 1001
    assert alert["regions"] == ["东区", "西区"]
    # 生效区间由受理时间推算
    accepted = parse(body["accepted_at"])
    assert parse(alert["start_at"]) == accepted + timedelta(seconds=0)
    assert parse(alert["end_at"]) == accepted + timedelta(seconds=600)
    assert alert["revoked_at"] is None
    assert await db_counts() == (1, 2, 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("regions", []),               # 空区域列表
        ("regions", ["东区", "东区"]),  # 区域重复
        ("regions", ["   "]),          # 空白区域名
        ("message", ""),               # 空文案
        ("message", "   "),            # 空白文案
        ("duration_seconds", 0),       # 非正持续时长
        ("duration_seconds", -5),
        ("delay_seconds", -1),         # 负延迟
        ("level", 0),
        ("level", 6),
        ("alert_no", 0),               # 非正告警号
        ("alert_no", -3),
    ],
)
async def test_publish_validation_rejected_leaves_no_state(client, field, value):
    resp = await client.post(PUBLISH_URL, json=publish_payload(**{field: value}))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    # 失败不得留下部分状态
    assert await db_counts() == (0, 0, 0)


async def test_alert_no_cannot_be_reused(client):
    first = await client.post(PUBLISH_URL, json=publish_payload())
    assert first.status_code == 200
    again = await client.post(PUBLISH_URL, json=publish_payload(request_no="req-0002"))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "alert_no_conflict"
    assert await db_counts() == (1, 2, 1)


# ---------- 幂等：同一请求号 ----------


async def test_same_request_no_returns_first_receipt(client):
    first = await client.post(PUBLISH_URL, json=publish_payload())
    replay = await client.post(PUBLISH_URL, json=publish_payload())
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert await db_counts() == (1, 2, 1)  # 只落库一次


async def test_same_request_no_different_payload_conflicts(client):
    await client.post(PUBLISH_URL, json=publish_payload())
    resp = await client.post(PUBLISH_URL, json=publish_payload(message="另一条文案"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "request_no_conflict"
    # 跨命令类型同号同样冲突
    resp2 = await client.post(REVOKE_URL, json={"request_no": "req-0001", "alert_no": 1001})
    assert resp2.status_code == 409


async def test_concurrent_same_request_no(client):
    results = await asyncio.gather(
        *[client.post(PUBLISH_URL, json=publish_payload()) for _ in range(5)]
    )
    assert all(r.status_code == 200 for r in results)
    assert all(r.json() == results[0].json() for r in results)
    assert await db_counts() == (1, 2, 1)


async def test_concurrent_same_alert_no(client):
    a, b = await asyncio.gather(
        client.post(PUBLISH_URL, json=publish_payload(request_no="r1")),
        client.post(PUBLISH_URL, json=publish_payload(request_no="r2")),
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    alerts, regions, _ = await db_counts()
    assert (alerts, regions) == (1, 2)


# ---------- 撤销 ----------


async def test_revoke_terminates_future_but_keeps_history(client):
    pub = (await client.post(PUBLISH_URL, json=publish_payload())).json()
    start_at = pub["alert"]["start_at"]

    rev = await client.post(REVOKE_URL, json={"request_no": "rv-1", "alert_no": 1001})
    assert rev.status_code == 200
    revoked_at = rev.json()["revoked_at"]

    # 历史时刻（生效起点）仍可查询
    hist = await client.get(BROADCASTS_URL, params={"at": start_at, "region": "东区"})
    assert hist.json()["alert"]["alert_no"] == 1001

    # 撤销时刻起终止后续播发
    after = parse(revoked_at) + timedelta(seconds=1)
    future = await client.get(BROADCASTS_URL, params={"at": after.isoformat(), "region": "东区"})
    assert future.json()["alert"] is None


async def test_revoke_before_start_suppresses_broadcast(client):
    pub = (await client.post(PUBLISH_URL, json=publish_payload(delay_seconds=300))).json()
    await client.post(REVOKE_URL, json={"request_no": "rv-1", "alert_no": 1001})
    resp = await client.get(
        BROADCASTS_URL, params={"at": pub["alert"]["start_at"], "region": "东区"}
    )
    assert resp.json()["alert"] is None


async def test_revoke_unknown_alert(client):
    resp = await client.post(REVOKE_URL, json={"request_no": "rv-x", "alert_no": 999})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "alert_not_found"
    assert await db_counts() == (0, 0, 0)


async def test_revoke_idempotent_and_conflict(client):
    await client.post(PUBLISH_URL, json=publish_payload())
    r1 = await client.post(REVOKE_URL, json={"request_no": "rv-1", "alert_no": 1001})
    r2 = await client.post(REVOKE_URL, json={"request_no": "rv-1", "alert_no": 1001})
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    conflict = await client.post(REVOKE_URL, json={"request_no": "rv-1", "alert_no": 2002})
    assert conflict.status_code == 409


async def test_second_revoke_keeps_earliest_time(client):
    await client.post(PUBLISH_URL, json=publish_payload())
    r1 = (await client.post(REVOKE_URL, json={"request_no": "rv-1", "alert_no": 1001})).json()
    r2 = await client.post(REVOKE_URL, json={"request_no": "rv-2", "alert_no": 1001})
    assert r2.status_code == 200
    assert r2.json()["revoked_at"] == r1["revoked_at"]


# ---------- 查询与裁决 ----------


async def test_query_empty_result(client):
    all_regions = await client.get(BROADCASTS_URL)
    assert all_regions.status_code == 200
    assert all_regions.json()["broadcasts"] == []
    one = await client.get(BROADCASTS_URL, params={"region": "东区"})
    assert one.json()["alert"] is None


async def test_broadcasts_all_regions(client):
    await client.post(PUBLISH_URL, json=publish_payload())
    resp = await client.get(BROADCASTS_URL)
    entries = {b["region"]: b["alert"] for b in resp.json()["broadcasts"]}
    assert set(entries) == {"东区", "西区"}
    assert all(a["alert_no"] == 1001 for a in entries.values())
    other = await client.get(BROADCASTS_URL, params={"region": "南区"})
    assert other.json()["alert"] is None


async def test_delay_window_half_open(client):
    pub = (await client.post(PUBLISH_URL, json=publish_payload(delay_seconds=300))).json()
    now = await client.get(BROADCASTS_URL, params={"region": "东区"})
    assert now.json()["alert"] is None  # 延迟期内不播
    at_start = await client.get(
        BROADCASTS_URL, params={"at": pub["alert"]["start_at"], "region": "东区"}
    )
    assert at_start.json()["alert"]["alert_no"] == 1001  # 起点含
    at_end = await client.get(
        BROADCASTS_URL, params={"at": pub["alert"]["end_at"], "region": "东区"}
    )
    assert at_end.json()["alert"] is None  # 终点不含


async def test_lower_level_resumes_after_high_expires(client):
    high = (
        await client.post(
            PUBLISH_URL,
            json=publish_payload(
                request_no="r-high", alert_no=1, regions=["R"], level=5, duration_seconds=60
            ),
        )
    ).json()
    await client.post(
        PUBLISH_URL,
        json=publish_payload(
            request_no="r-low", alert_no=2, regions=["R"], level=1, duration_seconds=3600
        ),
    )
    during = parse(high["alert"]["start_at"]) + timedelta(seconds=1)
    resp = await client.get(BROADCASTS_URL, params={"at": during.isoformat(), "region": "R"})
    assert resp.json()["alert"]["alert_no"] == 1  # 高级别优先
    after = parse(high["alert"]["end_at"]) + timedelta(seconds=1)
    resp = await client.get(BROADCASTS_URL, params={"at": after.isoformat(), "region": "R"})
    assert resp.json()["alert"]["alert_no"] == 2  # 高级别到期后低级别自动恢复


async def test_same_level_earlier_accepted_wins(client):
    await client.post(
        PUBLISH_URL, json=publish_payload(request_no="r-a", alert_no=9001, regions=["R"], level=3)
    )
    await client.post(
        PUBLISH_URL, json=publish_payload(request_no="r-b", alert_no=1002, regions=["R"], level=3)
    )
    resp = await client.get(BROADCASTS_URL, params={"region": "R"})
    assert resp.json()["alert"]["alert_no"] == 9001  # 受理早者优先于告警号小者


async def test_same_accepted_time_smaller_alert_no_wins(client):
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        async with s.begin():
            for no in (3002, 3001):  # 同时受理、同级别
                s.add(
                    Alert(
                        alert_no=no,
                        level=4,
                        message=f"m{no}",
                        delay_seconds=0,
                        duration_seconds=600,
                        accepted_at=now,
                        start_at=now,
                        end_at=now + timedelta(seconds=600),
                        regions=[AlertRegion(region="R")],
                    )
                )
    resp = await client.get(BROADCASTS_URL, params={"region": "R"})
    assert resp.json()["alert"]["alert_no"] == 3001


async def test_get_alert_detail(client):
    await client.post(PUBLISH_URL, json=publish_payload())
    resp = await client.get("/api/v1/alerts/1001")
    assert resp.status_code == 200
    assert resp.json()["regions"] == ["东区", "西区"]
    missing = await client.get("/api/v1/alerts/9999")
    assert missing.status_code == 404
