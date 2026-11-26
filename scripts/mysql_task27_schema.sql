-- Apply as administrator to the isolated MYSQL-R2 rehearsal schema.
USE mysql_r2;

CREATE TABLE IF NOT EXISTS chat_groups (
    conversation_id VARCHAR(32) PRIMARY KEY,
    user_id BIGINT NOT NULL,
    title VARCHAR(80) NOT NULL,
    archived TINYINT NOT NULL DEFAULT 0,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    INDEX chat_groups_owner (user_id, archived)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_group_runs (
    run_id VARCHAR(32) PRIMARY KEY,
    conversation_id VARCHAR(32) NOT NULL,
    trace_id VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    INDEX chat_group_runs_group (conversation_id, status),
    FOREIGN KEY (conversation_id) REFERENCES chat_groups(conversation_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_group_entries (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id VARCHAR(32) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    content LONGTEXT NOT NULL,
    priority BIGINT NOT NULL DEFAULT 1,
    details JSON,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    INDEX chat_group_entries_run (run_id, id),
    FOREIGN KEY (run_id) REFERENCES chat_group_runs(run_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS chat_group_run_requests (
    user_id BIGINT NOT NULL,
    conversation_id VARCHAR(32) NOT NULL,
    action VARCHAR(32) NOT NULL,
    request_key VARBINARY(128) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    run_id VARCHAR(32) NOT NULL UNIQUE,
    shop_id VARCHAR(128),
    marketplace VARCHAR(32),
    PRIMARY KEY (user_id, conversation_id, action, request_key),
    FOREIGN KEY (conversation_id) REFERENCES chat_groups(conversation_id),
    FOREIGN KEY (run_id) REFERENCES chat_group_runs(run_id)
) ENGINE=InnoDB;

INSERT INTO chat_groups (conversation_id, user_id, title)
SELECT CONCAT('legacy-', user_id), user_id, '历史聊天'
FROM conversations WHERE user_id IS NOT NULL GROUP BY user_id
ON DUPLICATE KEY UPDATE conversation_id=chat_groups.conversation_id;
