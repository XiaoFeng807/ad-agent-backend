CREATE DATABASE mysql_r2 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
USE mysql_r2;

CREATE TABLE migration_schema_version (
    version TINYINT PRIMARY KEY CHECK (version = 2)
) ENGINE=InnoDB;
INSERT INTO migration_schema_version VALUES (2);

CREATE TABLE users (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL UNIQUE,
    password LONGTEXT NOT NULL,
    role LONGTEXT DEFAULT ('user'),
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

CREATE TABLE sessions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL UNIQUE,
    token TEXT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE conversations (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT,
    role LONGTEXT,
    content LONGTEXT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    priority BIGINT DEFAULT 1,
    chat_view_id LONGTEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT,
    role LONGTEXT,
    content LONGTEXT,
    created_at DATETIME(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE ad_accounts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    account_name LONGTEXT NOT NULL,
    platform LONGTEXT NOT NULL,
    balance DOUBLE DEFAULT 0,
    daily_budget DOUBLE DEFAULT 0,
    status LONGTEXT DEFAULT ('active'),
    user_id BIGINT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE ad_plans (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    plan_name LONGTEXT NOT NULL,
    platform LONGTEXT,
    daily_budget DOUBLE DEFAULT 0,
    status VARCHAR(255) DEFAULT 'active',
    impressions BIGINT DEFAULT 0,
    clicks BIGINT DEFAULT 0,
    cost DOUBLE DEFAULT 0,
    sales DOUBLE DEFAULT 0,
    start_date LONGTEXT,
    end_date LONGTEXT,
    account_id BIGINT,
    user_id BIGINT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    INDEX owner_status_plan (user_id, status, id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (account_id) REFERENCES ad_accounts(id)
) ENGINE=InnoDB;

CREATE TABLE alerts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    type LONGTEXT,
    message LONGTEXT,
    level LONGTEXT DEFAULT ('warning'),
    is_read BIGINT DEFAULT 0,
    user_id BIGINT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE daily_reports (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    report_date LONGTEXT,
    cost DOUBLE DEFAULT 0,
    impressions BIGINT DEFAULT 0,
    clicks BIGINT DEFAULT 0,
    sales DOUBLE DEFAULT 0,
    user_id BIGINT,
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE decision_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT,
    category LONGTEXT,
    suggestion LONGTEXT,
    status LONGTEXT DEFAULT ('pending'),
    outcome LONGTEXT,
    effective BIGINT DEFAULT 0,
    verified_at DATETIME(6),
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE materials (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name LONGTEXT,
    type LONGTEXT DEFAULT ('image'),
    url LONGTEXT,
    plan_id BIGINT DEFAULT 0,
    user_id BIGINT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE operation_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT,
    action LONGTEXT,
    detail LONGTEXT,
    created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE plan_metric_bindings (
    plan_id BIGINT PRIMARY KEY,
    source_id LONGTEXT NOT NULL,
    data_version LONGTEXT NOT NULL,
    source_plan_id LONGTEXT NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES ad_plans(id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_tasks (
    user_id BIGINT NOT NULL,
    task_id VARCHAR(128) NOT NULL,
    plan_id JSON NOT NULL,
    rule_id LONGTEXT NOT NULL,
    baseline_start LONGTEXT NOT NULL,
    baseline_end LONGTEXT NOT NULL,
    current_start LONGTEXT NOT NULL,
    current_end LONGTEXT NOT NULL,
    mode VARCHAR(32) NOT NULL DEFAULT 'structured' CHECK (mode IN ('structured', 'natural')),
    PRIMARY KEY (user_id, task_id),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_turns (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    task_id VARCHAR(128) NOT NULL,
    message LONGTEXT NOT NULL,
    reply LONGTEXT,
    snapshot LONGTEXT CHECK (JSON_VALID(snapshot)),
    INDEX diagnosis_turns_task (user_id, task_id, id),
    FOREIGN KEY (user_id, task_id) REFERENCES diagnosis_tasks(user_id, task_id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_review_proposals (
    action_id VARCHAR(128) PRIMARY KEY,
    turn_id BIGINT NOT NULL UNIQUE,
    snapshot LONGTEXT NOT NULL CHECK (JSON_VALID(snapshot)),
    FOREIGN KEY (turn_id) REFERENCES diagnosis_turns(id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_reviews (
    review_id VARCHAR(128) PRIMARY KEY,
    action_id VARCHAR(128) NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    turn_id BIGINT NOT NULL,
    snapshot LONGTEXT NOT NULL CHECK (JSON_VALID(snapshot)),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (action_id) REFERENCES diagnosis_review_proposals(action_id),
    FOREIGN KEY (turn_id) REFERENCES diagnosis_turns(id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_sandbox_targets (
    plan_id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    registration_id VARCHAR(128) NOT NULL UNIQUE,
    FOREIGN KEY (plan_id) REFERENCES ad_plans(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TRIGGER diagnosis_sandbox_target_transferred AFTER UPDATE ON ad_plans FOR EACH ROW
    DELETE FROM diagnosis_sandbox_targets WHERE plan_id=OLD.id AND NOT (OLD.user_id <=> NEW.user_id);

CREATE TABLE diagnosis_pause_proposals (
    action_id VARCHAR(128) PRIMARY KEY,
    review_action_id VARCHAR(128) NOT NULL UNIQUE,
    plan_id BIGINT NOT NULL,
    registration_id LONGTEXT NOT NULL,
    plan_name LONGTEXT NOT NULL,
    expected_status VARCHAR(32) NOT NULL CHECK (expected_status = 'active'),
    FOREIGN KEY (review_action_id) REFERENCES diagnosis_review_proposals(action_id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_pause_receipts (
    receipt_id VARCHAR(128) PRIMARY KEY,
    action_id VARCHAR(128) NOT NULL UNIQUE,
    execution_trace_id LONGTEXT NOT NULL,
    committed_status VARCHAR(32) NOT NULL CHECK (committed_status = 'paused'),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    FOREIGN KEY (action_id) REFERENCES diagnosis_pause_proposals(action_id)
) ENGINE=InnoDB;
