-- 003_storage.sql —— happy-one 存储接入模块（对象元数据映射 + 断点续传记录）
--
-- 设计原则：只保存元数据，不复制文件内容。文件本体始终在 happy-hub 上，
-- 这里记录「业务实体 → hub 对象」的映射，便于按实体查询、签名、删除。
--
-- 命名空间与桶一一对应：relay / license / backup
--   relay   -> relay
--   license -> license-artifacts
--   backup  -> backup

CREATE TABLE IF NOT EXISTS storage_objects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT    NOT NULL CHECK (namespace IN ('relay', 'license', 'backup')),
    bucket       TEXT    NOT NULL CHECK (bucket IN ('relay', 'license-artifacts', 'backup')),
    object_key   TEXT    NOT NULL,
    object_id    TEXT,
    sha256       TEXT    NOT NULL CHECK (length(sha256) = 64),
    size_bytes   INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_type TEXT,
    entity_type  TEXT    NOT NULL,
    entity_id    TEXT    NOT NULL,
    filename     TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- 同一实体、同一桶、同一 key 只保留一行；重复上传走 upsert 更新元数据。
    -- 允许多个实体共用同一个 key（内容寻址下的正常去重），因此唯一键里带实体。
    UNIQUE (entity_type, entity_id, bucket, object_key)
);

CREATE INDEX IF NOT EXISTS ix_storage_objects_sha256
    ON storage_objects (sha256);

CREATE INDEX IF NOT EXISTS ix_storage_objects_object_id
    ON storage_objects (object_id);

CREATE INDEX IF NOT EXISTS ix_storage_objects_entity
    ON storage_objects (entity_type, entity_id);

-- 分片上传的本机会话记录。
-- happy-hub 的分片会话只存在内存里，服务重启即丢，因此「已传哪些分片」必须
-- 落在 happy-one 自己的库里；续传时以此为准，hub 侧会话失效则整体重传一次。
CREATE TABLE IF NOT EXISTS storage_upload_sessions (
    upload_id       TEXT    PRIMARY KEY,
    bucket          TEXT    NOT NULL,
    object_key      TEXT    NOT NULL,
    sha256          TEXT    NOT NULL,
    size_bytes      INTEGER NOT NULL CHECK (size_bytes >= 0),
    chunk_size      INTEGER NOT NULL CHECK (chunk_size > 0),
    uploaded_chunks TEXT    NOT NULL DEFAULT '[]',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_storage_upload_sessions_match
    ON storage_upload_sessions (bucket, object_key, sha256, size_bytes);
