-- 004_channel.sql
-- 通道层（配对 + 信令）数据库结构。
--
-- 重要：会话与长连接状态只保存在进程内存（单进程、≤20 人），
-- 本文件中的表仅用于「审计与排障」，不是运行时依赖；
-- 通道层在任何一张表缺失时都能正常工作（写入失败会被吞掉）。

CREATE TABLE IF NOT EXISTS channel_sessions (
    session_id      TEXT    PRIMARY KEY,
    direction       TEXT    NOT NULL CHECK (direction IN ('mobile_to_desktop', 'desktop_to_mobile')),
    state           TEXT    NOT NULL CHECK (state IN ('waiting_peer', 'paired', 'connected', 'closed', 'expired')),
    initiator_role  TEXT    NOT NULL CHECK (initiator_role IN ('mobile', 'desktop')),
    joiner_role     TEXT             CHECK (joiner_role IS NULL OR joiner_role IN ('mobile', 'desktop')),
    transport       TEXT    NOT NULL DEFAULT 'direct' CHECK (transport IN ('direct', 'relay')),
    created_at      TEXT    NOT NULL,
    paired_at       TEXT,
    closed_at       TEXT,
    close_reason    TEXT
);

-- 预留：当前取件码只存内存（10 分钟 TTL、一码一用）。
-- 若后续需要「进程重启后取件码仍有效」，可写入本表；写入方必须自己保证脱敏与过期清理。
CREATE TABLE IF NOT EXISTS channel_pair_codes (
    code        TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_channel_pair_codes_session
    ON channel_pair_codes (session_id);

CREATE TABLE IF NOT EXISTS channel_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    event       TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_channel_events_session
    ON channel_events (session_id, id);
