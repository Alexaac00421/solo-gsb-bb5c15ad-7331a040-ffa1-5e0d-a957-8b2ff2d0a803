# 园区应急广播播发裁决服务

基于 **Python / FastAPI + PostgreSQL** 的应急广播播发裁决服务。值守员通过请求号（`request_no`）提交**发布**或**撤销**命令，服务以受理时间计算告警生效区间，按统一裁决规则回答“指定时刻各区域应播发哪条告警”。

## 快速启动

依赖：Docker + Docker Compose。

```bash
docker compose up --build -d
```

Compose 会一并启动 PostgreSQL 并等待其健康检查通过；服务启动时自动初始化表结构（无需手工迁移）。

- 服务地址：`http://localhost:8000`
- 交互式接口文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`curl http://localhost:8000/health`

停止并清理：

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连同数据一起删除
```

## 配置

均通过环境变量配置（`docker compose -f docker-compose.yml` 中已为 `app` 设置好默认值）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://broadcast:broadcast@db:5432/broadcast` | PostgreSQL 连接串（asyncpg 驱动） |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库就绪的重试次数 |
| `DB_CONNECT_RETRY_INTERVAL` | `1.0` | 重试间隔（秒） |

端口：服务监听容器内 `8000`，Compose 映射到宿主机 `8000`；PostgreSQL 映射到宿主机 `5432`。

## 数据与裁决规则

- 发布命令以**受理时间**（服务端时钟）为基准：`生效区间 = [受理时间 + 延迟秒数, 受理时间 + 延迟秒数 + 持续秒数)`，左闭右开。
- 撤销命令以受理时间记录 `revoked_at`，该时刻起终止后续播发；**历史时刻的查询结果不受影响**。
- 同一区域同一时刻有多条有效告警时，取 **级别高 → 受理时间早 → 告警号小** 的第一条；高级别告警到期后，仍有效的低级别告警自动恢复播发。
- 告警号为正整数且**不可复用**；区域列表非空、互不重复；级别 1～5；文案非空；延迟秒数为非负整数；持续秒数为正整数。校验失败或冲突时整体回滚，**不留部分状态**；一次发布覆盖的多个区域**同成同败**。
- `request_no` 为幂等键：同一请求号（无论并发还是串行重试）返回**首次回执**；请求号相同但载荷不同返回 `409 request_no_conflict`。仅受理成功的命令占用请求号，校验失败可修正后重发。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/commands/publish` | 提交发布命令，返回命令回执 |
| POST | `/api/v1/commands/revoke` | 提交撤销命令，返回命令回执 |
| GET | `/api/v1/broadcasts?at=&region=` | 查询指定时刻各区域应播发的告警（`at` 缺省为当前时间；`region` 缺省返回全部区域） |
| GET | `/api/v1/alerts/{alert_no}` | 查询告警详情（含撤销时间） |
| GET | `/health` | 健康检查 |

错误响应统一为 `{"error": {"code": ..., "message": ...}}`：`422 validation_error`（参数校验失败）、`409 request_no_conflict`（请求号冲突）、`409 alert_no_conflict`（告警号复用）、`404 alert_not_found`（撤销/查询未知告警）。

## 调用示例

### 1. 发布告警

```bash
curl -s -X POST http://localhost:8000/api/v1/commands/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "req-20261006-0001",
    "alert_no": 1001,
    "regions": ["东区", "西区"],
    "level": 4,
    "message": "东区危化品泄漏，请立即疏散",
    "delay_seconds": 0,
    "duration_seconds": 600
  }'
```

响应（命令回执）：

```json
{
  "request_no": "req-20261006-0001",
  "command": "publish",
  "status": "accepted",
  "accepted_at": "2026-10-06T08:00:00.123456+00:00",
  "alert": {
    "alert_no": 1001,
    "level": 4,
    "message": "东区危化品泄漏，请立即疏散",
    "regions": ["东区", "西区"],
    "delay_seconds": 0,
    "duration_seconds": 600,
    "accepted_at": "2026-10-06T08:00:00.123456+00:00",
    "start_at": "2026-10-06T08:00:00.123456+00:00",
    "end_at": "2026-10-06T08:10:00.123456+00:00",
    "revoked_at": null
  }
}
```

幂等重试（同一 `request_no`、同一载荷，返回首次回执）：

```bash
curl -s -X POST http://localhost:8000/api/v1/commands/publish \
  -H 'Content-Type: application/json' \
  -d '{"request_no":"req-20261006-0001","alert_no":1001,"regions":["东区","西区"],"level":4,"message":"东区危化品泄漏，请立即疏散","delay_seconds":0,"duration_seconds":600}'
```

同一 `request_no` 但载荷不同 → `409`：

```json
{"error": {"code": "request_no_conflict", "message": "请求号 'req-20261006-0001' 已被不同载荷的命令占用"}}
```

### 2. 查询指定时刻各区域应播发的告警

```bash
# 当前时刻、全部区域
curl -s 'http://localhost:8000/api/v1/broadcasts'

# 指定时刻、指定区域
curl -s 'http://localhost:8000/api/v1/broadcasts?at=2026-10-06T08:05:00Z&region=东区'
```

全部区域响应（无告警的区域不出现在列表中；整体无告警时 `broadcasts` 为空数组）：

```json
{
  "at": "2026-10-06T08:05:00+00:00",
  "broadcasts": [
    {"region": "东区", "alert": {"alert_no": 1001, "level": 4, "message": "东区危化品泄漏，请立即疏散", "...": "..."}},
    {"region": "西区", "alert": {"alert_no": 1001, "level": 4, "message": "东区危化品泄漏，请立即疏散", "...": "..."}}
  ]
}
```

指定区域响应（无应播告警时 `alert` 为 `null`）：

```json
{"at": "2026-10-06T08:05:00+00:00", "region": "东区", "alert": {"alert_no": 1001, "...": "..."}}
```

### 3. 撤销告警

```bash
curl -s -X POST http://localhost:8000/api/v1/commands/revoke \
  -H 'Content-Type: application/json' \
  -d '{"request_no": "req-20261006-0002", "alert_no": 1001}'
```

```json
{
  "request_no": "req-20261006-0002",
  "command": "revoke",
  "status": "accepted",
  "accepted_at": "2026-10-06T08:03:00.000000+00:00",
  "alert_no": 1001,
  "revoked_at": "2026-10-06T08:03:00.000000+00:00"
}
```

撤销后：`at >= revoked_at` 的查询不再返回该告警，`at < revoked_at` 的历史时刻仍可查到。撤销未知告警号返回 `404 alert_not_found`。

### 4. 查询告警详情

```bash
curl -s http://localhost:8000/api/v1/alerts/1001
```

## 运行测试

```bash
docker compose exec app python -m pytest -q
```

测试覆盖：发布回执与生效区间推算、参数校验失败不留部分状态、告警号不可复用、请求号幂等（串行重试 / 并发重试 / 载荷冲突）、撤销后历史可查与未来终止、未知目标报错、多告警裁决顺序与低级别自动恢复等。

## 本地开发（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 准备本地 PostgreSQL 后：
export DATABASE_URL='postgresql+asyncpg://broadcast:broadcast@localhost:5432/broadcast'
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 设计要点

- **幂等**：`commands` 表以 `request_no` 为主键，命令行与业务写入在**同一事务**内完成；`INSERT ... ON CONFLICT DO NOTHING` 抢占幂等键，并发下同键请求会阻塞至首个事务提交，随后返回首次回执；载荷（JSONB 规范化存储）不一致则报 409。
- **原子性**：告警行与全部区域行同事务写入，任一失败整体回滚；告警号冲突（含并发抢号）同样回滚，不留部分状态。
- **裁决**：查询侧按 `级别 DESC, 受理时间 ASC, 告警号 ASC` 取一条（单区域 `LIMIT 1`，全区域 `DISTINCT ON`），高级别到期后低级别自然恢复，无需额外状态机。
- **撤销**：`revoked_at = LEAST(原 revoked_at, 受理时间)`，并发/重复撤销保留最早终止时刻；告警行保留，历史时刻查询不受影响。
