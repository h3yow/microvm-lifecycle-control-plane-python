# microVM 生命周期控制面（本地模拟）

用本地进程模拟 microVM 的创建、运行与销毁，提供一套具备并发一致性、
配额约束、失败回滚与多租户隔离的实例生命周期控制面。

## 架构

```
main.py            入口：装配配额、运行时与服务，启动 HTTP 服务
app/
  errors.py        可区分的失败原因（ErrorCode）与统一异常
  models.py        实例状态机、迁移表、配额/占用数据模型
  auth.py          鉴权模型：token -> Caller(tenant, role)
  quota.py         租户配额管理：检查与扣减同锁完成，不超卖
  runtime.py       本地运行时：以子进程模拟 microVM
  service.py       控制面核心：幂等创建、状态迁移、串行化与回滚
  api.py           FastAPI HTTP 层
tests/             并发、幂等、配额、越权、回滚测试
```

## 实例状态机

```
                 create
                   │
                   ▼
        ┌──────► STOPPED ◄──────┐
        │          │            │
        │ delete   │ start      │ stop
        │          ▼            │
        │       RUNNING ────────┘
        │          │
        │          │ restart（原地：停旧进程、起新进程）
        ▼          ▼
     DELETED    （非法迁移一律拒绝）
```

合法迁移表（`app/models.py: TRANSITIONS`）：

| 动作    | 允许的源状态 | 目标状态 |
|---------|-------------|----------|
| start   | STOPPED     | RUNNING  |
| stop    | RUNNING     | STOPPED  |
| restart | RUNNING     | RUNNING  |
| delete  | STOPPED     | DELETED  |

非法迁移（如对 STOPPED 实例 stop、对 RUNNING 实例 delete）返回
`INVALID_STATE_TRANSITION`（HTTP 409），且不会污染实例状态。

## 一致性保证

- **幂等创建**：按 `(tenant_id, idempotency_key)` 去重。重复/并发提交同一
  key 返回同一实例，不重复扣减配额。
- **冲突串行化**：同一实例的所有变更操作由实例级锁串行执行；并发
  start/stop/delete 的终态必为合法状态，失败方得到可区分的
  `INVALID_STATE_TRANSITION` 或 `NOT_FOUND`。
- **失败回滚**：运行时启动失败时状态回滚到迁移前（如 start 失败回到
  STOPPED、不残留 pid）；restart 重新拉起失败时落回 STOPPED 并可重试。
  任何失败都不残留半成品实例或泄漏配额。
- **不超卖**：配额检查与扣减在同一把锁内完成；容量不足返回
  `QUOTA_EXCEEDED`（HTTP 429），不产生任何部分占用。

## 配额规则

每个租户有独立配额（`Quota`）：`max_instances` / `max_vcpu` /
`max_memory_mb`。创建实例时一次性检查三项，任一超出即整体拒绝；
删除实例时释放对应占用，释放后可被新实例复用。

## 鉴权模型

- 请求头 `Authorization: Bearer <token>` 鉴权；缺失或伪造 token 返回
  `UNAUTHENTICATED`（401），错误信息不暗示任何有效 token 的存在性。
- 内置演示 token：`token-tenant-a`、`token-tenant-b`（租户角色）、
  `token-admin`（管理员）。
- **租户隔离**：租户只能操作本租户实例。越权访问一律返回 `NOT_FOUND`
  （404），与"实例不存在"不可区分，不泄露其他租户实例的存在性。
- **管理级操作**：`GET /admin/instances`（`list_all`）仅管理员可用；
  租户调用返回 `FORBIDDEN`（403），与越权访问的 404 明确区分。

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST   | `/instances`                | 创建（body: name/vcpu/memory_mb/idempotency_key） |
| GET    | `/instances`                | 列出本租户实例 |
| GET    | `/instances/{id}`           | 查询单个实例 |
| POST   | `/instances/{id}/start`     | 启动 |
| POST   | `/instances/{id}/stop`      | 停止 |
| POST   | `/instances/{id}/restart`   | 重启 |
| DELETE | `/instances/{id}`           | 删除 |
| GET    | `/admin/instances`          | 列出全部实例（仅管理员） |

错误响应统一为 `{"error": {"code": ..., "message": ...}}`。

## 本地验证

```bash
# 运行全部测试（并发、幂等、配额耗尽、越权、失败回滚）
uv run pytest -q

# 查看测试日志中的请求输入与判定依据
uv run pytest -q --log-cli-level=INFO

# 启动服务并手动验证
uv run python main.py
curl -s -X POST localhost:8080/instances \
  -H 'Authorization: Bearer token-tenant-a' \
  -H 'Content-Type: application/json' \
  -d '{"name":"web","vcpu":1,"memory_mb":256,"idempotency_key":"k1"}'
```
