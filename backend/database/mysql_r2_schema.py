"""The version 2 rehearsal schema's ordered column and identity contract."""

R2_SCHEMA = "mysql_r2"
R2_VERSION_TABLE = "migration_schema_version"

R2_COLUMNS = {
    table: tuple(columns.split())
    for table, columns in {
        "users": "id username password role created_at",
        "sessions": "id user_id token created_at",
        "conversations": "id user_id role content created_at priority chat_view_id",
        "messages": "id user_id role content created_at",
        "ad_accounts": "id account_name platform balance daily_budget status user_id created_at",
        "ad_plans": "id plan_name platform daily_budget status impressions clicks cost sales "
                    "start_date end_date account_id user_id created_at",
        "alerts": "id type message level is_read user_id created_at",
        "daily_reports": "id report_date cost impressions clicks sales user_id",
        "decision_logs": "id user_id category suggestion status outcome effective verified_at created_at",
        "materials": "id name type url plan_id user_id created_at",
        "operation_logs": "id user_id action detail created_at",
        "plan_metric_bindings": "plan_id source_id data_version source_plan_id",
        "diagnosis_tasks": "user_id task_id plan_id rule_id baseline_start baseline_end current_start current_end mode",
        "diagnosis_turns": "id user_id task_id message reply snapshot",
        "diagnosis_review_proposals": "action_id turn_id snapshot",
        "diagnosis_reviews": "review_id action_id user_id turn_id snapshot created_at",
        "diagnosis_sandbox_targets": "plan_id user_id registration_id",
        "diagnosis_pause_proposals": "action_id review_action_id plan_id registration_id plan_name expected_status",
        "diagnosis_pause_receipts": "receipt_id action_id execution_trace_id committed_status created_at",
    }.items()
}

R2_PRIMARY_KEYS = {
    **dict.fromkeys(R2_COLUMNS, ("id",)),
    "plan_metric_bindings": ("plan_id",),
    "diagnosis_tasks": ("user_id", "task_id"),
    "diagnosis_review_proposals": ("action_id",),
    "diagnosis_reviews": ("review_id",),
    "diagnosis_sandbox_targets": ("plan_id",),
    "diagnosis_pause_proposals": ("action_id",),
    "diagnosis_pause_receipts": ("receipt_id",),
}

R2_FOREIGN_KEYS = {
    (table, "user_id", "users", "id", "NO ACTION")
    for table, columns in R2_COLUMNS.items()
    if "user_id" in columns and table != "diagnosis_turns"
} | {
    ("ad_plans", "account_id", "ad_accounts", "id", "NO ACTION"),
    ("plan_metric_bindings", "plan_id", "ad_plans", "id", "NO ACTION"),
    ("diagnosis_turns", "user_id,task_id", "diagnosis_tasks", "user_id,task_id", "NO ACTION"),
    ("diagnosis_review_proposals", "turn_id", "diagnosis_turns", "id", "NO ACTION"),
    ("diagnosis_reviews", "action_id", "diagnosis_review_proposals", "action_id", "NO ACTION"),
    ("diagnosis_reviews", "turn_id", "diagnosis_turns", "id", "NO ACTION"),
    ("diagnosis_sandbox_targets", "plan_id", "ad_plans", "id", "CASCADE"),
    ("diagnosis_pause_proposals", "review_action_id", "diagnosis_review_proposals", "action_id", "NO ACTION"),
    ("diagnosis_pause_receipts", "action_id", "diagnosis_pause_proposals", "action_id", "NO ACTION"),
}

R2_UNIQUE_KEYS = {
    ("users", "username"),
    ("sessions", "user_id"),
    ("diagnosis_review_proposals", "turn_id"),
    ("diagnosis_reviews", "action_id"),
    ("diagnosis_sandbox_targets", "registration_id"),
    ("diagnosis_pause_proposals", "review_action_id"),
    ("diagnosis_pause_receipts", "action_id"),
}
R2_AUTO_IDS = tuple(table for table, key in R2_PRIMARY_KEYS.items() if key == ("id",))
