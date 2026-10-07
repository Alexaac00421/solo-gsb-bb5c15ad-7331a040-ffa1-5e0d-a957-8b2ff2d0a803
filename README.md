# 园区应急广播 · 播发裁决服务

面向园区应急广播值守台的**播发裁决服务**：值守员以「请求号」提交发布 / 撤销命令，
服务以**受理时间**为准计算告警生效区间，保证命令幂等、区域同成同败，
并支持查询任意时刻各区域「应当播发」的唯一一条告警。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Uvicorn
- 交付形式：Dockerfile + docker compose，**一键启动并自动初始化数据库依赖**

---

## 1. 一键启动

前置：已安装 Docker 与 Docker Compose 插件（`docker compose version` 可用）。

```bash
docker compose up -d --build
```

该命令会：

1. 启动 PostgreSQL 16（带健康检查，数据落命名卷 `pgdata`）；
2. 构建并启动 API 容器，等待数据库就绪；
3. 应用启动时自动 `CREATE TABLE IF NOT EXISTS` 初始化全部表结构与约束，
   **无需手工跑 SQL / 迁移命令**。

查看状态与日志：

```bash
docker compose ps
docker compose logs -f api
```

停止 / 清空：

```bash
docker compose down          # 停止，保留数据库数据
docker compose down -v       # 停止并删除数据库卷（重新初始化）
```

> 也提供交互式 API 文档：启动后访问 <http://localhost:8080/docs>（Swagger UI）。

---

## 2. 配置

所有配置通过环境变量注入，均有默认值，开箱即用。
可复制 `.env.example` 为 `.env` 后修改，`docker compose` 会自动读取。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `broadcast` | 数据库用户 |
| `POSTGRES_PASSWORD` | `broadcast` | 数据库密码（生产请修改） |
| `POSTGRES_DB` | `broadcast` | 数据库名 |
| `POSTGRES_PORT_PUBLISH` | `5432` | PostgreSQL 发布到宿主机的端口 |
| `APP_PORT_PUBLISH` | `8080` | API 发布到宿主机的端口 |
| `MAX_SECONDS` | `315360000` | 延迟 / 持续秒数允许的最大值（默认约 10 年） |
| `DATABASE_URL` | 由 Compose 拼装 | 直连数据库的 SQLAlchemy DSN（仅裸跑时需设置） |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8080` | 容器内监听地址端口 |

### 不用容器、本地裸跑（可选）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://broadcast:broadcast@127.0.0.1:5432/broadcast"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 3. 服务地址与端口

- 默认基地址：**`http://localhost:8080`**
- 接口前缀：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/commands/publish` | 发布告警（请求号幂等） |
| POST | `/api/v1/commands/revoke` | 撤销既有告警 |
| GET  | `/api/v1/broadcasts` | 查询指定时刻各区域应播发的告警 |
| GET  | `/healthz` | 存活探针 |
| GET  | `/readyz` | 就绪探针（检查数据库连通性） |
| GET  | `/docs` | Swagger 交互文档 |

### 发布 `POST /api/v1/commands/publish`

请求字段：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `request_no` | string | 非空，值守员请求号，幂等键 |
| `alert_no` | int | **不可复用的正整数** |
| `areas` | string[] | **非空、元素互不重复、每项非空**（服务端对区域名去空白后校验重复） |
| `level` | int | `1~5`（5 最高） |
| `content` | string | **非空文案**（纯空白也拒绝） |
| `delay_seconds` | int | **非负整数**延迟秒数 |
| `duration_seconds` | int | **正整数**持续秒数 |

生效区间以**服务端受理时间** `accepted_at` 计算：

```
effective_at = accepted_at + delay_seconds
expire_at    = effective_at + duration_seconds   （半开区间 [effective_at, expire_at)）
```

- 首次受理返回 **201** 与命令回执；
- 同一 `request_no` 的串行/并发重试返回 **200** 与**首次回执**（时间戳也是首次的）；
- 同一 `request_no` 但载荷不一致 → **409**；
- `alert_no` 已存在（含并发抢占）→ **409**；
- 载荷非法 → **422**，整个事务回滚，**不留任何部分状态**。

> 区域顺序不影响幂等判定（`["A","B"]` 与 `["B","A"]` 视为同一载荷）。

### 撤销 `POST /api/v1/commands/revoke`

请求字段：`request_no`（非空）、`alert_no`（正整数）、`reason`（可选）。

- 按**撤销命令的受理时间**终止该告警的后续播发：`stop_at = accepted_at`；
- **历史时刻仍可查询**（撤销不删除数据，只把逻辑结束时间提前）；
- 撤销时告警尚未到生效点，则该告警从不播发（历史区间本身为空）；
- 目标不存在 → **404**；已撤销 / 已自然到期 → **409**；
- 撤销命令自身同样遵循请求号幂等规则。

### 查询 `GET /api/v1/broadcasts`

查询参数（均可选）：

| 参数 | 说明 |
| --- | --- |
| `at` | ISO-8601 时刻，如 `2026-10-07T13:00:00+08:00` / `...Z`；缺省=服务端当前 UTC 时刻。无时区按 UTC 处理 |
| `area` | 仅查指定区域，可重复传入（`area=A&area=B`）或逗号分隔；缺省返回全部区域 |

同一区域同一时刻有多条有效告警时，裁决顺序为：

1. **级别高者优先**；
2. 级别相同 → **受理（发布）时间早者优先**；
3. 仍相同 → **告警号小者优先**。

高级别到期后低级别无需任何状态翻转即**自动恢复**——查询结果完全由时刻推导。
没有有效告警时 `broadcasts` 返回空数组。

### 统一错误格式

```json
{ "error": { "code": "conflict", "message": "……" } }
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 404 | `target_not_found` | 撤销目标告警不存在 |
| 409 | `conflict` | 请求号载荷冲突 / 告警号复用 / 重复撤销 / 并发唯一约束冲突 |
| 422 | `validation_error` | 请求字段不满足约束 |
| 503 | `not_ready` | 就绪探针检测数据库不可用 |

---

## 4. 调用示例

仓库自带两套可直接运行的示例（服务已启动时执行）：

```bash
# cURL 版（逐步演示发布/重试/冲突/查询/撤销/非法载荷）
BASE=http://localhost:8080 bash examples/curl_examples.sh

# Python 版（仅标准库，无需 pip 安装）
python examples/python_examples.py
```

### cURL 快速示例

发布：

```bash
curl -sS -X POST http://localhost:8080/api/v1/commands/publish \
  -H 'Content-Type: application/json' \
  -d '{
    "request_no": "REQ-20261007-001",
    "alert_no": 10001,
    "areas": ["东门", "南门", "中央广场"],
    "level": 4,
    "content": "发现火情，请附近人员沿指示牌有序撤离",
    "delay_seconds": 0,
    "duration_seconds": 1800
  }'
```

成功回执（201）：

```json
{
  "status": "accepted",
  "command_type": "publish",
  "request_no": "REQ-20261007-001",
  "alert_no": 10001,
  "areas": ["东门", "南门", "中央广场"],
  "level": 4,
  "content": "发现火情，请附近人员沿指示牌有序撤离",
  "delay_seconds": 0,
  "duration_seconds": 1800,
  "accepted_at": "2026-10-07T05:00:00.123456Z",
  "effective_at": "2026-10-07T05:00:00.123456Z",
  "expire_at": "2026-10-07T05:30:00.123456Z"
}
```

查询指定时刻：

```bash
curl -sS --get http://localhost:8080/api/v1/broadcasts \
  --data-urlencode "at=2026-10-07T13:00:00+08:00" \
  --data-urlencode "area=东门"
```

```json
{
  "at": "2026-10-07T05:00:00+00:00",
  "broadcasts": [
    {
      "area": "东门",
      "alert_no": 10001,
      "level": 4,
      "content": "发现火情，请附近人员沿指示牌有序撤离",
      "effective_at": "2026-10-07T05:00:00.123456Z",
      "expire_at": "2026-10-07T05:30:00.123456Z",
      "published_at": "2026-10-07T05:00:00.123456Z"
    }
  ]
}
```

撤销：

```bash
curl -sS -X POST http://localhost:8080/api/v1/commands/revoke \
  -H 'Content-Type: application/json' \
  -d '{"request_no": "REQ-20261007-R1", "alert_no": 10001, "reason": "现场确认安全"}'
```

```json
{
  "status": "revoked",
  "command_type": "revoke",
  "request_no": "REQ-20261007-R1",
  "alert_no": 10001,
  "reason": "现场确认安全",
  "accepted_at": "2026-10-07T05:10:00.654321Z",
  "effective_at": "2026-10-07T05:00:00.123456Z",
  "stop_at": "2026-10-07T05:10:00.654321Z"
}
```

### Python（requests）快速示例

```python
import requests

base = "http://localhost:8080/api/v1"

requests.post(f"{base}/commands/publish", json={
    "request_no": "REQ-20261007-001",
    "alert_no": 10001,
    "areas": ["东门", "南门", "中央广场"],
    "level": 4,
    "content": "发现火情，请附近人员沿指示牌有序撤离",
    "delay_seconds": 0,
    "duration_seconds": 1800,
})

# 超时/失败时用完全相同的 request_no 与载荷重试即可，服务端返回首次回执
requests.get(f"{base}/broadcasts", params={"at": "2026-10-07T13:00:00+08:00",
                                           "area": ["东门", "南门"]})

requests.post(f"{base}/commands/revoke",
              json={"request_no": "REQ-20261007-R1", "alert_no": 10001})
```

---

## 5. 数据模型与一致性设计

- `command_records`：请求号主键幂等表，保存首次受理时间、规范化载荷的 SHA-256 与首次回执原文；
- `alerts`：告警主表，告警号主键（不可复用），含级别、文案、受理时间、计划生效/到期时间、撤销时间；
- `alert_areas`：告警—区域关联表，随告警在同一事务内写入 / 回滚，保证**区域同成同败**。

关键并发手段：

- 首次受理使用 `INSERT ... ON CONFLICT (request_no) DO NOTHING RETURNING` 抢占请求号，
  并发重试经 `SELECT ... FOR UPDATE` 读取首个提交者的回执；对端回滚时自动重试抢占；
- 告警号冲突在插入时由主键约束兜底（不存在的行无法用行锁串行化），统一转 409；
- 撤销对告警行 `FOR UPDATE` 后再更新终止时间；
- 裁决查询使用 PostgreSQL `DISTINCT ON (area)` + 固定排序
  （`level DESC, published_at ASC, alert_no ASC`）单条 SQL 完成，时间区间为半开区间
  且结束时间取 `LEAST(expire_at, COALESCE(revoked_at, expire_at))`。

### 时间语义

服务内部一律使用时区感知的 UTC 时间戳；接收查询参数时，不带时区的时间按 UTC 解释。
