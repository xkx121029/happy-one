# happy-one

个人自用（< 20 人）的统一枢纽：身份、授权、产物分发、许可证校验、移动端文件中转、打洞信令。happy-one 同时是其他自研产品的登录与下载入口。

- 控制面：happy-one（Python + FastAPI + SQLite WAL，单进程单机）
- 内容面：happy-hub（独立 Go 服务，只做内容寻址存储与签名直链，不存用户体系）
- 不引入 Redis、消息队列、微服务拆分；不向终端用户发放 hub 令牌
- 授权走标准 OIDC + PKCE（授权码 / device code / refresh token），不自造私有协议

## 当前状态

| 模块 | 状态 |
| --- | --- |
| 地基：配置、SQLite 迁移、插件挂载、健康检查、统一错误包络 | 已交付 |
| 身份：用户、密码（scrypt）、第一方会话 | 已交付 |
| 授权：OIDC 发现、JWKS、授权码 + PKCE、device code、refresh、revoke、userinfo | 已交付 |
| 审计：只追加事件流与管理员查询 | 已交付 |
| 许可证 `/license/*`：Ed25519 签发、校验、吊销、公钥与吊销快照 | 已交付 |
| 存储接入 `/storage/*`：上传、映射、现签短链 302（依赖 happy-hub） | 已交付 |
| 通道 `/channel/*`、`/ws/signal`：设备配对与 SDP/ICE 信令（不含中转文件） | 已交付 |
| 人机验证 `/captcha/v1/*`：图形顺序挑战、一次性票据 | 已交付 |

接口契约见 [docs/API.md](docs/API.md)，分层与并行开发约定见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

要求 Python 3.11+（开发环境验证于 3.13）。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# 1. 建库（执行 migrations/001_core.sql 及各业务线迁移）
.\.venv\Scripts\python.exe -m happyone.cli migrate

# 2. 创建管理员（不传 --password 则交互输入，避免留在命令历史）
.\.venv\Scripts\python.exe -m happyone.cli create-user --username admin --admin

# 3. 启动（工厂模式，避免 import 时产生数据目录副作用）
.\.venv\Scripts\python.exe -m uvicorn happyone.app:create_app --factory --host 127.0.0.1 --port 8787
```

`cryptography` 是唯一带二进制扩展的运行时依赖（Ed25519 签名，`cryptography>=42.0`，开发环境实测 50.0.2）。`pip install -e ".[dev]"` 会自动装好；若网络受限，可单独安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "cryptography>=42.0"
.\.venv\Scripts\python.exe -c "import cryptography; print(cryptography.__version__)"
```

### 最小配置：一条命令起来

启动时自动执行未应用的迁移，因此 `pip install -e .` 后只需设置两三个环境变量：

```powershell
$env:HAPPYONE_DATA_DIR = "./data"
$env:HAPPYONE_ISSUER   = "http://127.0.0.1:8787"
$env:HAPPYONE_BOOTSTRAP_ADMIN_USERNAME = "admin"   # 仅用户表为空时生效，建好后删掉
$env:HAPPYONE_BOOTSTRAP_ADMIN_PASSWORD = "换成强密码"
.\.venv\Scripts\python.exe -m uvicorn happyone.app:create_app --factory --host 127.0.0.1 --port 8787
```

默认监听 `127.0.0.1:8787`；数据目录默认 `./data`，其中 `happyone.db` 为 SQLite（WAL 模式），`keys/` 存放自动生成的 OIDC、许可证、人机验证密钥。

健康检查：

```powershell
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/readyz
```

登录并访问受保护接口：

```powershell
$body = @{ username = "admin"; password = "你的密码" } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8787/api/v1/auth/login -ContentType "application/json" -Body $body
Invoke-RestMethod -Uri http://127.0.0.1:8787/api/v1/me -Headers @{ Authorization = "Bearer $($login.token)" }
```

## 配置

复制 `.env.example` 为 `.env` 后按需修改；环境变量优先于 `.env`，`.env` 优先于默认值。所有密钥只从环境变量或本机文件读取，不写仓库、不回显。

关键项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HAPPYONE_HOST` / `HAPPYONE_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `HAPPYONE_ISSUER` | `http://127.0.0.1:8787` | OIDC issuer，必须与客户端配置一致 |
| `HAPPYONE_DATA_DIR` | `./data` | 数据目录 |
| `HAPPYONE_HUB_BASE_URL` | 空 | happy-hub 地址（存储线使用） |
| `HAPPYONE_HUB_TOKEN` | 空 | hub 应用令牌，禁止下发客户端 |
| `HAPPYONE_CAPTCHA_SECRET` | 自动生成 | 人机验证密钥；未配置时持久化到 `<data_dir>/keys/captcha_secret` |
| `HAPPYONE_LICENSE_KEY_PATH` | `<data_dir>/keys/license_ed25519.pem` | 许可证签名私钥，须与 hub `.secret`、OIDC 密钥分离 |

首次启动若配置 `HAPPYONE_BOOTSTRAP_ADMIN_USERNAME` / `HAPPYONE_BOOTSTRAP_ADMIN_PASSWORD` 且用户表为空，会自动创建管理员；创建后建议删除这两项。

## 目录结构

```
src/happyone/
├─ app.py config.py db.py deps.py cli.py   # 地基
├─ core/                                   # 错误、ID、时间、密码学、插件挂载
├─ identity/ authz/ audit/                 # 地基三线
├─ license/ storage/ channel/ captcha/     # 四条业务线（各自独立交付）
└─ migrations/001_core.sql + 002..005      # 按编号顺序执行
tests/
├─ core/ ...                               # 各线自己的测试目录
```

并行开发约定：

- 每条业务线在自己的包内提供 `router.py`（模块级 `router: APIRouter`）、`service.py`（纯逻辑，不 import FastAPI）、`models.py`。
- 可选提供 `deps.configure(*, database, settings)`；挂载器会在 `include_router` 前调用。
- 也可直接使用兼容入口：`from happyone.config import settings`、`from happyone.db import get_conn`。
- 包缺失、导入失败、注入失败、路由挂载失败都会记录日志并跳过，不影响内核与其他线启动。

## 安全边界

- 密码只存 scrypt 摘要；会话、授权码、设备码、access / refresh token 只存 sha256。
- OIDC 仅支持公开客户端 + PKCE(S256)；id_token 使用 Ed25519(EdDSA)，公钥通过 JWKS 暴露。
- 下载短链有效期限制在 60–900 秒，happy-one 不代理整文件流（存储线落实）。
- 许可证私钥、OIDC 私钥、hub `.secret` 三者完全分离。
- 中转文件有大小上限、TTL 到期即清，与长期产物库物理分开；当前由存储线 `relay` 命名空间承担，通道线只做信令。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## 许可

[MIT](LICENSE)
