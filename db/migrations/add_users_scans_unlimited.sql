-- Per-user card-scan exception (not a company/plan change).
-- Default is FALSE for every existing row. Do not grant anyone here.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS scans_unlimited BOOLEAN NOT NULL DEFAULT FALSE;
