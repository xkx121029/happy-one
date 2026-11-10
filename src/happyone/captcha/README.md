# 人机验证模块（captcha）

目标：**两到三秒、不烦人、脚本难过**。二十人以内的内部系统，威胁很低，所以
不做重型方案，也**没有滑块拼图**。核心思路是「服务端出题 → 客户端回传操作过程 →
服务端校验过程」，即使答案正确，只要过程失真（零耗时、瞬移、节奏过于均匀）也拒绝。

```
浏览器                      captcha 模块
  │  POST /captcha/v1/challenge  │  出题（4 块碎片 + 4 个槽位），写库
  │ ───────────────────────────> │  返回可渲染 JSON（5 分钟有效）
  │  POST /captcha/v1/solve      │  校验：结构 → 节奏 → 落点 → 轨迹 → 答案
  │ ───────────────────────────> │  通过后签发 HMAC 票据（一题一票）
  │  POST /captcha/v1/verify     │  校验签名/用途/时效/IP，并单次核销
  │ ───────────────────────────> │  {"ok": true, "purpose": "captcha_pass"}
```

## 文件与契约

| 文件 | 说明 |
| --- | --- |
| `router.py` | 模块级 `router: APIRouter`，骨架线用 `app.include_router(router)` 挂载 |
| `service.py` | 纯逻辑（出题、判定、票据），**不 import fastapi** |
| `models.py` | pydantic 数据模型 / schema |
| `store.py` | SQLite 读写（挑战、票据），只做持久化 |
| `deps.py` | 配置与连接适配：优先 `happyone.config.settings` / `happyone.db.get_conn`，缺失时用 `Protocol` + 进程内 SQLite 兜底 |

迁移：`src/happyone/migrations/005_captcha.sql`（建表 DDL，模块首次访问时也会幂等执行，
因此骨架线尚未完成迁移器时也能独立跑通单测）。

密钥：只从配置读取，**不进仓库**。读取顺序 `HAPPYONE_CAPTCHA_SECRET`（环境变量）→
`CAPTCHA_SECRET` → `settings.captcha_secret` / `secret_key` / `secret`；
都没有时生成进程内临时密钥并打告警（进程重启后旧票据全部失效）。

## 接口

### 1. 取挑战 `POST /captcha/v1/challenge`

成功：

```json
{
  "ok": true,
  "challenge": {
    "id": "c_9f1c2ab37d4e5f60a1b2c3d4",
    "type": "sequence",
    "title": "按数字从小到大依次归位",
    "hint": "把每块碎片拖到下方对应序号的槽位；也可以直接按顺序点按碎片",
    "canvas": {"width": 360, "height": 220},
    "interaction": {
      "mode": "drag",
      "alt_modes": ["tap"],
      "drop_tolerance": 28,
      "events": {"grab": "pointerdown", "move": "pointermove", "drop": "pointerup"},
      "instructions": "拖动碎片到槽位；无障碍模式：按顺序点按碎片"
    },
    "items": [
      {"id": "3a9f01", "label": "7", "x": 21.4, "y": 49.7, "w": 48, "h": 48, "shape": "chip"}
    ],
    "slots": [
      {"id": "s1", "index": 1, "x": 23.0, "y": 146.0, "w": 52, "h": 52}
    ],
    "target": ["3a9f01", "b12c44", "0d5e77", "99ab10"],
    "issued_at": 1794313000,
    "expires_at": 1794313300,
    "expires_in": 300
  }
}
```

字段语义：

| 字段 | 说明 |
| --- | --- |
| `canvas` | 逻辑画布尺寸，前端按比例缩放（建议 `viewBox`），坐标全部是画布单位内的**左上角** |
| `items[]` | 可操作碎片；`label` 是展示文本，`shape` 目前固定 `chip` |
| `slots[]` | 目标槽位，`index` 从 1 起，**从左到右即目标顺序** |
| `target` | 目标顺序的 item id 数组，与控制台/调试面板对账用；正常渲染不要提前展示 |
| `interaction.mode` | 主通道 `drag`；`alt_modes` 为等价通道（无障碍）`tap` |
| `interaction.drop_tolerance` | 落点吸附半径，前端可在该半径内吸附到槽位中心 |
| `expires_in` | 剩余有效秒数，过期后必须重新取题 |

### 2. 提交轨迹 `POST /captcha/v1/solve`

```json
{
  "challenge_id": "c_9f1c2ab37d4e5f60a1b2c3d4",
  "mode": "drag",
  "steps": [
    {
      "n": 1,
      "item": "3a9f01",
      "x": 49.0,
      "y": 172.0,
      "t": 180,
      "dt": 180,
      "path": [
        {"x": 45.0, "y": 73.0, "t": 0},
        {"x": 47.2, "y": 120.5, "t": 90},
        {"x": 49.0, "y": 172.0, "t": 180}
      ]
    }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `n` | 是 | 步骤序号，从 1 连续递增 |
| `item` | 是 | 本步操作的碎片 id，四步各不同且覆盖全部碎片 |
| `x` `y` | 是 | `drag` 模式为**释放点**（槽位中心附近）；`tap` 模式为**点击点**（碎片中心附近） |
| `t` | 是 | 相对毫秒时间戳（自本题开始交互起），必须单调递增 |
| `dt` | 是 | 本步耗时（按下→释放，毫秒） |
| `path` | 否 | 本步指针采样；一旦提交就必须自洽（≥3 点、时间不减、不瞬移、终点接近 `x/y`） |

`path` 是可选增强：不传只按步级数据判定；传了会额外核对「不像脚本」的细节。

### 3. 校验票据 `POST /captcha/v1/verify`（内部使用）

请求 `{"ticket": "v1.xxx.yyy", "purpose": "captcha_pass"}`，成功返回
`{"ok": true, "purpose": "...", "issued_at": ..., "expires_at": ...}`，**同时把票据标记为已用**。
同一张票据第二次校验返回 `ticket_used`。

## 判定规则与阈值

判定顺序：结构 → 节奏 → 落点 → 轨迹 → 答案。任一失败都会让本题失败次数 +1。

| 规则 | 阈值 | 拒绝码 |
| --- | --- | --- |
| 挑战有效期 | 300s | `challenge_expired` |
| 单题失败上限 | 3 次（第 4 次直接拒绝，需换题） | `too_many_attempts` |
| 题目一次性 | 换取票据后 `used=1` | `challenge_used` |
| 单步耗时下限 | ≥ 60ms（防零耗时） | `timing_zero_step` |
| 单步耗时上限 | ≤ 6000ms | `timing_step_too_long` |
| 步间间隔 | ≥ 100ms（防瞬移连点） | `timing_too_fast` |
| 整题总耗时 | 800ms ~ 30000ms | `timing_too_fast` / `timing_too_slow` |
| 节奏过于均匀 | 间隔变异系数 < 0.08 **且** 极差 < 35ms | `timing_uniform` |
| 落点偏差 | ≤ 40px（相对槽位/碎片中心） | `position_invalid` |
| 轨迹瞬移 | 相邻采样跳变 > 400px | `track_invalid` |
| 轨迹采样 | < 3 点；或终点距落点 > 60px | `track_invalid` |
| 答案顺序 | 必须等于 `target` | `wrong_order` |
| 票据有效期 | 600s | `ticket_expired` |
| 票据签名/用途/一次性 | HMAC-SHA256；用途必须一致；`UPDATE ... WHERE used=0` | `ticket_invalid` / `ticket_purpose` / `ticket_used` |
| IP 绑定（可选） | IPv4 /24、IPv6 /48，双方都提供时才比对 | `ip_mismatch` |

失败会累计次数，错误体里带 `attempts_left`：

```json
{"ok": false, "code": "wrong_order", "error": "顺序不正确，请重新排列（剩余 2 次机会）", "attempts_left": 2}
```

## 票据格式

```
v1.<base64url(payload_json)>.<base64url(HMAC-SHA256(payload_b64))>
```

payload（键排序、紧凑 JSON）：

```json
{"exp": 1794313900, "iat": 1794313300, "ip": "203.0.113.0/24", "n": "8f2c...", "p": "captcha_pass", "v": 1}
```

* 签名密钥来自配置，不进仓库；
* `n`（nonce）同时是库内唯一键，核销用 `UPDATE captcha_ticket SET used=1 WHERE nonce=? AND used=0`，
  天然防并发重放；
* 票据 10 分钟内有效、**单次消耗**，不提供续期；
* `ip` 为空表示未绑定。

## 前端渲染要求

1. 画布用 `viewBox="0 0 360 220"` 等比缩放，所有元素直接使用接口给的 `x/y/w/h`，不要自行重排。
2. 槽位按 `index` 从左到右画 1..4 的序号；碎片展示 `label`，初始位置即接口给的 `x/y`。
3. 拖拽：`pointerdown` 记录起点与时间，`pointermove` 采样，`pointerup` 时若距槽位中心 ≤
   `drop_tolerance` 则吸附；**每步提交 `x/y` 用吸附后的槽位中心**（或实际释放点，两者都在容差内）。
4. `tap` 降级：按顺序点按碎片，`pointerup` 即算一步，`x/y` 用碎片中心；两种通道的 `steps`
   结构完全一致，只是 `mode` 不同。
5. `t` 用 `performance.now() - startTime`（毫秒，浮点），`dt` 用本次 `pointerup - pointerdown`；
   **不要取整到秒，也不要固定 sleep**，否则会被判为脚本。
6. 首次进入即把 `startTime` 定在用户第一次按下时，减少「看题时间」对总耗时的干扰；
   提交失败后按返回的 `attempts_left` 提示剩余次数，为 0 时自动重新取题。
7. 题目过期（`expires_in` 倒计时归零）或返回 `challenge_expired` / `too_many_attempts` 时，
   自动调用 `/challenge` 换题并重置轨迹缓存。
8. 视觉保持克制：碎片与槽位用中性色 + 单一强调色标记可操作项即可，不要做彩色光晕堆叠；
   不要改成滑块拼图，也不要加进度条式倒计时条，倒计时只放在角落以文字呈现。

## 仓储与降级

* 挑战与票据都落库（`captcha_challenge` / `captcha_ticket`），成功与消耗都用条件 `UPDATE`，
  并发下不会出现一题多票、一票多用。
* 骨架线的 `db.py` 尚未提供 `get_conn` 时，模块自动使用进程内 `:memory:` SQLite，并执行同一份迁移
  SQL，保证本模块可以单独跑单测；接入真实库后无需改代码。
* 骨架线的 `Database` 是显式传递的工厂对象，启动时可选择注入：
  `from happyone.captcha import deps; deps.configure(database=db, settings=settings)`，
  注入后连接与密钥都走骨架线；不注入则按 `get_conn` → 内存库的顺序回退。
* 当前按 SQLite 方言（`?` 占位符、`RETURNING`）实现，若骨架线切换到其他数据库需要改 `store.py`。

## 误伤真人的风险（已知）

1. **极快的人**：4 步总耗时 < 0.8s 会被判 `timing_too_fast`，手速极快或使用辅助输入设备的用户可能踩到。
2. **节奏过于稳定**：例如用键盘 Tab + Enter 匀速操作、或按固定节拍点击，可能命中 `timing_uniform`
   （已要求变异系数与极差**同时**满足才判，降低概率）。
3. **落点偏差**：手指粗、触屏缩放导致释放点偏离槽位中心 > 40px 会被拒；前端按吸附后的坐标回传可避免。
4. **`t`/`dt` 口径不一致**：若前端把 `t` 记成相对页面加载时间，跨题会出现异常大的总耗时；
   务必每题重置计时基准。
5. **IP 绑定**：同一用户在 Wi-Fi / 蜂窝网络切换（跨 /24 或 /48）时会被判 `ip_mismatch`，
   因此绑定是可选参数，内部调用可以不传 IP。
6. **无障碍通道**：`tap` 模式同样受节奏阈值约束，使用屏幕阅读器逐项确认的用户单步耗时可能偏长，
   但目前只有单步 6s 上限，正常不会触发。
