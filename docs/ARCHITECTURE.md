# happy-one 架构设计

> 版本：0.1（地基定稿）
> 适用范围：happy-one 内核与契约层。本文档定义分层、依赖方向、状态归属、失败隔离边界与并行开发纪律。

## 1. 定位与非目标

happy-one 是个人自用（< 20 人）的统一枢纽，承担六件事：

1. 身份：用户、密码、会话、第一方 API 令牌。
2. 授权：标准 OIDC / OAuth2 授权服务器（PKCE、device code、refresh token），是其他自研产品的登录入口。
3. 产物分发：维护「业务实体 ↔ hub object_id ↔ sha256」映射，下载时现签短链并 302。
4. 许可证：Ed25519 签发、吊销、离线宽限（**许可证线实现**）。
5. 通道层：设备配对取件码与 WebRTC 打洞信令（**通道信令线实现**，不含中转文件）。
6. 审计与 API：统一错误包络、请求追踪、审计事件。

非目标（明确不做）：

- 不做多租户、组织、计费、第三方注册（DCR）、邮件服务。
- 不引入 Redis、消息队列、微服务拆分、分布式锁、多副本部署。
- 不内嵌前端、管理面板、移动端 App。
- 不改造 happy-hub（Go 服务）的架构；happy-one 是控制面，hub 是内容面。
- 不向终端用户发放 hub 应用令牌；hub 的 `HUB_TOKEN` 只存在于 happy-one 进程环境。

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ API 层（FastAPI 路由）                                        │
│  identity/router.py  authz/router.py  audit/router.py         │
│  license/router.py   storage/router.py                        │
│  channel/router.py   captcha/router.py                        │
│  职责：协议解析、鉴权依赖、状态码、错误包络                      │
└───────────────────────────┬──────────────────────────────────┘
                            │ 只允许向下调用
┌───────────────────────────▼──────────────────────────────────┐
│ 业务服务层（纯 Python，不 import fastapi）                     │
│  identity/service.py  authz/service.py  audit/service.py       │
│  license/service.py   storage/service.py                      │
│  channel/service.py   captcha/service.py                      │
│  职责：领域规则、事务边界、跨表不变量                           │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 仓储 / 适配层                                                 │
│  <line>/repository.py   storage/hub_client.py（仅存储线）      │
│  职责：SQL 与外部 HTTP，不含业务判断                            │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ 内核共享（地基线独占）                                         │
│  config.py  db.py  core/errors.py  core/security.py            │
│  core/ids.py  core/timeutil.py  core/plugins.py               │
│  migrations/001_core.sql                                       │
└───────────────────────────┬──────────────────────────────────┘
                            │
              SQLite(WAL) / 本地文件 / happy-hub HTTP
```

### 2.1 依赖方向（硬约束）

| 允许 | 禁止 |
| --- | --- |
| router → service / repository | service → fastapi、starlette、Request/Response |
| service → repository → db.py | repository → service（回环） |
| 任意线 → `audit.service.record`（审计汇） | license / storage / channel / captcha 之间互相 import service |
| 任意线 → `identity.deps`（鉴权依赖） | 内核 `core/` / `config.py` / `db.py` import 任何业务线 |
| 存储线 service → hub_client（外部适配） | 其他线直连 happy-hub |
| 路由层跨线编排（如先鉴权再调存储） | 跨线共享数据库事务 |

`core/` 只放无业务语义的通用件：错误类型、令牌与密码学工具、ID、时间、插件扫描。审计是唯一的跨线写入汇（sink），允许所有线调用；其余跨线能力一律通过 router 层编排。

### 2.2 业务线插件契约

每个业务线包（`license/`、`storage/`、`channel/`、`captcha/`）必须提供：

| 文件 | 内容 | 约束 |
| --- | --- | --- |
| `router.py` | 模块级 `router: APIRouter` | 路径自带完整前缀（如 `/api/v1/licenses`），不依赖外部再加 prefix |
| `service.py` | 纯逻辑 | 不 import fastapi / starlette；不直接建 HTTP 响应 |
| `models.py` | 数据结构 | dataclass 或 pydantic 模型，不得依赖 router |

可选：`repository.py`、`deps.py`、`hub_client.py` 等内部实现文件。

挂载器（`core/plugins.py`）行为：

1. 依次尝试导入 `happyone.license.router`、`happyone.storage.router`、`happyone.channel.router`、`happyone.captcha.router`。
2. 包不存在 → 记 INFO 日志并跳过。
3. 导入抛异常或缺少模块级 `router` → 记 ERROR 日志（带堆栈）并跳过，**不阻断应用启动**。
4. 成功 → `app.include_router(router)`，并登记到 `/readyz` 与 `/api/v1/meta` 的 `plugins` 字段。
5. 内核三线（identity / authz / audit）**静态挂载**，不经过插件扫描，保证核心功能不依赖别人的代码状态。

启动日志必须出现每个包的 `loaded` 或 `skipped` 结论，便于并行开发时确认。

### 2.3 依赖注入兼容层（并行开发约定）

四条业务线已经落地，但它们最初无法预知地基的实例化方式，因此地基提供**两种等价入口**，任选其一即可，不得再各自猜测：

| 入口 | 形式 | 生命周期 |
| --- | --- | --- |
| 模块级配置 | `from happyone.config import settings` | 应用启动 / CLI 初始化时由地基绑定为当前 `Settings`；未绑定时按 `.env` 惰性加载 |
| 模块级连接工厂 | `from happyone.db import get_conn` | 返回绑定到当前 `Database` 的新连接，调用方负责 `close()` |
| 显式注入（可选） | `deps.configure(*, database, settings)` | 挂载器在 `include_router` 前调用，参数为当前实例；导入失败或不接受该签名则跳过该线 |

挂载顺序：导入 `router` → 导入 `deps`（若有）→ 调用 `configure` → `include_router`。任一步失败都只影响该业务线，记 ERROR 日志并标记 `skipped`，内核与其余业务线照常启动。

`core/` 与 `config.py` / `db.py` 不反向依赖业务线；`deps.py`（应用根目录）是唯一的装配层。

**密钥持久化**：`captcha_secret` 优先取环境变量 `HAPPYONE_CAPTCHA_SECRET`（兼容 `CAPTCHA_SECRET`）；未配置时读取 `<data_dir>/keys/captcha_secret`，缺失则生成并 0600 落盘。这样进程重启后未核销票据仍可验证。密钥只存在于配置对象（`SecretStr`）与注入参数中，不写日志、不进仓库。

**已知契约差异（由各线后续对齐，地基不代改）**：

| 线 | 实际前缀 | 原文档占位前缀 | 实际错误格式 |
| --- | --- | --- | --- |
| 许可证 | `/license/*` | `/api/v1/licenses/*` | `{"ok": false, "code": "LICENSE_*", "error"}` |
| 存储 | `/storage/*` | `/api/v1/artifacts/*` | `{"ok": false, "code": "ST-*", "error"}` |
| 通道 | `/channel/*`、`/ws/signal` | `/api/v1/relay/*`、`/ws/signaling` | `{"ok": false, "code", "error"}` |
| 人机验证 | `/captcha/v1/*` | `/api/v1/captcha/*` | `{"ok": false, "code", "error"}` |

四条线已全部交付，`docs/API.md` 已按实际实现改写，占位前缀只作历史对照保留。地基挂载器仍不校验路径与包络，避免后续调整时阻断；本表以代码为准，改代码须同步改 API.md。


## 3. 状态归属

### 3.1 持久化（SQLite WAL：`data/happyone.db`）

| 表 | 归属 | 说明 |
| --- | --- | --- |
| `schema_migrations` | 地基 | 已执行迁移版本与校验和 |
| `users` | 身份线 | 账号、scrypt 密码摘要、状态、管理员标记 |
| `sessions` | 身份线 | 第一方会话令牌的 sha256 摘要，非明文 |
| `oidc_clients` | 授权线 | 客户端、类型、回调地址、允许 scope |
| `oidc_auth_codes` | 授权线 | 授权码摘要、PKCE challenge、一次性消费 |
| `oidc_device_codes` | 授权线 | 设备码摘要、用户码、状态机 |
| `oidc_tokens` | 授权线 | access / refresh 令牌摘要与吊销状态 |
| `audit_events` | 审计线 | 只追加事件流 |
| 其余表 | 各业务线 | 通过 002–005 迁移建立，地基不代建 |

### 3.2 持久化（文件）

| 路径 | 归属 | 说明 |
| --- | --- | --- |
| `data/happyone.db`（含 `-wal`/`-shm`） | 地基 | 运行期数据库 |
| `data/keys/oidc_ed25519.pem` | 授权线 | OIDC 签名私钥，首次启动生成，权限 0600（Windows 尽力而为） |
| 许可证私钥 | 许可证线 | 与 OIDC 密钥、hub `data/.secret` 完全分离 |
| 中转临时文件 | 未实现 | 通道线只做配对与信令；中转文件归存储线 `relay` 命名空间或后续阶段，TTL 到期即删 |

### 3.3 只存在内存

- 进程配置（`Settings`）、插件登记表、OIDC 密钥对象与 `kid`。
- 每个请求的 `request_id`、连接、事务。
- 通道线的取件码配对表、WebSocket 连接与房间（本进程内；重启即失效，符合「过路货」语义）。
- 预留的登录失败计数 / 限流计数（本次未实现，落地时必须内存态 + 重启清零）。

### 3.4 绝不持久化

- 明文密码、明文令牌（只存 sha256 摘要）。
- `HUB_TOKEN`、许可证私钥、OIDC 私钥进入仓库或日志。
- 设备码、授权码在消费后不再可复用；过期记录由启动清理任务删除。

## 4. 失败隔离边界

| 边界 | 行为 | 理由 |
| --- | --- | --- |
| 插件导入失败 | 跳过 + ERROR 日志，`/healthz` 仍 200，`/readyz` 标记该插件 `skipped` | 别人的代码不能拖垮登录 |
| 迁移失败 | 启动中止，不提供服务 | 半套 schema 比停服更危险（fail-closed） |
| SQLite 锁等待 | `busy_timeout=5000`，超时返回 503 `E_UNAVAILABLE` | 单机单进程，冲突应极少 |
| 审计写入失败 | 记录 ERROR 日志 + 继续主流程，响应中不体现 | 审计不能反向阻断登录；严格模式留作后续 |
| hub 不可达/超时 | 仅存储线接口返回 503 `ST-UNAVAILABLE` | 内容面故障不影响身份与授权 |
| 信令 WebSocket 异常 | 连接级关闭，不影响 HTTP 路由与其他 WS | 打洞是增强能力，不是登录前置 |
| 许可证验签异常 | 仅许可证接口报错，公钥与私钥均不参与核心启动 | 密钥缺失应显式暴露，不静默降级 |
| 单个请求异常 | 500 `E_INTERNAL`，日志带 `request_id`，不外泄堆栈 | 统一包络 |

## 5. 安全边界

- 令牌：第一方会话、授权码、设备码、access/refresh token 一律随机生成，库里只存 sha256；比较使用常量时间。
- 密码：`hashlib.scrypt(n=2^15, r=8, p=1)`，格式 `scrypt$n$r$p$salt_b64$hash_b64`，校验用 `compare_digest`。
- OIDC：仅公开客户端 + PKCE(S256)；`id_token` 使用 EdDSA(Ed25519)，`kid` 由公钥指纹派生，`/.well-known/jwks.json` 暴露公钥；access token 为不透明令牌（库内可吊销）。
- 密钥分离：OIDC 签名密钥、许可证签名密钥、hub `.secret` 三者互不复用；环境变量优先于文件。
- 下载：happy-one 现签 60–900 秒短链后 302；hub 地址与令牌不出进程（存储线落实）。
- 审计：登录成功/失败、登出、用户变更、客户端变更、授权码/设备码授予、令牌吊销必须落审计。

## 6. 并发与运行模型

- 单进程、单机、多线程（uvicorn 默认线程池处理同步路由）；SQLite 开启 WAL。
- 每个请求独立连接，事务用 `BEGIN IMMEDIATE` 仅用于「读-改-写」竞态点（授权码消费、设备码轮询、令牌轮换）。
- 不使用后台线程/定时器；过期数据在启动时清理，运行期按需惰性清理。
- 20 人规模下不做连接池；如后续需要，在 `db.py` 内替换实现，不改仓储接口。

## 7. 目录与并行归属

```
e:\happy_one\
├─ pyproject.toml / README.md / LICENSE / docs/          ← 地基线
├─ src/happyone/
│  ├─ __init__.py app.py config.py db.py deps.py cli.py    ← 地基线
│  ├─ core/                                              ← 地基线
│  ├─ identity/  authz/  audit/                          ← 地基线
│  ├─ license/                                           ← 许可证线
│  ├─ storage/                                           ← 存储接入线
│  ├─ channel/                                           ← 通道信令线
│  ├─ captcha/                                           ← 人机验证线
│  └─ migrations/001_core.sql(地基) 002_license.sql 003_storage.sql
│     004_channel.sql 005_captcha.sql(各线自建)
└─ tests/core/  tests/license/  tests/storage/  tests/channel/  tests/captcha/
```

- 迁移文件按文件名编号顺序执行，已执行版本写入 `schema_migrations`，并校验 sha256；改动已执行文件会被拒绝启动，只能追加新文件。
- 地基线不预建 002–005 空文件；各线自行新增，编号不得重复。
- 地基线提交时按路径显式指定，不使用 `git add -A`。

## 8. 关键取舍

1. **OIDC 用 EdDSA 而非 RS256**：单机自用，Ed25519 密钥小、签名快、无 RSA 依赖；客户端内置公钥即可离线验签。
2. **access token 不透明而非 JWT**：可即时吊销，符合「许可证式控制」；`id_token` 仍为 JWT，供客户端识别身份。
3. **不引入 PyJWT/pydantic-settings 等额外依赖**：JWS 编解码与配置加载在 `core/` 内实现（约 200 行），减少供应链面。
4. **审计失败不阻断主流程**：个人自用优先可用性；严格模式（审计失败即拒绝）留作配置项后续补充。
5. **插件容错启动**：并行开发期间每条线可独立演进，任何一条未就绪不影响内核与端到端链路验证。
6. **hub 只作为内容面**：happy-one 保存映射与授权，不代理整文件流；下载 302 到短链，避免 happy-one 成为带宽瓶颈。
