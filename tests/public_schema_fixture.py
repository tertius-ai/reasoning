"""Remove current collaboration schemas before constructing an earlier-schema fixture."""


def remove_collaboration_schema(db):
    """Strip collaboration migrations in foreign-key dependency order, preserving core rows."""
    for index in ("llm_run_accounting", "llm_run_status", "llm_request_recent"):
        db.execute("DROP INDEX IF EXISTS " + index)
    db.execute("DROP TABLE IF EXISTS email_verifications")
    db.execute("DROP TABLE IF EXISTS user_model_favorites")
    if "default_effort" in {row[1] for row in db.execute("PRAGMA table_info(topics)")}:
        db.execute("ALTER TABLE topics DROP COLUMN default_effort")
    if "email_verified_at" in {row[1] for row in db.execute("PRAGMA table_info(users)")}:
        db.execute("ALTER TABLE users DROP COLUMN email_verified_at")
    if "project_id" in {row[1] for row in db.execute("PRAGMA table_info(compute_jobs)")}:
        db.execute("DROP INDEX compute_project_page")
        db.execute("DROP INDEX compute_worker_activity")
        db.execute("ALTER TABLE compute_jobs DROP COLUMN project_id")
    if "bot_run_id" in {row[1] for row in db.execute("PRAGMA table_info(workflow_node_attempts)")}:
        db.execute("ALTER TABLE workflow_node_attempts DROP COLUMN bot_run_id")
    for table in (
        "bot_compute_approvals",
        "bot_agent_handoffs",
        "bot_agent_tasks",
        "bot_issue_approvals",
        "bot_developer_permissions",
        "research_memory_edit_versions",
        "message_revisions",
        "research_media_chunks",
        "research_media",
        "chat_reactions",
        "fork_message_sources",
        "topic_forks",
        "ledger_human_checks",
        "blog_deletions",
        "topic_agent_handoffs",
        "topic_agent_waits",
        "topic_agent_tasks",
        "topic_agent_threads",
        "topic_agent_dispatch",
        "work_task_counts",
        "work_compute_receipts",
        "work_computations",
        "work_workspace_files",
        "work_integration_groups",
        "work_integration_inputs",
        "work_integration_state",
        "work_source_captures",
        "work_source_requests",
        "research_artifact_blobs",
        "research_blob_chunks",
        "research_blob_objects",
        "work_provider_call_sequences",
        "work_controller_wakes",
        "work_checks",
        "work_controller_steps",
        "work_controllers",
        "work_stream_events",
        "work_provider_calls",
        "work_tool_operations",
        "work_input_bindings",
        "work_compactions",
        "work_task_prompts",
        "work_dispatch_counts",
        "work_scheduler_clocks",
        "work_scheduler_policies",
        "work_effect_scopes",
        "work_resource_accounts",
        "work_effects",
        "work_results",
        "work_claims",
        "work_jobs",
        "work_worker_projects",
        "work_workers",
        "work_task_ancestors",
        "compute_job_sequences",
        "work_knowledge_relations",
        "work_events",
        "work_provenance",
        "work_dependencies",
        "work_tasks",
        "work_specifications",
        "work_programs",
        "bot_execution_approvals",
        "bot_tool_receipts",
        "agent_goal_events",
        "agent_goals",
        "bot_runs",
        "bot_runtimes",
        "bot_pairings",
        "blog_publications",
        "mathematical_reviews",
        "research_artifact_versions",
        "research_artifacts",
        "mathematical_records",
        "mathematical_requests",
        "inference_call_events",
        "inference_calls",
        "model_recommendation_preferences",
        "research_proposals",
        "research_tool_receipts",
        "research_sessions",
        "research_pins",
        "research_memories",
        "research_contexts",
        "research_topic_settings",
        "agent_goal_events",
        "agent_goals",
        "hierarchy_deletions",
    ):
        db.execute("DROP TABLE IF EXISTS " + table)
    for column in ("revision", "edited_at"):
        if column in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
            db.execute("ALTER TABLE messages DROP COLUMN " + column)
    if "routing_policy_json" in {row[1] for row in db.execute("PRAGMA table_info(organizations)")}:
        db.execute("ALTER TABLE organizations DROP COLUMN routing_policy_json")
    if "delegation_kind" in {row[1] for row in db.execute("PRAGMA table_info(bots)")}:
        db.execute("ALTER TABLE bots DROP COLUMN delegation_kind")
    if "public_access" in {row[1] for row in db.execute("PRAGMA table_info(bots)")}:
        db.execute("ALTER TABLE bots DROP COLUMN public_access")
    for table in ("projects", "channels", "topics"):
        if "deleted_at" in {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}:
            db.execute("ALTER TABLE " + table + " DROP COLUMN deleted_at")
