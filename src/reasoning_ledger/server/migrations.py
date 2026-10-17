"""Ordered, atomic migrations for an installation's durable organization state."""

MIGRATIONS = (
    (
        "CREATE TABLE installation (singleton INTEGER PRIMARY KEY CHECK(singleton=1), initialized INTEGER NOT NULL DEFAULT 0)",
        "INSERT INTO installation(singleton) VALUES(1)",
        """CREATE TABLE users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
            password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
        )""",
        """CREATE TABLE organizations (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL,
            model_policy_json TEXT NOT NULL DEFAULT '[]', monthly_budget_microusd INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE memberships (
            organization_id TEXT NOT NULL REFERENCES organizations(id), user_id TEXT NOT NULL REFERENCES users(id),
            role TEXT NOT NULL CHECK(role IN ('owner','admin','member','viewer')),
            PRIMARY KEY(organization_id,user_id)
        )""",
        """CREATE TABLE invitations (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), email TEXT NOT NULL COLLATE NOCASE,
            role TEXT NOT NULL CHECK(role IN ('admin','member','viewer')), token_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), accepted_at INTEGER, revoked_at INTEGER
        )""",
        """CREATE TABLE projects (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), name TEXT NOT NULL,
            created_at INTEGER NOT NULL, UNIQUE(organization_id,id)
        )""",
        """CREATE TABLE channels (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
            created_at INTEGER NOT NULL, UNIQUE(organization_id,id), UNIQUE(project_id,name),
            FOREIGN KEY(organization_id,project_id) REFERENCES projects(organization_id,id)
        )""",
        """CREATE TABLE topics (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, channel_id TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('alpha','beta','gamma')), created_at INTEGER NOT NULL,
            UNIQUE(organization_id,id), FOREIGN KEY(organization_id,channel_id) REFERENCES channels(organization_id,id)
        )""",
        """CREATE TABLE messages (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, organization_id TEXT NOT NULL,
            topic_id TEXT NOT NULL, author_id TEXT NOT NULL REFERENCES users(id), content TEXT NOT NULL,
            client_id TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(organization_id,author_id,client_id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
        )""",
        "CREATE INDEX messages_topic_page ON messages(organization_id,topic_id,sequence)",
        """CREATE TABLE provider_keys (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), provider TEXT NOT NULL,
            label TEXT NOT NULL, ciphertext BLOB NOT NULL, fingerprint TEXT NOT NULL, created_at INTEGER NOT NULL,
            disabled_at INTEGER, UNIQUE(organization_id,id)
        )""",
        """CREATE TABLE audit_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT REFERENCES organizations(id),
            actor_id TEXT REFERENCES users(id), action TEXT NOT NULL, target_id TEXT NOT NULL, created_at INTEGER NOT NULL
        )""",
        "CREATE TABLE auth_attempts (scope TEXT NOT NULL, attempted_at INTEGER NOT NULL)",
        "CREATE INDEX auth_attempt_window ON auth_attempts(scope,attempted_at)",
    ),
    (
        "CREATE UNIQUE INDEX messages_org_identity ON messages(organization_id,id)",
        """CREATE TABLE topic_subscriptions (
            organization_id TEXT NOT NULL, user_id TEXT NOT NULL, topic_id TEXT NOT NULL,
            subscribed INTEGER NOT NULL DEFAULT 1 CHECK(subscribed IN (0,1)),
            last_read_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_read_sequence>=0),
            PRIMARY KEY(organization_id,user_id,topic_id),
            FOREIGN KEY(organization_id,user_id) REFERENCES memberships(organization_id,user_id) ON DELETE CASCADE,
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
        )""",
        """CREATE TABLE message_mentions (
            organization_id TEXT NOT NULL, message_id TEXT NOT NULL, user_id TEXT NOT NULL,
            PRIMARY KEY(organization_id,message_id,user_id),
            FOREIGN KEY(organization_id,message_id) REFERENCES messages(organization_id,id),
            FOREIGN KEY(organization_id,user_id) REFERENCES memberships(organization_id,user_id) ON DELETE CASCADE
        )""",
        "CREATE INDEX mention_recipient ON message_mentions(organization_id,user_id,message_id)",
    ),
    (
        "ALTER TABLE messages ADD COLUMN author_kind TEXT NOT NULL DEFAULT 'human' CHECK(author_kind IN ('human','assistant'))",
        "ALTER TABLE messages ADD COLUMN assistant_model TEXT",
        "ALTER TABLE messages ADD COLUMN inference_run_id TEXT",
        "CREATE INDEX audit_target_lookup ON audit_events(organization_id,action,target_id,sequence)",
        """CREATE TABLE llm_requests (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, topic_id TEXT NOT NULL,
            requested_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL,
            parameters_json TEXT NOT NULL, context_json TEXT NOT NULL, context_through_sequence INTEGER NOT NULL,
            created_at INTEGER NOT NULL, UNIQUE(organization_id,id), UNIQUE(organization_id,requested_by,client_id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
        )""",
        """CREATE TABLE llm_runs (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, request_id TEXT NOT NULL, model TEXT NOT NULL,
            key_id TEXT NOT NULL, quote_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled','interrupted')),
            cancel_requested INTEGER NOT NULL DEFAULT 0, attempt_id TEXT, started_at INTEGER, finished_at INTEGER,
            provider_generation_id TEXT, output_text TEXT NOT NULL DEFAULT '', error_code TEXT, message_id TEXT,
            reserved_microusd INTEGER NOT NULL CHECK(reserved_microusd>=0), actual_microusd INTEGER,
            accounting_state TEXT NOT NULL CHECK(accounting_state IN ('reserved','settled','uncertain','released')),
            settled_at INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
            UNIQUE(organization_id,id), UNIQUE(request_id,model),
            FOREIGN KEY(organization_id,request_id) REFERENCES llm_requests(organization_id,id),
            FOREIGN KEY(organization_id,key_id) REFERENCES provider_keys(organization_id,id),
            FOREIGN KEY(organization_id,message_id) REFERENCES messages(organization_id,id)
        )""",
        "CREATE INDEX llm_queue ON llm_runs(status,id)",
        """CREATE TABLE llm_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL, run_id TEXT NOT NULL,
            event_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY(organization_id,run_id) REFERENCES llm_runs(organization_id,id)
        )""",
        "CREATE INDEX llm_event_page ON llm_events(organization_id,run_id,sequence)",
    ),
    (
        "ALTER TABLE organizations ADD COLUMN compute_policy_json TEXT NOT NULL "
        'DEFAULT \'{"allowed_images":[],"monthly_cpu_seconds":0,"max_active_jobs":2}\'',
        """CREATE TABLE compute_workers (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, image_ids_json TEXT NOT NULL, resources_json TEXT NOT NULL,
            created_at INTEGER NOT NULL, last_seen_at INTEGER, draining INTEGER NOT NULL DEFAULT 0,
            revoked_at INTEGER, UNIQUE(organization_id,id)
        )""",
        """CREATE TABLE compute_jobs (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, topic_id TEXT NOT NULL,
            requested_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL, parameters_json TEXT NOT NULL,
            image_json TEXT NOT NULL, program_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled','outcome_unknown')),
            created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER, worker_id TEXT,
            attempt_id TEXT, lease_hash TEXT, lease_expires_at INTEGER, deadline_at INTEGER,
            cancel_requested INTEGER NOT NULL DEFAULT 0, error_code TEXT, exit_code INTEGER,
            elapsed_ms INTEGER, log_bytes INTEGER NOT NULL DEFAULT 0,
            reserved_cpu_seconds INTEGER NOT NULL CHECK(reserved_cpu_seconds>=0), used_cpu_seconds INTEGER,
            accounted_at INTEGER, UNIQUE(organization_id,id), UNIQUE(organization_id,requested_by,client_id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id),
            FOREIGN KEY(organization_id,worker_id) REFERENCES compute_workers(organization_id,id)
        )""",
        "CREATE INDEX compute_queue ON compute_jobs(organization_id,status,created_at,id)",
        """CREATE TABLE compute_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL, job_id TEXT NOT NULL,
            event_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY(organization_id,job_id) REFERENCES compute_jobs(organization_id,id)
        )""",
        "CREATE INDEX compute_event_page ON compute_events(organization_id,job_id,sequence)",
        """CREATE TABLE compute_log_chunks (
            job_id TEXT NOT NULL REFERENCES compute_jobs(id), chunk_index INTEGER NOT NULL,
            content_hash TEXT NOT NULL, PRIMARY KEY(job_id,chunk_index)
        )""",
        """CREATE TABLE compute_artifacts (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, job_id TEXT NOT NULL, name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), digest TEXT NOT NULL, committed_at INTEGER,
            UNIQUE(organization_id,id), UNIQUE(job_id,name),
            FOREIGN KEY(organization_id,job_id) REFERENCES compute_jobs(organization_id,id)
        )""",
        """CREATE TABLE compute_artifact_chunks (
            artifact_id TEXT NOT NULL REFERENCES compute_artifacts(id), chunk_index INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(artifact_id,chunk_index)
        )""",
    ),
    (
        "ALTER TABLE compute_jobs ADD COLUMN claim_id TEXT",
        "CREATE UNIQUE INDEX compute_worker_claim ON compute_jobs(worker_id,claim_id) WHERE claim_id IS NOT NULL",
    ),
    (
        "ALTER TABLE users ADD COLUMN account_kind TEXT NOT NULL DEFAULT 'human' CHECK(account_kind IN ('human','bot'))",
        "ALTER TABLE messages ADD COLUMN sender_kind TEXT NOT NULL DEFAULT 'human' CHECK(sender_kind IN ('human','assistant','bot'))",
        "UPDATE messages SET sender_kind=author_kind",
        """CREATE TABLE bots (
            id TEXT PRIMARY KEY REFERENCES users(id), organization_id TEXT NOT NULL REFERENCES organizations(id),
            created_by TEXT NOT NULL REFERENCES users(id), authorized_by TEXT NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL UNIQUE, scopes_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, revoked_at INTEGER, last_used_at INTEGER,
            UNIQUE(organization_id,id)
        )""",
        """CREATE TABLE bot_topics (
            organization_id TEXT NOT NULL, bot_id TEXT NOT NULL, topic_id TEXT NOT NULL,
            PRIMARY KEY(bot_id,topic_id),
            FOREIGN KEY(organization_id,bot_id) REFERENCES bots(organization_id,id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
        )""",
        "CREATE TABLE bot_requests (bot_id TEXT NOT NULL REFERENCES bots(id), attempted_at INTEGER NOT NULL, operation TEXT NOT NULL)",
        "CREATE INDEX bot_request_window ON bot_requests(bot_id,attempted_at)",
        "CREATE INDEX bot_request_expiry ON bot_requests(attempted_at)",
    ),
    (
        "ALTER TABLE compute_workers ADD COLUMN retired_at INTEGER CHECK(retired_at IS NULL OR retired_at>0)",
        "ALTER TABLE compute_workers ADD COLUMN retirement_id TEXT CHECK(retirement_id IS NULL OR "
        "(length(retirement_id)=32 AND retirement_id NOT GLOB '*[^0-9a-f]*'))",
        "CREATE UNIQUE INDEX compute_worker_retirement ON compute_workers(retirement_id) WHERE retirement_id IS NOT NULL",
    ),
    (
        """CREATE TABLE compute_scalers (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
            authorized_by TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, image_ids_json TEXT NOT NULL, resources_json TEXT NOT NULL,
            maximum_workers INTEGER NOT NULL CHECK(maximum_workers BETWEEN 1 AND 64),
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER,
            UNIQUE(organization_id,id)
        )""",
        """CREATE TABLE compute_enrollments (
            organization_id TEXT NOT NULL, scaler_id TEXT NOT NULL, request_id TEXT NOT NULL,
            worker_id TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(scaler_id,request_id),
            FOREIGN KEY(organization_id,scaler_id) REFERENCES compute_scalers(organization_id,id),
            FOREIGN KEY(organization_id,worker_id) REFERENCES compute_workers(organization_id,id)
        )""",
        "CREATE TABLE compute_scaler_requests (scaler_id TEXT NOT NULL REFERENCES compute_scalers(id), attempted_at INTEGER NOT NULL)",
        "CREATE INDEX compute_scaler_request_window ON compute_scaler_requests(scaler_id,attempted_at)",
    ),
    (
        """CREATE TABLE host_setup_receipt (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), setup_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
            organization_id TEXT NOT NULL REFERENCES organizations(id)
        )""",
    ),
    (
        "CREATE UNIQUE INDEX invitation_org_identity ON invitations(organization_id,id)",
        """CREATE TABLE invitation_mail (
            invitation_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
            requested_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL,
            request_json TEXT NOT NULL, configuration_hash TEXT NOT NULL, ciphertext BLOB,
            state TEXT NOT NULL CHECK(state IN ('queued','sending','accepted','rejected','unknown','cancelled')),
            created_at INTEGER NOT NULL, attempted_at INTEGER, finished_at INTEGER,
            provider_id TEXT, error_code TEXT,
            UNIQUE(organization_id,requested_by,client_id),
            FOREIGN KEY(organization_id,invitation_id) REFERENCES invitations(organization_id,id)
        )""",
        "CREATE INDEX invitation_mail_queue ON invitation_mail(state,created_at)",
        "CREATE INDEX invitation_mail_daily ON invitation_mail(created_at)",
    ),
    (
        """CREATE TABLE workflows (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, topic_id TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
            head_version INTEGER NOT NULL CHECK(head_version BETWEEN 1 AND 128),
            UNIQUE(organization_id,id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id),
            FOREIGN KEY(organization_id,id,head_version) REFERENCES workflow_versions(organization_id,workflow_id,version)
                DEFERRABLE INITIALLY DEFERRED
        )""",
        "CREATE INDEX workflow_topic_page ON workflows(organization_id,topic_id,id)",
        """CREATE TABLE workflow_versions (
            organization_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version BETWEEN 1 AND 128),
            definition_json TEXT NOT NULL, definition_digest TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
            PRIMARY KEY(organization_id,workflow_id,version),
            FOREIGN KEY(organization_id,workflow_id) REFERENCES workflows(organization_id,id)
        )""",
        """CREATE TABLE workflow_writes (
            organization_id TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL,
            request_json TEXT NOT NULL, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(organization_id,requested_by,client_id),
            UNIQUE(organization_id,workflow_id,version),
            FOREIGN KEY(organization_id,workflow_id,version) REFERENCES workflow_versions(organization_id,workflow_id,version)
        )""",
        "CREATE INDEX workflow_write_window ON workflow_writes(organization_id,requested_by,created_at)",
        """CREATE TRIGGER workflow_version_immutable_update BEFORE UPDATE ON workflow_versions
            BEGIN SELECT RAISE(ABORT,'workflow versions are immutable'); END""",
        """CREATE TRIGGER workflow_version_immutable_delete BEFORE DELETE ON workflow_versions
            BEGIN SELECT RAISE(ABORT,'workflow versions are immutable'); END""",
    ),
    (
        """CREATE TABLE workflow_runs (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, organization_id TEXT NOT NULL,
            topic_id TEXT NOT NULL, workflow_id TEXT NOT NULL, definition_version INTEGER NOT NULL,
            requested_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL, created_at INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','awaiting_approval','outcome_unknown','succeeded','failed','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1, cancel_requested INTEGER NOT NULL DEFAULT 0,
            restore_fenced INTEGER NOT NULL DEFAULT 0, finished_at INTEGER, images_json TEXT NOT NULL,
            UNIQUE(organization_id,id), UNIQUE(organization_id,requested_by,client_id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id),
            FOREIGN KEY(organization_id,workflow_id,definition_version) REFERENCES workflow_versions(organization_id,workflow_id,version)
        )""",
        "CREATE INDEX workflow_run_topic_page ON workflow_runs(organization_id,topic_id,sequence)",
        "CREATE INDEX workflow_run_queue ON workflow_runs(status,sequence)",
        """CREATE TABLE workflow_node_attempts (
            organization_id TEXT NOT NULL, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 16), kind TEXT NOT NULL CHECK(kind IN ('compute','llm','approval')),
            status TEXT NOT NULL CHECK(status IN
                ('pending','awaiting_approval','running','outcome_unknown','succeeded','failed','cancelled','blocked')),
            created_at INTEGER NOT NULL, finished_at INTEGER, error_code TEXT,
            inputs_json TEXT, inputs_digest TEXT,
            compute_job_id TEXT UNIQUE, inference_run_id TEXT UNIQUE,
            approved_by TEXT REFERENCES users(id), approval_decision TEXT CHECK(approval_decision IN ('approve','reject')),
            PRIMARY KEY(organization_id,run_id,node_id,attempt),
            CHECK(compute_job_id IS NULL OR kind='compute'), CHECK(inference_run_id IS NULL OR kind='llm'),
            FOREIGN KEY(organization_id,run_id) REFERENCES workflow_runs(organization_id,id),
            FOREIGN KEY(organization_id,compute_job_id) REFERENCES compute_jobs(organization_id,id),
            FOREIGN KEY(organization_id,inference_run_id) REFERENCES llm_runs(organization_id,id)
        )""",
        """CREATE TABLE workflow_run_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL, run_id TEXT NOT NULL,
            event_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY(organization_id,run_id) REFERENCES workflow_runs(organization_id,id)
        )""",
        "CREATE INDEX workflow_run_event_page ON workflow_run_events(organization_id,run_id,sequence)",
        """CREATE TABLE workflow_run_actions (
            organization_id TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL,
            run_id TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(organization_id,requested_by,client_id),
            FOREIGN KEY(organization_id,run_id) REFERENCES workflow_runs(organization_id,id)
        )""",
        "CREATE INDEX workflow_action_window ON workflow_run_actions(organization_id,requested_by,created_at)",
    ),
    (
        "CREATE UNIQUE INDEX message_org_identity ON messages(organization_id,id)",
        """CREATE TABLE bot_webhooks (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, bot_id TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL,
            url TEXT NOT NULL, prefix TEXT NOT NULL, ciphertext BLOB,
            created_at INTEGER NOT NULL, disabled_at INTEGER, dropped INTEGER NOT NULL DEFAULT 0,
            UNIQUE(organization_id,id), UNIQUE(organization_id,created_by,client_id),
            FOREIGN KEY(organization_id,bot_id) REFERENCES bots(organization_id,id)
        )""",
        "CREATE UNIQUE INDEX bot_webhook_active ON bot_webhooks(bot_id) WHERE disabled_at IS NULL",
        """CREATE TABLE bot_webhook_deliveries (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
            organization_id TEXT NOT NULL, webhook_id TEXT NOT NULL, topic_id TEXT NOT NULL,
            message_id TEXT NOT NULL, payload_json TEXT, payload_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','sending','accepted','failed','unknown','cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
            created_at INTEGER NOT NULL, next_attempt_at INTEGER NOT NULL,
            attempted_at INTEGER, finished_at INTEGER, error_code TEXT,
            UNIQUE(webhook_id,message_id),
            FOREIGN KEY(organization_id,webhook_id) REFERENCES bot_webhooks(organization_id,id),
            FOREIGN KEY(organization_id,message_id) REFERENCES messages(organization_id,id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
        )""",
        "CREATE INDEX bot_webhook_queue ON bot_webhook_deliveries(state,next_attempt_at,sequence)",
        "CREATE INDEX bot_webhook_history ON bot_webhook_deliveries(webhook_id,sequence)",
        """CREATE TABLE bot_webhook_requests (
            organization_id TEXT NOT NULL, webhook_id TEXT NOT NULL, attempted_at INTEGER NOT NULL,
            FOREIGN KEY(organization_id,webhook_id) REFERENCES bot_webhooks(organization_id,id)
        )""",
        "CREATE INDEX bot_webhook_request_window ON bot_webhook_requests(organization_id,attempted_at)",
    ),
    (
        """CREATE TABLE model_impact_profiles (
            organization_id TEXT NOT NULL REFERENCES organizations(id), model TEXT NOT NULL, profile_json TEXT NOT NULL,
            PRIMARY KEY(organization_id,model)
        )""",
        "ALTER TABLE llm_runs ADD COLUMN impact_profile_json TEXT",
    ),
    (
        "ALTER TABLE topics ADD COLUMN default_model TEXT "
        "CHECK(default_model IS NULL OR (kind='beta' AND length(default_model) BETWEEN 1 AND 200))",
        "UPDATE topics SET default_model=(SELECT json_extract(o.model_policy_json,'$[0]') "
        "FROM organizations o WHERE o.id=topics.organization_id) WHERE kind='beta'",
    ),
    (
        """CREATE TABLE user_presence (
            organization_id TEXT NOT NULL, user_id TEXT NOT NULL, last_seen_at INTEGER NOT NULL,
            PRIMARY KEY(organization_id,user_id),
            FOREIGN KEY(organization_id,user_id) REFERENCES memberships(organization_id,user_id) ON DELETE CASCADE
        )""",
        "CREATE INDEX user_presence_recent ON user_presence(organization_id,last_seen_at)",
    ),
    (
        "ALTER TABLE topics ADD COLUMN knowledge_policy_json TEXT NOT NULL DEFAULT '[]'",
        """UPDATE topics SET knowledge_policy_json='["bibliography","journal","ledger","workflows"]' WHERE kind='beta'""",
        """CREATE TABLE knowledge_entries (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, kind TEXT NOT NULL
                CHECK(kind IN ('ledger','bibliography','journal')),
            scope_kind TEXT NOT NULL CHECK(scope_kind IN ('organization','project','topic')),
            project_id TEXT, topic_id TEXT, created_by TEXT NOT NULL REFERENCES users(id),
            created_by_kind TEXT NOT NULL CHECK(created_by_kind IN ('human','bot')),
            created_at INTEGER NOT NULL, head_revision INTEGER NOT NULL CHECK(head_revision BETWEEN 1 AND 10000),
            UNIQUE(organization_id,id),
            CHECK((scope_kind='organization' AND project_id IS NULL AND topic_id IS NULL)
                OR (scope_kind='project' AND project_id IS NOT NULL AND topic_id IS NULL)
                OR (scope_kind='topic' AND project_id IS NOT NULL AND topic_id IS NOT NULL)),
            FOREIGN KEY(organization_id,project_id) REFERENCES projects(organization_id,id),
            FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id),
            FOREIGN KEY(organization_id,id,head_revision)
                REFERENCES knowledge_versions(organization_id,entry_id,revision) DEFERRABLE INITIALLY DEFERRED
        )""",
        "CREATE INDEX knowledge_scope ON knowledge_entries(organization_id,kind,scope_kind,project_id,topic_id,created_at)",
        """CREATE TABLE knowledge_versions (
            organization_id TEXT NOT NULL, entry_id TEXT NOT NULL, revision INTEGER NOT NULL,
            title TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
            created_by TEXT NOT NULL REFERENCES users(id), created_by_kind TEXT NOT NULL CHECK(created_by_kind IN ('human','bot')),
            created_at INTEGER NOT NULL,
            PRIMARY KEY(organization_id,entry_id,revision),
            FOREIGN KEY(organization_id,entry_id) REFERENCES knowledge_entries(organization_id,id)
        )""",
        """CREATE TABLE knowledge_writes (
            organization_id TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES users(id),
            requested_by_kind TEXT NOT NULL CHECK(requested_by_kind IN ('human','bot')), client_id TEXT NOT NULL,
            request_json TEXT NOT NULL, entry_id TEXT NOT NULL, revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(organization_id,requested_by_kind,requested_by,client_id),
            FOREIGN KEY(organization_id,entry_id,revision) REFERENCES knowledge_versions(organization_id,entry_id,revision)
        )""",
        """CREATE TRIGGER knowledge_version_immutable_update BEFORE UPDATE ON knowledge_versions
            BEGIN SELECT RAISE(ABORT,'knowledge versions are immutable'); END""",
        """CREATE TRIGGER knowledge_version_immutable_delete BEFORE DELETE ON knowledge_versions
            BEGIN SELECT RAISE(ABORT,'knowledge versions are immutable'); END""",
    ),
    (
        "ALTER TABLE projects ADD COLUMN visibility TEXT NOT NULL DEFAULT 'organization' "
        "CHECK(visibility IN ('organization','private'))",
        """CREATE TABLE project_memberships (
            organization_id TEXT NOT NULL, project_id TEXT NOT NULL, user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('owner','member')), created_at INTEGER NOT NULL,
            PRIMARY KEY(project_id,user_id),
            FOREIGN KEY(organization_id,project_id) REFERENCES projects(organization_id,id) ON DELETE CASCADE,
            FOREIGN KEY(organization_id,user_id) REFERENCES memberships(organization_id,user_id) ON DELETE CASCADE
        )""",
        "CREATE INDEX project_membership_user ON project_memberships(organization_id,user_id,project_id)",
    ),
    (
        "ALTER TABLE projects ADD COLUMN deleted_at INTEGER",
        "ALTER TABLE channels ADD COLUMN deleted_at INTEGER",
        "ALTER TABLE topics ADD COLUMN deleted_at INTEGER",
        """CREATE TABLE hierarchy_deletions (
            organization_id TEXT NOT NULL REFERENCES organizations(id), kind TEXT NOT NULL
                CHECK(kind IN ('projects','channels','topics')), entity_id TEXT NOT NULL,
            name TEXT NOT NULL, deleted_by TEXT NOT NULL REFERENCES users(id), deleted_at INTEGER NOT NULL,
            counts_json TEXT NOT NULL, PRIMARY KEY(organization_id,kind,entity_id)
        )""",
    ),
)

from .research_schema import SCHEMA as RESEARCH_SCHEMA  # noqa: E402

MIGRATIONS += (RESEARCH_SCHEMA,)

from .inference_ledger_schema import MIGRATION as INFERENCE_LEDGER_SCHEMA  # noqa: E402

MIGRATIONS += (INFERENCE_LEDGER_SCHEMA,)

from .workbench_schema import SCHEMA as WORKBENCH_SCHEMA  # noqa: E402

MIGRATIONS += (WORKBENCH_SCHEMA,)

MIGRATIONS += ((
    "ALTER TABLE organizations ADD COLUMN routing_policy_json TEXT NOT NULL "
    "DEFAULT '{\"strategy\":\"recommended\",\"zdr\":false,\"region\":\"global\"}'",
    """CREATE TABLE blog_publications (
        organization_id TEXT NOT NULL, entry_id TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(organization_id,entry_id),
        FOREIGN KEY(organization_id,entry_id,revision) REFERENCES knowledge_versions(organization_id,entry_id,revision)
    )""",
    "INSERT INTO blog_publications SELECT e.organization_id,e.id,e.head_revision FROM knowledge_entries e "
    "JOIN knowledge_versions v ON v.organization_id=e.organization_id AND v.entry_id=e.id AND v.revision=e.head_revision "
    "WHERE e.kind='journal' AND v.state='published'",
),)

MIGRATIONS += (("ALTER TABLE bots ADD COLUMN delegation_kind TEXT NOT NULL DEFAULT 'administrator' "
               "CHECK(delegation_kind IN ('administrator','personal'))",),)

MIGRATIONS += (("""CREATE TABLE bot_pairings (
    device_hash TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
    credential_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL, engine TEXT NOT NULL,
    expires_at INTEGER NOT NULL, bot_id TEXT REFERENCES bots(id),
    approved_by TEXT REFERENCES users(id), approval_json TEXT
)""",),)

MIGRATIONS += ((
    "ALTER TABLE bots ADD COLUMN public_access INTEGER NOT NULL DEFAULT 0 CHECK(public_access IN (0,1))",
    """CREATE TABLE bot_runtimes (
        bot_id TEXT PRIMARY KEY REFERENCES bots(id), engine TEXT NOT NULL, models_json TEXT NOT NULL,
        allow_other_users INTEGER NOT NULL, local_tools INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
        workspace_policy TEXT NOT NULL
    )""",
    """CREATE TABLE bot_runs (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id), channel_id TEXT NOT NULL REFERENCES channels(id),
        topic_id TEXT NOT NULL REFERENCES topics(id), bot_id TEXT NOT NULL REFERENCES bots(id),
        requested_by TEXT NOT NULL REFERENCES users(id), model TEXT NOT NULL, client_id TEXT NOT NULL,
        request_json TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, context_through_sequence INTEGER,
        lease_hash TEXT, output_text TEXT NOT NULL DEFAULT '', error_code TEXT,
        UNIQUE(organization_id,requested_by,client_id)
    )""",
    "CREATE INDEX bot_run_queue ON bot_runs(bot_id,status,created_at)",
    "ALTER TABLE workflow_node_attempts ADD COLUMN bot_run_id TEXT REFERENCES bot_runs(id)",
    """CREATE TABLE bot_tool_receipts (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES bot_runs(id),
        name TEXT NOT NULL, result_json TEXT NOT NULL, created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE bot_execution_approvals (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES bot_runs(id), bot_id TEXT NOT NULL REFERENCES bots(id),
        owner_id TEXT NOT NULL REFERENCES users(id), command_json TEXT NOT NULL, command_sha256 TEXT NOT NULL,
        status TEXT NOT NULL, created_at INTEGER NOT NULL, decided_at INTEGER, consumed_at INTEGER,
        UNIQUE(run_id,command_sha256)
    )""",
),)

from .work_schema import SCHEMA as WORK_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_SCHEMA,)

MIGRATIONS += ((
    """CREATE TABLE compute_job_sequences (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL UNIQUE REFERENCES compute_jobs(id)
    )""",
    "INSERT INTO compute_job_sequences(sequence,job_id) SELECT rowid,id FROM compute_jobs ORDER BY rowid",
),)

from .work_jobs_schema import SCHEMA as WORK_JOBS_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_JOBS_SCHEMA,)

from .work_context_schema import SCHEMA as WORK_CONTEXT_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_CONTEXT_SCHEMA,)

from .work_tools_schema import SCHEMA as WORK_TOOLS_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_TOOLS_SCHEMA,)

from .work_provider_schema import SCHEMA as WORK_PROVIDER_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_PROVIDER_SCHEMA,)

from .work_controller_schema import SCHEMA as WORK_CONTROLLER_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_CONTROLLER_SCHEMA,)

from .artifact_chunks_schema import SCHEMA as ARTIFACT_CHUNKS_SCHEMA  # noqa: E402

MIGRATIONS += (ARTIFACT_CHUNKS_SCHEMA,)

from .work_sources_schema import SCHEMA as WORK_SOURCES_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_SOURCES_SCHEMA,)

from .work_integration_schema import SCHEMA as WORK_INTEGRATION_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_INTEGRATION_SCHEMA,)

from .work_compaction_schema import SCHEMA as WORK_COMPACTION_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_COMPACTION_SCHEMA,)

from .schema_operations import PROJECT_ARTIFACTS  # noqa: E402

MIGRATIONS += ((PROJECT_ARTIFACTS,),)

from .work_workspace_schema import SCHEMA as WORK_WORKSPACE_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_WORKSPACE_SCHEMA,)

from .project_compute_schema import SCHEMA as PROJECT_COMPUTE_SCHEMA  # noqa: E402

MIGRATIONS += (PROJECT_COMPUTE_SCHEMA,)

from .work_compute_schema import SCHEMA as WORK_COMPUTE_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_COMPUTE_SCHEMA,)

from .work_overview_schema import SCHEMA as WORK_OVERVIEW_SCHEMA  # noqa: E402

MIGRATIONS += (WORK_OVERVIEW_SCHEMA,)

from .topic_agents_schema import SCHEMA as TOPIC_AGENTS_SCHEMA  # noqa: E402

MIGRATIONS += (TOPIC_AGENTS_SCHEMA,)

from .feedback_schema import SCHEMA as FEEDBACK_SCHEMA  # noqa: E402

MIGRATIONS += (FEEDBACK_SCHEMA,)

from .ledger_human_checks_schema import SCHEMA as LEDGER_HUMAN_CHECKS_SCHEMA  # noqa: E402

MIGRATIONS += (LEDGER_HUMAN_CHECKS_SCHEMA,)

from .blog_deletion_schema import SCHEMA as BLOG_DELETION_SCHEMA  # noqa: E402

MIGRATIONS += (BLOG_DELETION_SCHEMA,)

from .feedback_single_schema import SCHEMA as FEEDBACK_SINGLE_SCHEMA  # noqa: E402

MIGRATIONS += (FEEDBACK_SINGLE_SCHEMA,)

from .media_schema import SCHEMA as MEDIA_SCHEMA  # noqa: E402

MIGRATIONS += (MEDIA_SCHEMA,)

from .message_edit_schema import SCHEMA as MESSAGE_EDIT_SCHEMA  # noqa: E402

MIGRATIONS += (MESSAGE_EDIT_SCHEMA,)

from .bot_activity_schema import SCHEMA as BOT_ACTIVITY_SCHEMA  # noqa: E402

MIGRATIONS += (BOT_ACTIVITY_SCHEMA,)

from .bot_developer_schema import SCHEMA as BOT_DEVELOPER_SCHEMA  # noqa: E402

MIGRATIONS += (BOT_DEVELOPER_SCHEMA,)

from .bot_integration_schema import SCHEMA as BOT_INTEGRATION_SCHEMA  # noqa: E402

MIGRATIONS += (BOT_INTEGRATION_SCHEMA,)

from .email_verification_schema import SCHEMA as EMAIL_VERIFICATION_SCHEMA  # noqa: E402

MIGRATIONS += (EMAIL_VERIFICATION_SCHEMA,)

from .source_statements_schema import SCHEMA as SOURCE_STATEMENTS_SCHEMA  # noqa: E402

MIGRATIONS += (SOURCE_STATEMENTS_SCHEMA,)

from .model_preferences_schema import SCHEMA as MODEL_PREFERENCES_SCHEMA  # noqa: E402

MIGRATIONS += (MODEL_PREFERENCES_SCHEMA,)

from .bot_issue_self_approval_schema import SCHEMA as BOT_ISSUE_SELF_APPROVAL_SCHEMA  # noqa: E402

MIGRATIONS += (BOT_ISSUE_SELF_APPROVAL_SCHEMA,)

from .inference_admission_schema import SCHEMA as INFERENCE_ADMISSION_SCHEMA  # noqa: E402

MIGRATIONS += (INFERENCE_ADMISSION_SCHEMA,)

from .bot_run_timing_schema import SCHEMA as BOT_RUN_TIMING_SCHEMA  # noqa: E402

MIGRATIONS += (BOT_RUN_TIMING_SCHEMA,)
