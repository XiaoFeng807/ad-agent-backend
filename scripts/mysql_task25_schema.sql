-- Run explicitly against the isolated mysql_r2 rehearsal before ordinary chat.
-- Idempotent; deliberately no historical adjacency/time-based backfill.
USE mysql_r2;
CREATE TABLE IF NOT EXISTS conversation_turns (
    user_message_id BIGINT PRIMARY KEY,
    assistant_message_id BIGINT NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    CHECK (user_message_id < assistant_message_id),
    FOREIGN KEY (user_message_id) REFERENCES conversations(id),
    FOREIGN KEY (assistant_message_id) REFERENCES conversations(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS conversation_review_receipts (
    assistant_message_id BIGINT PRIMARY KEY,
    receipt JSON NOT NULL,
    FOREIGN KEY (assistant_message_id) REFERENCES conversations(id)
) ENGINE=InnoDB;
