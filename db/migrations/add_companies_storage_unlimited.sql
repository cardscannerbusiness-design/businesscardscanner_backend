-- Company-level storage quota exception (not a scan/card change).
-- Default is FALSE for every existing row. Do not grant anyone here.
ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS storage_unlimited BOOLEAN NOT NULL DEFAULT FALSE;
