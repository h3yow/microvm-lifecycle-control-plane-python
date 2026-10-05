# 本地 microVM 实例生命周期控制面（Python）

用**本地进程**模拟 microVM 的创建、运行与销毁，提供实例生命周期控制面。
目标是在**并发请求、有限配额、失败回滚**下，实例状态始终一致、可解释。

- Web 框架：FastAPI（内存态，无外部依赖）
- VM 模拟：每个运行态实例对应一个真实的长睡眠子进程（独立进程组），
  停止/删除时 `SIGKILL` 进程组并 `wait()` 回收，无僵尸进程
- 测试：pytest（并发、幂等重放、容量耗尽与回滚、越权访问）

---

## 1. 实例状态机

```
                 create（先占配额，成功后）
   (无) ───────────────────────────────► STOPPED ──start──► RUNNING
      │                                   ▲  ▲              │  │
      │ 创建在占配额后失败：回滚并移除      │  └────stop──────┘  │
      └──────────────────────────────✗    └────delete──✗      restart（原地）
                                           （删除即释放配额+移除记录）
   RUNNING ──start/stop/restart 失败──► FAILED
   FAILED  ──start（重新占配额）──────► RUNNING
   FAILED  ──delete（不持配额）──────► (记录移除)
```

| 状态 | 含义 | 是否持有配额 | 是否有存活进程 |
| --- | --- | --- | --- |
| `pending` | 操作进行中的**瞬时态**（持每实例锁期间可见） | 视阶段而定 | 视阶段而定 |
| `stopped` | 已创建/已关机 | 是 | 否 |
| `running` | 模拟进程存活 | 是 | 是 |
| `failed` | 启动/重启失败后的稳定终态 | 否 | 否 |
| `deleted` | 已销毁，记录已从注册表移除 | 否 | 否 |

允许的发起状态（非法迁移返回 `409 invalid_state_transition`）：

| 操作 | 允许的当前状态 |
| --- | --- |
| `start` | `stopped`、`failed` |
| `stop` | `running` |
| `restart` | `running` |
| `resize` | `stopped`（**原地调整规格，不重建实例**） |
| `delete` | `stopped`、`running`、`failed` |

### 实例规格调整（resize）

在**不重建实例**的前提下调整 CPU 核数与内存：

- **身份与归属不变**：`instance_id`、`tenant_id`、`created_at` 均保持原值，
  调整后仍是同一个实例；实例状态全程保持 `stopped`（仅允许对**已创建但
  未运行**的实例调整；对 `running` 实例调整返回 `409 invalid_state_transition`，
  先 `stop` 再调即可）。
- **原子账本变更**：在资源账本的同一把锁内完成"旧占用释放 → 容量检查 →
  新占用计入"。调大后超出主机剩余容量时**整体拒绝**（`507 capacity_exhausted`，
  与其他失败原因可区分），不会只调一半；拒绝后实例的状态、规格与配额占用
  与提交前**完全一致**，仍可继续调整或删除。
- **容量视图准确**：调整成功后 `GET /v1/admin/capacity` 立即按新规格反映
  已用/剩余容量；调小释放出的容量立即可被其他实例使用。
- **与其他操作同队列**：resize 与 start/stop/restart/delete 使用同一把
  "每实例锁"。同一实例上已有操作进行中时，resize（或其反向碰撞）立即得到
  `409 operation_conflict`，两个操作不会互相覆盖。
- **幂等**：支持 `X-Idempotency-Key`。同键同体重放返回首次结果，不重复
  扣减/归还容量；同键不同体返回 `409 idempotency_conflict`。首次容量不足
  的失败也会被记录，同键重放得到相同失败；待其他实例释放容量后，用**新的
  请求**（不带旧键或用新键）重试即可成功。

### 失败回滚（不残留半成品）

- **create**：先在资源锁内原子扣减配额，再登记实例；其后任一步失败都会
  **归还配额 + 移除实例记录**（`fault_inject=true` 可注入该失败路径）。
- **start**：`failed` 重试时先占配额；进程拉起失败则**归还配额、落 `failed`、
  不留进程**，可再次 `start` 或 `delete`。
- **restart**：先回收旧进程再拉新进程；新进程失败时同样释放配额并落 `failed`。
- **delete**：回收进程，仅当实例此前持有配额时**恰好归还一次**，然后移除记录。

---

## 2. 配额规则

- 单机固定容量：默认 **4 vCPU / 1024 MB**（见 `microvm/api.py::build_default_service`）。
- 创建实例时申请 `cpus`、`memory_mb`，二者都必须为正整数。
- “容量检查 + 扣减”在**资源账本锁内原子完成**：并发创建下已用容量永远
  不会超过总量，**绝不超卖**；不足时返回 `507 capacity_exhausted`，
  错误信息中给出申请量与剩余量（可解释）。
- CPU 与内存是两个独立维度，任一维度不足即拒绝。
- `stopped` 实例继续占用配额（关机不等于删除）；`failed` 不占配额。
- 删除实例或启动失败回滚时释放配额。

### 并发与串行化

- **同一实例**的操作由“每实例锁”串行化；锁以**非阻塞**方式获取，
  抢不到立即返回 `409 operation_conflict`（与非法迁移 `invalid_state_transition`
  是两个可区分的失败码）。
- **不同实例**的创建在资源账本锁上短暂串行，保证账本一致。
- **同一幂等键**的并发提交由“每键锁”串行：首个请求执行，其余重放首次结果。

---

## 3. 鉴权模型

所有接口要求 `Authorization: Bearer <token>`。骨架内置测试账号：

| Token | 租户 | 角色 |
| --- | --- | --- |
| `token-tenant-a` | `tenant-a` | 租户 |
| `token-tenant-b` | `tenant-b` | 租户 |
| `token-admin` | `admin` | 管理员 |

- **租户隔离**：普通租户只能操作自身实例。访问/操作**他人**实例一律返回
  `404 instance_not_found`，与“实例不存在”不可区分，**不泄露其他租户实例
  是否存在**；列表接口按租户过滤。
- **管理级操作**：`GET /v1/admin/capacity` 仅管理员可用，普通租户返回
  `403 forbidden`（与越权访问的 404 明确区分）。管理员可操作任意租户实例。
- 缺少/无效凭据返回 `401 unauthorized`。

### 幂等

- 变更类请求可携带 `X-Idempotency-Key` 头。
- 相同键 + 相同请求体：重复提交返回**首次结果**，不重复创建实例、不重复扣减。
- 相同键 + **不同请求体**：返回 `409 idempotency_conflict`。
- 首次失败也会被记录，重放返回相同错误码，不会“重试成功”而产生副作用。
- 幂等作用域含租户身份，不同租户使用同一键互不影响。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/instances` | 创建实例（可带 `X-Idempotency-Key`） |
| POST | `/v1/instances/{id}/start` | 启动 |
| POST | `/v1/instances/{id}/stop` | 停止 |
| POST | `/v1/instances/{id}/restart` | 重启 |
| POST | `/v1/instances/{id}/resize` | 原地调整规格（请求体 `{"cpus":N,"memory_mb":N}`，可带幂等键） |
| POST | `/v1/instances/{id}/delete` | 删除 |
| GET | `/v1/instances/{id}` | 查询单个实例 |
| GET | `/v1/instances` | 列举本租户实例 |
| GET | `/v1/admin/capacity` | 全局容量视图（管理员） |
| GET | `/healthz` | 健康检查 |

错误响应统一结构：

```json
{ "error": { "code": "capacity_exhausted", "message": "容量不足：申请 ... 剩余 ..." } }
```

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 400 | `invalid_request` | 参数非法（空名称、非正配额） |
| 401 | `unauthorized` | 缺少/无效凭据 |
| 403 | `forbidden` | 非管理员调用管理级接口 |
| 404 | `instance_not_found` | 不存在，或属于其他租户（不泄露） |
| 409 | `invalid_state_transition` | 当前状态不允许该操作 |
| 409 | `operation_conflict` | 同一实例上已有操作在执行 |
| 409 | `idempotency_conflict` | 幂等键绑定了不同请求体 |
| 507 | `capacity_exhausted` | 主机剩余容量不足 |
| 500 | `hypervisor_error` | 本地模拟进程层失败 |

---

## 5. 本地验证方法

需要 Python 3.14+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装依赖（含 pytest）
uv sync --group dev

# 运行全部单测（-s 打印“请求输入 + 判定依据”日志）
uv run pytest -s

# 按主题运行
uv run python -m pytest tests/test_concurrency.py -s   # 并发提交 / 同实例串行化
uv run python -m pytest tests/test_idempotency.py -s   # 幂等重放 / 不重复扣减
uv run python -m pytest tests/test_capacity.py -s      # 容量耗尽 / 失败回滚无残留
uv run python -m pytest tests/test_authz.py -s         # 越权访问 / 管理操作
uv run python -m pytest tests/test_state_machine.py -s # 非法状态迁移
uv run python -m pytest tests/test_resize.py -s        # 规格调整：成功/容量拒绝/并发冲突/幂等/恢复
uv run python -m pytest tests/test_api.py -s           # HTTP 端到端
```

单测中每条用例都会通过 `print` 与 `logging` 输出**请求输入**与**判定依据**
（例如“剩余 cpus=X mem=Y 不足以容纳申请 -> 拒绝，未超卖”）。

### 手动 curl 验证

```bash
uv run uvicorn main:app --host 127.0.0.1 --port 8000

# 创建（带幂等键，重复执行两次得到同一实例）
curl -s -X POST localhost:8000/v1/instances \
  -H 'Authorization: Bearer token-tenant-a' \
  -H 'X-Idempotency-Key: demo-1' -H 'Content-Type: application/json' \
  -d '{"name":"web","cpus":1,"memory_mb":256}'

# 启动 / 重启 / 停止 / 删除（把 $ID 换成返回的 instance_id）
curl -s -X POST localhost:8000/v1/instances/$ID/start  -H 'Authorization: Bearer token-tenant-a'
curl -s -X POST localhost:8000/v1/instances/$ID/restart -H 'Authorization: Bearer token-tenant-a'
curl -s -X POST localhost:8000/v1/instances/$ID/stop   -H 'Authorization: Bearer token-tenant-a'

# 原地调整规格（仅 stopped 实例；同键重放不重复计账）
curl -s -X POST localhost:8000/v1/instances/$ID/resize \
  -H 'Authorization: Bearer token-tenant-a' \
  -H 'X-Idempotency-Key: resize-1' -H 'Content-Type: application/json' \
  -d '{"cpus":2,"memory_mb":512}'

curl -s -X POST localhost:8000/v1/instances/$ID/delete -H 'Authorization: Bearer token-tenant-a'

# 容量视图（仅管理员）
curl -s localhost:8000/v1/admin/capacity -H 'Authorization: Bearer token-admin'
```

### 代码地图

| 文件 | 职责 |
| --- | --- |
| `microvm/states.py` | 状态枚举、合法迁移表、操作允许的源状态 |
| `microvm/errors.py` | 带稳定 code 与 HTTP 状态的错误类型 |
| `microvm/resources.py` | 全局容量账本，原子检查+扣减（`try_resize` 支持原子调规格），拒绝超卖 |
| `microvm/store.py` | 实例注册表 + 每实例锁（冲突串行化） |
| `microvm/idempotency.py` | 幂等键记录 + 每键锁（重放首次结果） |
| `microvm/hypervisor.py` | 本地子进程模拟 VM 运行/回收 + 故障注入 |
| `microvm/auth.py` | Bearer 鉴权、租户隔离、管理员判定 |
| `microvm/service.py` | 生命周期编排：状态机+配额+回滚的唯一事务边界（含 `resize`） |
| `microvm/api.py` | FastAPI 路由、错误码到 HTTP 的映射、请求日志 |

---

## 6. 兼容性说明

本次仅**新增**能力，原有接口与状态语义保持不变：

- 原有 `create/start/stop/restart/delete` 的路径、请求/响应结构、错误码
  与状态机语义**完全不变**；resize 是新增的 `POST /v1/instances/{id}/resize`，
  不占用也不改变既有动作名。
- 实例的五个状态（`pending/stopped/running/failed/deleted`）含义不变；
  resize 不引入新状态，调整期间实例稳定停留在 `stopped`。
- resize 复用既有的每实例锁、幂等存储、错误码体系与统一错误响应结构，
  对未携带幂等键的调用方无任何额外要求。
- 失败后的可恢复承诺对所有操作一致：任何操作失败，实例都停在一个可解释、
  可继续处理的稳定态（`stopped/running/failed` 或记录已移除），可重试或
  删除；容量账本始终与真正持配额的实例一一对应。
