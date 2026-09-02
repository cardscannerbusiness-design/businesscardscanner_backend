-- Per-user card scan cap (NULL = inherit company card_limit).
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS user_card_limit INTEGER,
    ADD COLUMN IF NOT EXISTS user_cards_used INTEGER NOT NULL DEFAULT 0;

UPDATE users
SET user_card_limit = 10
WHERE user_card_limit IS NULL
  AND COALESCE(scans_unlimited, FALSE) = FALSE;
