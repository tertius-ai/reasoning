"""Lean environments an organization has asked for, keyed by their exact toolchain and pinned packages."""

SCHEMA = (
    """CREATE TABLE compute_environments (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
        environment_key TEXT NOT NULL CHECK(length(environment_key)=64 AND environment_key NOT GLOB '*[^0-9a-f]*'),
        specification_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('requested','approved','declined')),
        requested_by TEXT NOT NULL REFERENCES users(id), requested_at INTEGER NOT NULL,
        decided_by TEXT REFERENCES users(id), decided_at INTEGER,
        CHECK((status='requested')=(decided_by IS NULL)), CHECK((decided_by IS NULL)=(decided_at IS NULL)),
        UNIQUE(organization_id,id), UNIQUE(organization_id,environment_key)
    )""",
    "CREATE INDEX compute_environment_page ON compute_environments(organization_id,status,requested_at)",
    """CREATE TRIGGER compute_environment_identity BEFORE UPDATE ON compute_environments
        WHEN NEW.id<>OLD.id OR NEW.organization_id<>OLD.organization_id OR NEW.environment_key<>OLD.environment_key
        OR NEW.specification_json<>OLD.specification_json OR NEW.requested_by<>OLD.requested_by
        OR NEW.requested_at<>OLD.requested_at
        BEGIN SELECT RAISE(ABORT,'a Lean environment names its sources for good'); END""",
    """CREATE TRIGGER compute_environment_approval BEFORE UPDATE ON compute_environments
        WHEN OLD.status='approved'
        BEGIN SELECT RAISE(ABORT,'an approved Lean environment stays approved'); END""",
)
