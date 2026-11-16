CREATE DATABASE mysql_r1 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
USE mysql_r1;

CREATE TABLE users (
    id BIGINT PRIMARY KEY,
    username VARCHAR(255) NOT NULL UNIQUE,
    password CHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'user'
) ENGINE=InnoDB;

CREATE TABLE sessions (
    user_id BIGINT PRIMARY KEY,
    token TEXT NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE ad_accounts (
    id BIGINT PRIMARY KEY,
    account_name VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE ad_plans (
    id BIGINT PRIMARY KEY,
    plan_name VARCHAR(255) NOT NULL,
    platform VARCHAR(128),
    daily_budget DOUBLE DEFAULT 0,
    status VARCHAR(32) DEFAULT 'active',
    impressions BIGINT DEFAULT 0,
    clicks BIGINT DEFAULT 0,
    cost DOUBLE DEFAULT 0,
    sales DOUBLE DEFAULT 0,
    start_date CHAR(10),
    end_date CHAR(10),
    account_id BIGINT,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX owner_status_plan (user_id, status, id),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (account_id) REFERENCES ad_accounts(id)
) ENGINE=InnoDB;

CREATE TABLE daily_reports (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    report_date CHAR(10),
    cost DOUBLE DEFAULT 0,
    impressions BIGINT DEFAULT 0,
    clicks BIGINT DEFAULT 0,
    sales DOUBLE DEFAULT 0,
    user_id BIGINT NOT NULL,
    INDEX owner_report_date (user_id, report_date),
    FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB;

CREATE TABLE plan_metric_bindings (
    plan_id BIGINT PRIMARY KEY,
    source_id VARCHAR(255) NOT NULL,
    data_version VARCHAR(255) NOT NULL,
    source_plan_id VARCHAR(255) NOT NULL,
    FOREIGN KEY (plan_id) REFERENCES ad_plans(id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_tasks (
    user_id BIGINT NOT NULL,
    task_id VARCHAR(128) NOT NULL,
    plan_id JSON NOT NULL,
    rule_id VARCHAR(128) NOT NULL,
    baseline_start CHAR(10) NOT NULL,
    baseline_end CHAR(10) NOT NULL,
    current_start CHAR(10) NOT NULL,
    current_end CHAR(10) NOT NULL,
    mode VARCHAR(32) NOT NULL DEFAULT 'structured',
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
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (action_id) REFERENCES diagnosis_review_proposals(action_id),
    FOREIGN KEY (turn_id) REFERENCES diagnosis_turns(id)
) ENGINE=InnoDB;
