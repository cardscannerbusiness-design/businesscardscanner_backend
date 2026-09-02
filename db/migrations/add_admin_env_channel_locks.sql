-- CMS Super Admin channel kill-switches (locked=true → OFF in main app for that company).
ALTER TABLE admin_env_settings
    ADD COLUMN IF NOT EXISTS channel_locks JSONB NOT NULL
    DEFAULT '{"whatsapp": true, "email": false, "google_sheets": false}'::jsonb;
