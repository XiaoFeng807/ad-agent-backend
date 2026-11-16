CREATE DATABASE mysql_t0 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
USE mysql_t0;

CREATE TABLE ad_plans (
    id BIGINT PRIMARY KEY,
    user_id BIGINT,
    status VARCHAR(32) DEFAULT 'active',
    INDEX owner_status_plan (user_id, status, id)
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
    mode VARCHAR(32) NOT NULL DEFAULT 'structured',
    PRIMARY KEY (user_id, task_id)
) ENGINE=InnoDB;

CREATE TABLE diagnosis_turns (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    task_id VARCHAR(128) NOT NULL,
    reply TEXT,
    snapshot LONGTEXT CHECK (JSON_VALID(snapshot)),
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
    FOREIGN KEY (action_id) REFERENCES diagnosis_review_proposals(action_id),
    FOREIGN KEY (turn_id) REFERENCES diagnosis_turns(id)
) ENGINE=InnoDB;
