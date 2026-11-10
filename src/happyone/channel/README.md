# 通道层（channel）

移动端与电脑之间的文件中转配对，以及 WebRTC 打洞所需的信令服务。
本模块可独立运行单测，不依赖登录、授权、下载等其他路由；任一环节异常都会在模块内转成
`{"ok": false, "code": "...", "error": "中文"}`，不会把 500 抛给 FastAPI。

## 模块契约

| 文件 | 作用 |
| --- | --- |
| `router.py` | 模块级 `router: APIRouter`，由骨架线 `app.include_router(channel.router.router)` 挂载 |
| `service.py` | 纯逻辑（配对、状态机、信令路由、TTL 清理），不 import fastapi |
| `models.py` | 请求/响应模型、事件名、中文错误文案 |
| `deps.py` | 对 `happyone.config.settings` / `happyone.db.get_conn` 的适配与 Protocol 兜底 |
| `../migrations/004_channel.sql` | 审计表结构（运行时状态仍在内存，不是必需依赖） |

挂载说明：路由路径写作 `/channel/...` 与 `/ws/signal`，挂载到根路由即可；若骨架给 Router 加了
prefix，实际路径会随之中移（README 中提到的路径均为根路径）。

## 客户端接入顺序

1. **建码**：一端调用 `POST /channel/pair`，提交 `role`（`mobile` 或 `desktop`）与 `device_name`。
   返回 6 位取件码（字符集 `23456789ABCDEFGHJKMNPQRSTUVWXYZ`，去掉了 0/O/1/I/L）、
   `session_id`、本端 `token`，有效期 10 分钟，一码一用。
2. **加入**：另一端调用 `POST /channel/pair/join`，提交 `code`、自己的 `role` 与 `device_name`。
   成功后拿到本端 `token`，取件码立即作废；两端角色必须不同。
3. **建立信令**：两端分别连接
   `ws://<host>/ws/signal?session_id=<id>&role=<role>&token=<token>`。
   先收到 `signal.ready`，对端上线时收到 `signal.peer_joined`。
4. **交换 SDP / ICE**：通过 WebSocket 互发 `signal.offer` / `signal.answer` / `signal.ice`，
   服务端只做路由与序号标记，不解析也不修改 `payload`。
5. **建 DataChannel**：ICE 连通后由发起方在 `RTCPeerConnection` 上 `createDataChannel`，
   进入文件传输阶段；传输期间两端需继续每 10 秒发一次 `ping` 维持信令连接。
6. **收尾**：任一端发 `bye` 或主动关闭 WebSocket，对端收到 `signal.peer_left`；
   也可调用 `POST /channel/pair/{session_id}/revoke` 强制结束会话。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/channel/pair` | 创建取件码（201） |
| POST | `/channel/pair/join` | 凭码加入，建立配对会话 |
| GET | `/channel/pair/{session_id}` | 查询配对与在线状态 |
| POST | `/channel/pair/{session_id}/revoke` | 撤销配对，body 需要成员 `token` |
| WS | `/ws/signal` | 信令通道，query 参数 `session_id`、`role`、`token` |

错误响应统一为：

```json
{"ok": false, "code": "code_expired", "error": "取件码已过期，请重新生成"}
```

常见 `code`：`bad_request`、`bad_json`、`code_format`、`code_not_found`、`code_expired`、
`code_used`、`role_conflict`、`session_not_found`、`session_closed`、`session_expired`、
`too_many_sessions`、`token_required`、`token_invalid`、`peer_offline`、
`payload_too_large`、`internal`。

## WebSocket 事件清单

客户端 → 服务端：

| 事件 | 说明 |
| --- | --- |
| `signal.offer` | 转发给对端，`payload` 原样透传 |
| `signal.answer` | 转发给对端，`payload` 原样透传 |
| `signal.ice` | 转发给对端，`payload` 原样透传 |
| `ping` | 心跳，服务端回 `signal.pong` |
| `bye` | 主动结束本次信令连接 |

服务端 → 客户端：

| 事件 | 说明 |
| --- | --- |
| `signal.ready` | 连接注册成功，`payload` 含 `peer_online`、`state`、`transport` |
| `signal.peer_joined` | 对端已上线 |
| `signal.peer_left` | 对端断线，`payload.reason` 说明原因 |
| `signal.pong` | 心跳响应 |
| `signal.session_closed` | 会话被撤销或过期 |
| `signal.error` | 结构化错误，含 `ok:false`、`code`、`error` |

转发事件统一结构：

```json
{"type": "signal.offer", "session_id": "...", "from": "mobile", "seq": 1, "ts": 1760000000.0, "payload": {"sdp": "..."}}
```

## 状态机

```
waiting_peer --join--> paired --双方 WS 接入--> connected
     |                   |                        |
     |                   |<----一方断线------------+
     |                   |
     +-----过期/撤销-----> expired / closed
```

- 取件码：`unused` → `used`（加入成功即作废）；过期或被撤销后从内存移除。
- 会话：`waiting_peer`（已建码未加入）→ `paired`（已配对，尚未双端在线）→
  `connected`（双端 WebSocket 都在线）→ `closed`（撤销）/ `expired`（TTL 到期）。
- 连接：同一角色重复连接会替换旧连接，旧连接收到 `signal.error`（`connection_replaced`）后关闭。

## TTL 策略

| 对象 | TTL | 说明 |
| --- | --- | --- |
| 取件码 | 10 分钟（`channel_pair_code_ttl`） | 未使用则过期回收；已使用则随会话结束回收 |
| 会话 | 30 分钟空闲（`channel_session_ttl`） | 每次心跳或信令都会滑动续期；仍有在线连接时不回收 |
| 半开连接 | 30 秒（`channel_heartbeat_timeout`） | 单条消息等待超时即回 `heartbeat_timeout` 并关闭 |
| 已结束会话 | 额外保留 10 分钟 | 方便客户端补查状态后回收 |
| 清理任务 | 30 秒（`channel_sweep_interval`） | 首次请求/连接时懒启动，随事件循环运行 |

所有阈值都有环境变量兜底（`HAPPYONE_CHANNEL_*`），骨架提供 `happyone.config.settings`
后优先读取其中的同名字段。

## 中继兜底（本版本未实现）

- 数据结构上保留 `transport: "direct" | "relay"` 与 `relay_available` 标记位，当前恒为
  `direct` / `false`。
- 以下情况 P2P 必然失败，需要中继（TURN）：双方均为对称 NAT（企业防火墙、部分 4G/5G 网络）、
  UDP 被完全封锁、运营商级 NAT（CGNAT）且无可用 STUN 反射。
- 接入方式：实现 `relay` 转发服务后，把 `service.RELAY_SUPPORTED` 置为 `True`，
  在会话上标记 `transport="relay"`，客户端再按 `relay_available` 走 TURN；
  信令层无需改动。

## 隔离与并发

- 全局单例 `PairingService` 只保存内存字典；HTTP 与 WebSocket 共用同一个事件循环。
- 每条 WebSocket 连接由独立队列 + sender 任务串行发送，避免并发写同一连接。
- 审计写入 SQLite 是可选项：`happyone.db` 不存在、表缺失或写入失败都会被吞掉。
- 未捕获异常会记录日志并返回 `internal`，不会影响其他路由。

## 测试

```bash
python -m pytest tests/channel -q
```

覆盖：WebSocket 双向转发、配对过期、取件码重复使用、对端断线事件、心跳超时、
令牌错误、内存 TTL 清理。
