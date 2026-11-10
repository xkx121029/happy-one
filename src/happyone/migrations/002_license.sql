-- 002_license.sql
-- 许可证模块表结构（SQLite）。
-- 说明：
--   1. license_record 保存签发结果的完整令牌，供查询与吊销使用；
--   2. license_revocation 保存吊销项，导出吊销快照时按 revoked_at 排序；
--   3. license_device 记录多设备许可的激活设备（仅存设备指纹的 SHA-256，不存原文）。
-- 全部语句幂等，可重复执行。

CREATE TABLE IF NOT EXISTS license_record (
    license_id      TEXT    PRIMARY KEY,
    user_id         TEXT    NOT NULL,
    product_slug    TEXT    NOT NULL,
    features_json   TEXT    NOT NULL DEFAULT '[]',
    issued_at       INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    device_max      INTEGER,
    device_fp_hash  TEXT,
    kid             TEXT    NOT NULL,
    token           TEXT    NOT NULL,
    created_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_license_record_user
    ON license_record (user_id);

CREATE INDEX IF NOT EXISTS idx_license_record_product
    ON license_record (product_slug);

CREATE TABLE IF NOT EXISTS license_revocation (
    license_id  TEXT    PRIMARY KEY,
    revoked_at  INTEGER NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    revoked_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_license_revocation_time
    ON license_revocation (revoked_at);

CREATE TABLE IF NOT EXISTS license_device (
    license_id   TEXT    NOT NULL,
    device_hash  TEXT    NOT NULL,
    activated_at INTEGER NOT NULL,
    PRIMARY KEY (license_id, device_hash)
);

CREATE INDEX IF NOT EXISTS idx_license_device_license
    ON license_device (license_id);
