# happyone.storage —— happy-hub 客户端层

本模块是 happy-one 与存储底座 happy-hub 之间唯一的接入层。只记录元数据，
文件本体始终在 hub 上；本地库只保存「业务实体 ↔ hub 对象」的映射与分片续传进度。

## 上层怎么调用

```python
from happyone.storage import StorageService
from happyone.storage.deps import ConnProvider, get_hub_client, get_conn

service = StorageService(
    client=get_hub_client(),                 # 进程内复用同一个 httpx.AsyncClient
    conn_provider=ConnProvider(get_conn),    # happyone.db.get_conn
)

# 上传：bytes / 本地路径 / 二进制流都可以
outcome = await service.upload(
    namespace="license",
    entity_type="license",
    entity_id="lic_123",
    filename="license.pdf",
    source=b"%PDF-1.7 ...",
    content_type="application/pdf",
)
outcome.record.id       # 映射表主键，后续接口都用它
outcome.deduped         # True 表示命中秒传，没有传字节

# 下载短链（TTL 收敛到 60–900 秒）
link = await service.sign_download(outcome.record.id, expires_sec=300)
link.url

# 302 跳转：service 只给状态码与地址，由 router 构造响应
spec = await service.sign_redirect(outcome.record.id)

await service.delete(outcome.record.id)
service.list_files("license", "lic_123", namespace="license")

# 配置缺失时可主动初始化：deps.aclose_hub_client()
```

HTTP 接口（由骨架线 `app.include_router(storage_router)` 挂载；路由自带
`/storage` 前缀，挂载时不要再叠加前缀）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/storage/upload` | multipart 上传，表单字段 `namespace` / `entity_type` / `entity_id` / `filename` / `content_type` |
| GET | `/storage/objects/{record_id}` | 取映射记录 |
| GET | `/storage/objects/{record_id}/link?expires_sec=` | 签名直链，返回纯 URL |
| GET | `/storage/objects/{record_id}/redirect?expires_sec=` | 302 跳转 |
| DELETE | `/storage/objects/{record_id}` | 删除 |
| GET | `/storage/entities/{entity_type}/{entity_id}?namespace=` | 按业务实体查文件 |
| GET | `/storage/info` | 命名空间、桶与 TTL 范围 |

响应包络与 hub 一致：成功 `{"ok": true, ...}`，失败
`{"ok": false, "code": "ST-XXX", "error": "中文说明"}`。

## 配置从哪来

优先级：`happyone.config.settings` → 环境变量。模块内不保存任何默认令牌。
`happyone.db.get_conn()` 需要返回可直接 `execute` 的连接对象（`sqlite3.Connection`
或等价 DBAPI 连接）；若骨架线使用生成器式依赖，请在 `deps.get_conn` 里包一层。

| 配置项 | 环境变量 | 默认 |
| --- | --- | --- |
| hub 地址 | `HAPPYONE_HUB_BASE_URL` | 无，必填 |
| hub 令牌 | `HAPPYONE_HUB_TOKEN` | 无，必填 |
| 请求超时 | `HAPPYONE_HUB_TIMEOUT_SEC` | 10s |
| 重试次数 | `HAPPYONE_HUB_MAX_RETRIES` | 2 |
| 签名 TTL | `HAPPYONE_STORAGE_SIGN_TTL_SEC` | 300s（下限 60、上限 900） |
| 分片大小 | `storage_chunk_size` | 8 MiB，必须与 hub 约定一致 |

## key 怎么取

命名空间与桶一一对应，调用方只传 namespace，不允许直接指定桶：

| namespace | bucket |
| --- | --- |
| `relay` | `relay` |
| `license` | `license-artifacts` |
| `backup` | `backup` |

两种 key 形态，都由 `keys.py` 生成：

- 内容寻址：`<namespace>/<sha256>`，上传时没有文件名就走这个，天然去重；
- 实体归档：`<namespace>/<entity_type>/<entity_id>/<filename>`，有文件名时使用。

规则：全部小写；只用 `/` 分段；段内仅允许 `a-z0-9._-`；禁止空段、`.`、`..`、
反斜杠、问号、井号；总长 ≤ 512，单段 ≤ 128。文件名会被清洗并保留扩展名，
实体类型/编号会被小写规范化（映射表里仍保存原始值）。

## 重试与失败语义

- 上传前先算 sha256，再 `HEAD /api/v1/blobs/{sha256}` 探针；命中且该 key 已有
  对象时直接登记映射，不传字节。
- 小文件（≤ 8 MiB）走 `PUT /api/v1/objects` 流式直传；大文件走
  `POST /api/v1/uploads` → 分片 PUT → `POST .../complete`。
- 已传分片写入 `storage_upload_sessions`，重试时跳过；hub 的会话只在内存，
  重启后若分片接口返回 404，本机记录作废并整体重传一次（只重来一次，不死循环）。
- 客户端自动重试仅限幂等请求：HEAD/GET/PUT/DELETE、签名、完成分片。
  建分片会话不重试，避免留下孤儿会话。重试对象为超时、网络错误、
  408/429/5xx；默认 2 次，退避 0.2s 起指数增长并带抖动，封顶 2s，
  会尊重 `Retry-After`。
- 失败统一抛 `StorageError`，带 `code / message / status / hub_code`。
  hub 的 `EXXX` 先按部署登记表覆盖，未登记时按 HTTP 状态给出可执行中文文案，
  原始错误码与文案保留在 `hub_code` / `detail` 里。
- 删除是「先查共享、再删本体」：还有别的实体映射指向同一个 hub 对象时，
  只删本条映射，不删除对象本体。

## 库表

`003_storage.sql` 建两张表（只存元数据，不存内容）：

- `storage_objects`：`namespace / bucket / object_key / object_id / sha256 /
  size_bytes / content_type / entity_type / entity_id / filename / 时间戳`；
  唯一键 `(entity_type, entity_id, bucket, object_key)`，同一实体重复上传走 upsert。
- `storage_upload_sessions`：`upload_id / bucket / object_key / sha256 /
  size_bytes / chunk_size / uploaded_chunks(JSON 数组)`，供断点续传。

## 依赖

- 运行：`httpx`；挂载 router 时 multipart 表单还需要 `python-multipart`。
- 单测：`pytest`、`pytest-asyncio`、`respx`（骨架线的 pyproject 负责声明）。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest tests\storage -q
```

测试全部用 respx 拦截 HTTP，不连真实 hub、不起容器；数据库用内存 sqlite 执行
`src/happyone/migrations/003_storage.sql`。
