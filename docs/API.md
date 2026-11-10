# happy-one 接口契约

> 版本：0.1（契约定稿）
> 状态标记：`已实现` = 本轮骨架可跑通；`契约已定` = 只定协议，后续实现；`占位` = 由对应业务线补齐。

## 1. 通用约定

### 1.1 基础

- 默认地址：`http://127.0.0.1:8787`
- 业务接口前缀：`/api/v1`；OIDC 使用标准路径 `/oauth/*` 与 `/.well-known/*`
- 请求/响应：`application/json`，上传与 token 端点例外（见各节）
- 时间：ISO 8601 UTC，形如 `2026-11-10T12:00:00Z`
- 分页：`?limit=50&cursor=<opaque>`，响应 `{"items": [...], "next_cursor": null}`
- 请求追踪：响应头 `X-Request-ID`；客户端可传同名请求头，非法值将被替换

### 1.2 认证

| 令牌 | 前缀 | 用途 |
| --- | --- | --- |
| 第一方会话令牌 | `ho_sess_` | `/api/v1/*` 登录态；权限等同该用户全部 scope |
| OIDC access token | `ho_at_` | 第三方产品调用 `/oauth/userinfo` 与受 scope 保护的接口 |
| refresh token | `ho_rt_` | 仅用于 `/oauth/token` 刷新 |
| 授权码 / 设备码 | `ho_ac_` / `ho_dc_` | 一次性交换，不直接访问资源 |

请求头：`Authorization: Bearer <token>`。

### 1.3 成功响应

`2xx` 直接返回资源对象或 `204 No Content`，不额外包裹 `data`。列表统一 `{"items": [...], "next_cursor": ...}`。

### 1.4 错误包络（业务接口）

```json
{
  "ok": false,
  "error": {
    "code": "E_AUTH_INVALID_CREDENTIALS",
    "message": "用户名或密码不正确",
    "details": null
  },
  "request_id": "req_0123456789abcdef"
}
```

- `code` 机器可读，`message` 为中文可读文案，`details` 可为 `null` 或对象。
- **OIDC 端点例外**：`/oauth/*` 按 RFC 6749/6750 返回 `{"error":"invalid_grant","error_description":"..."}`，HTTP 状态见各节。
- **四条业务线例外**：许可证、存储、通道、人机验证使用扁平包络
  `{"ok": false, "code": "<线内错误码>", "error": "中文原因"}`，不带 `error` 子对象与 `request_id`。
  WebSocket 失败事件同样携带 `ok` / `code` / `error` 三个字段。各线错误码见对应章节。

### 1.5 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `E_VALIDATION` | 422 | 字段缺失或格式错误 |
| `E_UNAUTHORIZED` | 401 | 未携带令牌、令牌无效或过期 |
| `E_FORBIDDEN` | 403 | 权限不足（非管理员、scope 不足） |
| `E_NOT_FOUND` | 404 | 资源不存在 |
| `E_METHOD_NOT_ALLOWED` | 405 | 请求方法不被允许 |
| `E_CONFLICT` | 409 | 唯一约束冲突（用户名、邮箱已存在） |
| `E_RATE_LIMITED` | 429 | 触发限流（预留） |
| `E_UNAVAILABLE` | 503 | 数据库繁忙或依赖暂不可用 |
| `E_NOT_READY` | 503 | 迁移未完成或内核未就绪 |
| `E_INTERNAL` | 500 | 未预期异常（不返回堆栈） |

业务线错误码前缀实际形态：许可证 `LICENSE_*`（`/license/*`）、存储 `ST-*`（`/storage/*`）、通道小写短语（如 `code_expired`，`/channel/*`、`/ws/signal`）、人机验证小写短语（如 `ticket_expired`，`/captcha/v1/*`）。新增错误码必须登记在本表对应章节。

## 2. 健康与元信息（已实现）

### GET /healthz

存活探针，不查库。

```json
{ "status": "ok", "version": "0.1.0", "uptime_sec": 42 }
```

### GET /readyz

就绪探针：数据库可读、迁移已执行。就绪返回 200：

```json
{
  "status": "ready",
  "db": "ok",
  "migrations": 1,
  "plugins": { "license": "skipped", "storage": "skipped", "channel": "skipped", "captcha": "skipped" }
}
```

未就绪返回 503 + `E_NOT_READY` 包络。插件值为 `loaded` / `skipped`。

### GET /api/v1/meta

```json
{
  "name": "happy-one",
  "version": "0.1.0",
  "issuer": "http://127.0.0.1:8787",
  "plugins": ["identity", "authz", "audit"]
}
```

## 3. 身份与会话（已实现）

### POST /api/v1/auth/login

请求：

```json
{ "username": "alice", "password": "secret", "device_name": "Windows 桌面" }
```

`username` 支持用户名或邮箱；`device_name` 可选，写入会话记录。

响应 200：

```json
{
  "token": "ho_sess_...",
  "token_type": "Bearer",
  "expires_at": "2026-11-17T12:00:00Z",
  "user": {
    "id": "usr_...",
    "username": "alice",
    "email": "alice@example.com",
    "display_name": "Alice",
    "is_admin": false,
    "status": "active",
    "created_at": "2026-11-10T12:00:00Z"
  }
}
```

错误：`E_AUTH_INVALID_CREDENTIALS`（401，账号不存在或密码错误，不区分原因）、`E_AUTH_DISABLED`（403，账号停用）。

### POST /api/v1/auth/logout

需要 Bearer 会话令牌。注销当前会话，返回 `204`。重复注销幂等返回 `204`。

### POST /api/v1/auth/logout-all

需要 Bearer 令牌。吊销该用户全部会话，返回 `204`。

### GET /api/v1/me

需要 Bearer（会话令牌或带 `openid` scope 的 access token）。

```json
{
  "user": { "id": "usr_...", "username": "alice", "display_name": "Alice", "email": null, "is_admin": false, "status": "active", "created_at": "..." },
  "auth_method": "session",
  "scopes": ["openid", "profile", "email"]
}
```

`auth_method`：`session` | `oidc`。

### POST /api/v1/me/password

```json
{ "current_password": "old", "new_password": "new" }
```

成功 `204`；改密后吊销该用户其他会话（保留当前会话）。错误：`E_AUTH_INVALID_CREDENTIALS`。

### GET /api/v1/auth/sessions

当前用户会话列表：

```json
{
  "items": [
    { "id": "ses_...", "device_name": "Windows 桌面", "ip": "127.0.0.1", "created_at": "...", "expires_at": "...", "current": true }
  ],
  "next_cursor": null
}
```

### DELETE /api/v1/auth/sessions/{id}

吊销指定会话，`204`；不存在或不属于当前用户返回 `E_NOT_FOUND`。

## 4. 用户管理（管理员，已实现）

### POST /api/v1/users

```json
{ "username": "bob", "password": "secret", "email": "bob@example.com", "display_name": "Bob", "is_admin": false }
```

成功 201，返回用户对象（不含密码）。错误：`E_CONFLICT`（用户名/邮箱重复）、`E_VALIDATION`。

### GET /api/v1/users?limit=50&cursor=&q=

管理员。`q` 匹配用户名/邮箱/显示名子串。返回分页用户对象。

### GET /api/v1/users/{id}

管理员。返回单个用户，不存在为 `E_NOT_FOUND`。

### PATCH /api/v1/users/{id}

管理员。可改 `display_name`、`email`、`status`（`active`|`disabled`）、`is_admin`。成功返回更新后的用户对象。

### POST /api/v1/users/{id}/password

管理员重置密码：`{ "new_password": "..." }`，`204`，并吊销该用户全部会话。

## 5. 审计（已实现）

### GET /api/v1/audit/events?actor_id=&action=&outcome=&from=&to=&limit=50&cursor=

管理员。按时间倒序返回事件。

```json
{
  "items": [
    {
      "id": 1,
      "ts": "2026-11-10T12:00:00Z",
      "actor_user_id": "usr_...",
      "action": "auth.login",
      "target_type": "user",
      "target_id": "usr_...",
      "outcome": "success",
      "request_id": "req_...",
      "ip": "127.0.0.1",
      "detail": { "device_name": "Windows 桌面" }
    }
  ],
  "next_cursor": null
}
```

### GET /api/v1/audit/events/{id}

管理员。单条事件，不存在为 `E_NOT_FOUND`。

## 6. OIDC / OAuth2（授权线）

### GET /.well-known/openid-configuration（已实现）

返回 `issuer`、`authorization_endpoint`、`token_endpoint`、`userinfo_endpoint`、`jwks_uri`、`device_authorization_endpoint`、`revocation_endpoint`、`response_types_supported=["code"]`、`grant_types_supported`、`code_challenge_methods_supported=["S256"]`、`scopes_supported`、`id_token_signing_alg_values_supported=["EdDSA"]`。

### GET /.well-known/jwks.json（已实现）

Ed25519 公钥 JWK：`{"keys":[{"kty":"OKP","crv":"Ed25519","x":"...","kid":"...","use":"sig","alg":"EdDSA"}]}`。

### GET /oauth/authorize（已实现）

查询参数：`response_type=code`（必需）、`client_id`、`redirect_uri`、`scope`（空格分隔）、`state`、`code_challenge`、`code_challenge_method=S256`、`nonce`。

需要 `Authorization: Bearer <ho_sess_...>`（无前端登录页时以此代替浏览器会话）。成功：`302` 到 `redirect_uri?code=ho_ac_...&state=...`。
错误：参数缺失 → 400 标准 OAuth 错误；未认证 → 401；`redirect_uri` 不在白名单 → 400 `invalid_request`，**不重定向**。
当前为个人自用，授权自动同意（不实现 consent 页面）；`prompt=consent` 预留。

### POST /oauth/token（已实现）

`application/x-www-form-urlencoded`。

支持三种 `grant_type`：

1. `authorization_code`：`code`、`redirect_uri`、`client_id`、`code_verifier`；机密客户端另带 `client_secret`。
2. `refresh_token`：`refresh_token`、`client_id`；机密客户端另带 `client_secret`。
3. `urn:ietf:params:oauth:grant-type:device_code`：`device_code`、`client_id`。

成功响应：

```json
{
  "access_token": "ho_at_...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "ho_rt_...",
  "scope": "openid profile",
  "id_token": "eyJ..."
}
```

错误（RFC 6749）：`invalid_request` 400、`invalid_client` 401、`invalid_grant` 400、`invalid_scope` 400、`unauthorized_client` 400、`authorization_pending` 400、`slow_down` 400、`expired_token` 400、`access_denied` 400。

`id_token` 声明：`iss`、`sub`、`aud`、`exp`、`iat`、`nonce`（若请求带）、`preferred_username`、`name`（若授权 `profile`）、`email`、`email_verified`（若授权 `email`）。

### POST /oauth/device_authorization（已实现）

表单：`client_id`、`scope`。

```json
{
  "device_code": "ho_dc_...",
  "user_code": "ABCD-EFGH",
  "verification_uri": "http://127.0.0.1:8787/device",
  "verification_uri_complete": "http://127.0.0.1:8787/device?user_code=ABCD-EFGH",
  "expires_in": 600,
  "interval": 5
}
```

### POST /oauth/device/approve（已实现）

需要第一方会话 Bearer。表单或 JSON：`{ "user_code": "ABCD-EFGH", "action": "approve" | "deny" }`。成功 200：

```json
{ "status": "approved", "client_id": "app_market", "scope": "openid profile" }
```

错误：`E_AUTH_INVALID_USER_CODE` 404（码不存在或过期）、`E_CONFLICT` 409（已被处理）。

### GET /oauth/userinfo（已实现）

需要 `ho_at_` 且含 `openid` scope：

```json
{ "sub": "usr_...", "preferred_username": "alice", "name": "Alice", "email": "alice@example.com", "email_verified": false }
```

### POST /oauth/revoke（已实现）

表单：`token`、`client_id`。吊销 access 或 refresh token，恒返回 200 `{}`（不泄露令牌是否存在）。

### POST /oauth/introspect（契约已定）

保留给管理端自检，需要管理员会话令牌；请求表单 `token`，返回 `{"active": true, "scope": "...", "client_id": "...", "sub": "...", "exp": 0}`。本轮不实现。

### 客户端登记

不开放动态注册。通过 CLI 创建：

```powershell
python -m happyone.cli create-client --client-id app_market --name "开心软件市场" --redirect-uri http://127.0.0.1:53682/callback --type public --scope "openid profile email offline_access"
```

## 7. 许可证（已实现）

> 归属：许可证线（`src/happyone/license/`，迁移 `002_license.sql`）。
> 路径前缀 `/license`（不挂 `/api/v1`），错误包络为扁平 `{"ok": false, "code": "LICENSE_*", "error": "中文"}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/license/issue` | 签发许可证，成功 201 |
| POST | `/license/verify` | 校验：验签 → 过期 → 吊销 → 设备绑定 → 宽限期 |
| POST | `/license/revoke` | 吊销 |
| GET | `/license/revocations/snapshot` | 已签名吊销快照（客户端离线判断） |
| GET | `/license/public-key` | 验签公钥、`kid` 与宽限期 |
| GET | `/license/{license_id}` | 查询签发记录与吊销状态 |

### POST /license/issue

请求：

```json
{
  "user_id": "usr_...",
  "product_slug": "happy-one-demo",
  "features": ["export", "sync"],
  "expires_in_days": 30,
  "device_fingerprint": "可选，绑定单设备",
  "max_devices": 3,
  "license_id": "可选，自定义编号"
}
```

`expires_at`（Unix 秒）与 `expires_in_days` 二选一。响应 201：

```json
{
  "ok": true,
  "data": {
    "license_id": "lic_...",
    "kid": "k_...",
    "token": "hlic1....",
    "payload": { "typ": "license", "ver": 1, "license_id": "lic_...", "expires_at": 1796906762 }
  }
}
```

### POST /license/verify

请求：`{ "license": "hlic1....", "device_fingerprint": "可选" }`。
**恒返回 200**，业务结果在 `ok` 字段：

- 通过：`{"ok": true, "data": {"status": "valid" | "grace", "license_id": "...", "expires_at": "...", "grace_days_left": 0, "warnings": []}}`
- 不通过：`{"ok": false, "code": "LICENSE_EXPIRED", "error": "..."}`

### POST /license/revoke

请求：`{ "license_id": "lic_...", "reason": "可选", "revoked_by": "可选" }`。
响应 200：`{"ok": true, "data": {"license_id": "...", "revoked_at": 1794314762, "reason": "..."}}`。
重复吊销返回 409 `LICENSE_ALREADY_REVOKED`。

### 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `LICENSE_INVALID_REQUEST` | 400 | 请求字段缺失或类型错误 |
| `LICENSE_NOT_FOUND` | 404 | 许可证不存在 |
| `LICENSE_ALREADY_REVOKED` | 409 | 重复吊销 |
| `LICENSE_MALFORMED` / `LICENSE_UNSUPPORTED_VERSION` / `LICENSE_WRONG_TYPE` / `LICENSE_UNKNOWN_KEY` / `LICENSE_INVALID_SIGNATURE` / `LICENSE_EXPIRED` / `LICENSE_REVOKED` / `LICENSE_DEVICE_REQUIRED` / `LICENSE_DEVICE_MISMATCH` / `LICENSE_DEVICE_LIMIT` | 200 + `ok:false` | `/license/verify` 的判定结果 |
| `LICENSE_SERVICE_UNAVAILABLE` | 503 | 私钥或数据库不可用 |
| `LICENSE_INTERNAL_ERROR` | 500 | 未预期异常 |

私钥只存在于本机文件，不出现在响应、日志与仓库；公钥与 `kid` 通过 `/license/public-key` 分发。

## 8. 产物分发 / 存储接入（已实现）

> 归属：存储接入线（`src/happyone/storage/`，迁移 `003_storage.sql`）。
> 路径前缀 `/storage`（不挂 `/api/v1`），成功 `{"ok": true, ...}`，失败 `{"ok": false, "code": "ST-*", "error": "中文"}`。
> happy-one 只维护「业务实体 ↔ object_id ↔ sha256」映射，整文件流与直链由 happy-hub 承担。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/storage/info` | 命名空间 → 桶映射、短链有效期范围 |
| POST | `/storage/upload` | multipart 上传并写映射，201 |
| GET | `/storage/objects/{record_id}` | 单条产物元数据 |
| GET | `/storage/objects/{record_id}/link?expires_sec=` | 现签短链（收敛到 60–900 秒） |
| GET | `/storage/objects/{record_id}/redirect?expires_sec=` | 现签后 302 到 hub 直链 |
| DELETE | `/storage/objects/{record_id}` | 解除映射（按共享计数决定是否删 hub 对象） |
| GET | `/storage/entities/{entity_type}/{entity_id}?namespace=` | 按业务实体列出产物 |

`POST /storage/upload` 表单字段：`file`（必填）、`namespace`（`relay` / `license` / `backup`）、`entity_type`、`entity_id`、`filename`、`content_type`。响应：

```json
{
  "ok": true,
  "object": { "id": 1, "bucket": "relay", "object_id": "obj_...", "sha256": "...", "size_bytes": 1024, "key": "relay/..." },
  "deduped": false,
  "resumed": false,
  "uploaded_chunks": 1
}
```

`GET /storage/objects/{record_id}/link` 返回 `{ "ok": true, "url": "...", "expires_sec": 300, "expires_at": ... }`；
`/redirect` 直接返回 `302 Location: <hub 直链>`，不向前端暴露 hub 令牌。

错误码：`ST-INVALID` 400、`ST-NOT-FOUND` 404、`ST-CONFLICT` 409、`ST-TOO-LARGE` 413、`ST-AUTH` / `ST-THROTTLED` 透传 401/403/429、`ST-UNAVAILABLE` 503、`ST-BAD-RESPONSE` / `ST-HUB` 502。失败包络可能附带 `hub_code`、`detail` 两个排障字段。

## 9. 通道层：配对与 WebRTC 信令（已实现）

> 归属：通道信令线（`src/happyone/channel/`）。
> HTTP 路径为 `/channel/*`，WebSocket 为 `/ws/signal`，都不挂 `/api/v1`。错误为扁平包络 `{"ok": false, "code": ..., "error": "中文"}`。
> 本轮只做配对与 SDP/ICE 信令交换；**不提供中转文件上传/下载**（中转文件归存储线的 `relay` 命名空间，或后续阶段单独实现）。
> 会话与连接状态只保存在进程内存，进程重启即失效，不写 SQLite。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/channel/pair` | 创建配对，返回 6 位取件码，201 |
| POST | `/channel/pair/join` | 凭取件码加入配对 |
| GET | `/channel/pair/{session_id}` | 查询会话状态与双方连接情况 |
| POST | `/channel/pair/{session_id}/revoke` | 主动结束会话 |
| WS | `/ws/signal?session_id=&role=&token=` | 交换 offer / answer / ICE |

### POST /channel/pair

请求：

```json
{ "role": "mobile", "device_name": "iPhone 15" }
```

`role` 只能是 `mobile` 或 `desktop`。响应 201：

```json
{
  "ok": true,
  "code": "7KQ2M9",
  "session_id": "3f2a...",
  "token": "会话令牌",
  "role": "mobile",
  "direction": "mobile_to_desktop",
  "state": "waiting_peer",
  "transport": "direct",
  "relay_available": false,
  "expires_at": "2026-11-10T13:00:00+00:00",
  "expires_in": 600,
  "ws_path": "/ws/signal"
}
```

取件码 6 位，字符集去掉 `0/O/1/I/L` 等易混字符；有效期 10 分钟（`HAPPYONE_CHANNEL_PAIR_CODE_TTL`）。

### POST /channel/pair/join

请求：`{ "code": "7KQ2M9", "role": "desktop", "device_name": "MacBook" }`。`code` 大小写与连字符/空格会被归一化。
响应 200：

```json
{
  "ok": true,
  "session_id": "3f2a...",
  "token": "会话令牌",
  "role": "desktop",
  "direction": "mobile_to_desktop",
  "state": "paired",
  "transport": "direct",
  "relay_available": false,
  "peer": { "role": "mobile", "device_name": "iPhone 15", "online": false },
  "expires_at": "...",
  "expires_in": 1800,
  "ws_path": "/ws/signal"
}
```

进入 `paired` 只表示双方已配对；双方都建立 WebSocket 后才变为 `connected`。同一取件码只能成功加入一次。

### GET /channel/pair/{session_id}

响应 200：

```json
{
  "ok": true,
  "session_id": "3f2a...",
  "state": "connected",
  "direction": "mobile_to_desktop",
  "transport": "direct",
  "relay_available": false,
  "initiator": { "role": "mobile", "device_name": "iPhone 15", "online": true },
  "joiner": { "role": "desktop", "device_name": "MacBook", "online": true },
  "connection": { "initiator_online": true, "joiner_online": true, "transport": "direct", "relay_available": false },
  "created_at": "...",
  "expires_at": "...",
  "expires_in": 1500,
  "close_reason": null
}
```

`state` 取值：`waiting_peer` / `paired` / `connected` / `closed` / `expired`。
`transport` 本轮固定为 `direct`；`relay_available` 为中继兜底标记位，本轮恒为 `false`（未接 TURN）。

### POST /channel/pair/{session_id}/revoke

请求：`{ "token": "本端会话令牌" }`（必须属于该会话的某一端）。
响应 200：`{ "ok": true, "session_id": "...", "state": "closed", "notified": ["mobile", "desktop"] }`。
已结束会话再次调用幂等返回当前状态，`notified` 为空数组。

### WS /ws/signal

查询参数：`session_id`、`role`（`mobile` / `desktop`）、`token`。三者缺一即以 `token_required` 拒绝并关闭（close code 1008）。

客户端 → 服务端：

| type | 说明 |
| --- | --- |
| `signal.offer` / `signal.answer` / `signal.ice` | 必须带 `payload`（JSON 对象），服务端原样转发，不解析 SDP |
| `ping` | 心跳，服务端回 `signal.pong` |
| `bye` | 结束本次连接，对端收到 `signal.peer_left` |

服务端 → 客户端：

```json
{ "type": "signal.ready", "session_id": "...", "ts": 1794314762.123, "payload": { "role": "mobile", "state": "connected", "transport": "direct", "relay_available": false, "peer_online": true, "peer_role": "desktop" } }
```

| type | 说明 |
| --- | --- |
| `signal.ready` | 连接注册成功，携带当前状态与对端在线情况 |
| `signal.peer_joined` / `signal.peer_left` | 对端上线 / 下线（`payload.role`、`payload.reason`） |
| `signal.offer` / `signal.answer` / `signal.ice` | 转发的对端事件，带 `from` 与递增 `seq` |
| `signal.pong` | 心跳响应 |
| `signal.session_closed` | 会话结束（`payload.reason`，如 `revoked_by_mobile`、`ttl_expired`） |
| `signal.error` | 失败事件，携带 `ok:false`、`code`、`error` |

约束：单条消息体积上限 64 KiB（`HAPPYONE_CHANNEL_MAX_SIGNAL_BYTES`）；30 秒无任何消息判定半开连接并断开；会话空闲 TTL 30 分钟，有信令或心跳则滑动续期；同时存活会话上限 64；已结束会话保留 10 分钟供补查状态。

### 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `bad_request` / `bad_json` / `invalid_role` / `code_format` / `bad_event` | 400 | 请求格式或角色/取件码格式错误 |
| `code_not_found` / `session_not_found` | 404 | 取件码或会话不存在 |
| `code_expired` / `session_expired` | 410 | 取件码或会话已过期 |
| `code_used` / `session_closed` / `role_conflict` / `not_connected` / `peer_offline` | 409 | 状态冲突（已被使用、已结束、角色重复、未连接、对端离线） |
| `token_required` / `token_invalid` / `role_not_in_session` | 403 | 会话令牌缺失或无权限 |
| `payload_too_large` | 413 | 信令内容超过大小上限 |
| `peer_busy` / `too_many_sessions` | 429 / 503 | 对端队列满 / 全局会话数超限 |
| `heartbeat_timeout` / `unknown_event` / `relay_not_enabled` / `internal` | 事件或 500 | 心跳超时、未知事件、中继未启用、内部错误 |

完整文案见 `src/happyone/channel/models.py` 的 `ERROR_MESSAGES`。

## 10. 人机验证（已实现）

> 归属：人机验证线（`src/happyone/captcha/`，迁移 `005_captcha.sql`）。
> 路径前缀 `/captcha/v1`，错误为扁平包络 `{"ok": false, "code": ..., "error": "中文"}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/captcha/v1/challenge` | 出题：图形顺序拖拽（等价支持按序点按） |
| POST | `/captcha/v1/solve` | 提交操作轨迹，通过后换取一次性票据 |
| POST | `/captcha/v1/verify` | 核销票据（内部接口） |

### POST /captcha/v1/challenge

无请求体。响应 200：

```json
{
  "ok": true,
  "challenge": {
    "id": "cap_...",
    "type": "sequence",
    "title": "按顺序拼好图形",
    "hint": "...",
    "canvas": { "width": 260, "height": 208 },
    "interaction": { "mode": "drag", "alt_modes": ["tap"], "drop_tolerance": 28, "events": {...}, "instructions": "..." },
    "items": [{ "id": "i1", "label": "1", "x": 12, "y": 40, "w": 48, "h": 48, "shape": "chip" }],
    "slots": [{ "id": "s1", "index": 1, "x": 12, "y": 150, "w": 52, "h": 52 }],
    "target": ["i1", "i2", "i3", "i4"],
    "issued_at": 1794314762,
    "expires_at": 1794315062,
    "expires_in": 300
  }
}
```

挑战有效期 300 秒，同一题最多失败 3 次，之后必须换题。

### POST /captcha/v1/solve

请求 `{ "challenge_id": "cap_...", "mode": "drag" | "tap", "steps": [{ "n": 1, "item": "i1", "x": 12, "y": 150, "t": 320, "dt": 260, "path": [{ "x": 12, "y": 40, "t": 0 }] }] }`。
服务端校验顺序、落点、耗时与轨迹真实性。响应 200：`{ "ok": true, "ticket": "cap_tk_...", "purpose": "captcha_pass", "expires_in": 600 }`。
失败时可能附带 `attempts_left`。

### POST /captcha/v1/verify

请求 `{ "ticket": "cap_tk_...", "purpose": "captcha_pass" }`。票据一次性，核销成功后返回 `{ "ok": true, "purpose": "...", "issued_at": 0, "expires_at": 0 }`。

### 错误码

| code | HTTP | 含义 |
| --- | --- | --- |
| `invalid_request` | 422 | 提交结构不合法 |
| `timing_zero_step` / `timing_step_too_long` / `timing_invalid` / `timing_too_fast` / `timing_too_slow` / `timing_uniform` | 400 | 操作节奏异常（脚本或机器特征） |
| `position_invalid` / `track_invalid` / `wrong_order` | 400 | 落点、轨迹或顺序不正确 |
| `challenge_not_found` / `challenge_used` / `challenge_expired` | 404 / 409 / 410 | 挑战不存在、已使用、已过期 |
| `ticket_invalid` / `ticket_purpose` / `ticket_expired` | 401 | 票据格式、用途或有效期不符 |
| `ticket_used` | 409 | 票据已被核销 |
| `ip_mismatch` | 403 | 票据与当前网络环境不匹配 |

答案与密钥不写日志；票据用 HMAC 签名，密钥优先取 `HAPPYONE_CAPTCHA_SECRET`，否则持久化到 `<data_dir>/keys/captcha_secret`。

## 11. 实现状态总览

| 模块 | 状态 |
| --- | --- |
| 健康检查 / 元信息 | 已实现 |
| 第一方登录、登出、会话、改密 | 已实现 |
| 用户管理与审计查询 | 已实现 |
| OIDC 发现、JWKS、授权码+PKCE、设备码、刷新、吊销、userinfo | 已实现 |
| OIDC introspect | 契约已定 |
| 许可证 `/license/*` | 已实现 |
| 产物分发 `/storage/*` | 已实现（依赖 happy-hub，未联调真实 hub 时仅单元验证） |
| 通道配对与信令 `/channel/*`、`/ws/signal` | 已实现（仅信令，不含中转文件） |
| 人机验证 `/captcha/v1/*` | 已实现 |
| 中转文件上传 / 下载 | 未实现，归存储线 `relay` 命名空间或后续阶段 |
