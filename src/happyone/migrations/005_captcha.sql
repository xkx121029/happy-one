-- 005_captcha.sql —— 人机验证：一次性挑战 + 一次性票据（SQLite 方言）
-- 由骨架线迁移执行器按序应用；captcha 模块在缺少迁移器时也会幂等执行本文件。

CREATE TABLE IF NOT EXISTS captcha_challenge (
    id          TEXT PRIMARY KEY,           -- 挑战 id（随机，客户端可见）
    kind        TEXT NOT NULL DEFAULT 'sequence',
    payload     TEXT NOT NULL,              -- 挑战完整 JSON（含目标顺序）
    created_at  REAL NOT NULL,              -- Unix 秒
    expires_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0, -- 已失败次数
    solved_at   REAL,
    used        INTEGER NOT NULL DEFAULT 0  -- 1 = 已换取过票据
);

CREATE INDEX IF NOT EXISTS idx_captcha_challenge_expires
    ON captcha_challenge (expires_at);

CREATE TABLE IF NOT EXISTS captcha_ticket (
    nonce       TEXT PRIMARY KEY,           -- 票据内嵌随机串，兼作唯一键
    purpose     TEXT NOT NULL,              -- 用途标记，如 captcha_pass
    issued_at   REAL NOT NULL,
    expires_at  REAL NOT NULL,
    ip_prefix   TEXT,                       -- 可选：绑定的 IP 前缀，如 203.0.113.0/24
    sig         TEXT NOT NULL,              -- HMAC-SHA256 签名（base64url）
    used        INTEGER NOT NULL DEFAULT 0, -- 单次消耗标记
    used_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_captcha_ticket_expires
    ON captcha_ticket (expires_at);
