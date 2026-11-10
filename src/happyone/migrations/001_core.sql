-- 001_core.sql —— 地基线独占迁移
-- 内容：身份、会话、OIDC 客户端/授权码/设备码/令牌、审计
-- 约束：本文件执行后不可修改；新增结构一律追加新编号迁移（002+ 由各业务线负责）。

CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    email         TEXT UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disabled')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX idx_users_created ON users(created_at);

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    device_name TEXT,
    ip          TEXT,
    user_agent  TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

CREATE TABLE oidc_clients (
    client_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    client_type  TEXT NOT NULL DEFAULT 'public'
                 CHECK (client_type IN ('public', 'confidential')),
    secret_hash  TEXT,
    redirect_uris TEXT NOT NULL DEFAULT '[]',
    scopes       TEXT NOT NULL DEFAULT 'openid profile email',
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'disabled')),
    created_at   TEXT NOT NULL
);

CREATE TABLE oidc_auth_codes (
    code_hash             TEXT PRIMARY KEY,
    client_id             TEXT NOT NULL,
    user_id               TEXT NOT NULL,
    redirect_uri          TEXT NOT NULL,
    scope                 TEXT NOT NULL,
    code_challenge        TEXT,
    code_challenge_method TEXT,
    nonce                 TEXT,
    created_at            TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    consumed_at           TEXT
);

CREATE INDEX idx_auth_codes_expires ON oidc_auth_codes(expires_at);

CREATE TABLE oidc_device_codes (
    device_code_hash TEXT PRIMARY KEY,
    user_code        TEXT NOT NULL UNIQUE,
    client_id        TEXT NOT NULL,
    scope            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    user_id          TEXT,
    interval_sec     INTEGER NOT NULL DEFAULT 5,
    last_polled_at   TEXT,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    approved_at      TEXT
);

CREATE INDEX idx_device_codes_expires ON oidc_device_codes(expires_at);

CREATE TABLE oidc_tokens (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    token_hash TEXT NOT NULL UNIQUE,
    client_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    parent_id  TEXT,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX idx_tokens_user ON oidc_tokens(user_id);
CREATE INDEX idx_tokens_client ON oidc_tokens(client_id);
CREATE INDEX idx_tokens_expires ON oidc_tokens(expires_at);

CREATE TABLE audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    actor_user_id TEXT,
    action        TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    outcome       TEXT NOT NULL,
    request_id    TEXT,
    ip            TEXT,
    detail        TEXT
);

CREATE INDEX idx_audit_ts ON audit_events(ts);
CREATE INDEX idx_audit_actor ON audit_events(actor_user_id);
CREATE INDEX idx_audit_action ON audit_events(action);
