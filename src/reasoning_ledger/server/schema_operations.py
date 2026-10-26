"""Canonical schema operations that need an explicit SQLite table rebuild."""

import re

from .reaction_emoji_schema import ADD_CHECK as REACTION_EMOJI_ADD
from .reaction_emoji_schema import DROP_CHECK as REACTION_EMOJI_DROP

PROJECT_ARTIFACTS = "ALTER TABLE research_artifacts ALTER COLUMN topic_id DROP NOT NULL"
PROJECT_COMPUTE = "ALTER TABLE compute_jobs ALTER COLUMN topic_id DROP NOT NULL"
# A bot run in a direct conversation with its owner has no topic, channel or project.
BOT_DIRECT_RUNS = tuple(f"ALTER TABLE bot_runs ALTER COLUMN {column} DROP NOT NULL" for column in ("project_id", "channel_id", "topic_id"))
BOT_DIRECT_COLUMN = "ALTER TABLE bot_runs ADD COLUMN direct_conversation_id TEXT REFERENCES direct_conversations(id)"
CLAIM_INDEX = ("UPDATE mathematical_records SET object_id=claim_id "
               "WHERE record_type IN ('claim-head','claim-revision') AND claim_id IS NOT NULL")


def repair_claim_index(db):
    """Repair derived lookup metadata atomically; retain immutable bodies and the exact guard."""
    from reasoning_ledger.server.source_statements_schema import SCHEMA as index_schema
    from reasoning_ledger.server.workbench_schema import SCHEMA as records_schema

    repair_sql = ("UPDATE mathematical_records SET object_id=claim_id "
                  "WHERE record_type IN ('claim-head','claim-revision') AND claim_id IS NOT NULL")
    if index_schema != (repair_sql,):
        raise ValueError("unrecognized claim index repair operation")
    expected = records_schema[2].strip()
    guard = db.execute("SELECT sql FROM sqlite_schema WHERE type='trigger' "
                       "AND name='mathematical_revision_update' AND tbl_name='mathematical_records'").fetchone()
    if guard is None or guard[0] != expected:
        raise ValueError("claim index repair requires the exact immutable revision guard")
    db.execute("SAVEPOINT reasoning_claim_index")
    try:
        db.execute("DROP TRIGGER mathematical_revision_update")
        db.execute(index_schema[0])
        db.execute(guard[0])
    except BaseException:
        db.execute("ROLLBACK TO reasoning_claim_index")
        raise
    finally:
        db.execute("RELEASE reasoning_claim_index")


def _reaction_check(db, clause):
    """Rebuild chat_reactions with `clause` as the emoji column's check, keeping every row, index and other constraint."""
    schema = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='chat_reactions'").fetchone()[0]
    rebuilt, checks = re.subn(r"emoji TEXT NOT NULL(?:\s*CHECK\(emoji IN \([^)]*\)\))?", "emoji TEXT NOT NULL" + clause, schema, count=1)
    rebuilt, renamed = re.subn(r'^CREATE TABLE (?:"chat_reactions"|chat_reactions)(?=\s)', "CREATE TABLE chat_reactions_rebuild",
                               rebuilt, count=1)
    if checks != 1 or renamed != 1:
        raise ValueError("reaction schema does not match its emoji migration")
    indexes = [row[0] for row in db.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name='chat_reactions' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY name"
    )]
    db.execute(rebuilt)
    db.execute("INSERT INTO chat_reactions_rebuild SELECT * FROM chat_reactions")
    db.execute("DROP TABLE chat_reactions")
    db.execute("ALTER TABLE chat_reactions_rebuild RENAME TO chat_reactions")
    for sql in indexes:
        db.execute(sql)


def sqlite_migration(db, statement):
    if statement == REACTION_EMOJI_DROP:
        _reaction_check(db, "")
        return
    if statement == REACTION_EMOJI_ADD:
        _reaction_check(db, statement[statement.index(" CHECK("):])
        return
    if statement == CLAIM_INDEX:
        repair_claim_index(db)
        return
    if statement == BOT_DIRECT_COLUMN and any(row[1] == "direct_conversation_id" for row in db.execute("PRAGMA table_info(bot_runs)")):
        return
    if statement not in {PROJECT_ARTIFACTS, PROJECT_COMPUTE, *BOT_DIRECT_RUNS}:
        db.execute(statement)
        return
    # The installation migration disables FK enforcement before BEGIN, retains
    # every parent key, and runs foreign_key_check before committing the rebuild.
    if db.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("SQLite table rebuild requires a checked schema migration transaction")
    if statement == PROJECT_COMPUTE:
        _project_compute(db)
        return
    if statement in BOT_DIRECT_RUNS:
        _nullable_bot_run_column(db, statement.split()[5])
        return
    db.execute("""CREATE TABLE research_artifacts_rebuild (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, project_id TEXT NOT NULL,
        topic_id TEXT, path TEXT NOT NULL, head_revision INTEGER NOT NULL,
        UNIQUE(organization_id,project_id,path),
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""")
    db.execute("INSERT INTO research_artifacts_rebuild SELECT * FROM research_artifacts")
    db.execute("DROP TABLE research_artifacts")
    db.execute("ALTER TABLE research_artifacts_rebuild RENAME TO research_artifacts")


def _nullable_bot_run_column(db, column):
    # Removing NOT NULL changes no stored row, so SQLite's documented simpler procedure applies: rewrite the table's
    # own definition and bump the schema version, without copying the table. One edit covers all three columns, and
    # the statements for the other two find nothing left to do; backups replay every migration to build reference
    # schemas, so a full rebuild here would slow each of them.
    columns = [statement.split()[5] for statement in BOT_DIRECT_RUNS]
    schema = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='bot_runs'").fetchone()[0]
    named = re.match(r'CREATE TABLE (?:"bot_runs"|bot_runs)\s', schema)
    if column not in columns or not named or any(f"{name} TEXT" not in schema for name in columns):
        raise ValueError("bot run schema does not match its direct conversation migration")
    if f"{column} TEXT NOT NULL" not in schema:
        return
    for name in columns:
        schema = schema.replace(f"{name} TEXT NOT NULL", f"{name} TEXT", 1)
    version = db.execute("PRAGMA schema_version").fetchone()[0]
    db.execute("PRAGMA writable_schema=ON")
    try:
        db.execute("UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='bot_runs'", (schema,))
        db.execute(f"PRAGMA schema_version={version + 1}")
    finally:
        db.execute("PRAGMA writable_schema=OFF")


def _project_compute(db):
    # Retain every column introduced by earlier compute migrations and every
    # explicit index/guard. SQL comes from the installation's verified schema,
    # not user input; only this exact named table/column alteration is accepted.
    schema = db.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='compute_jobs'").fetchone()[0]
    rebuilt, count = re.subn(r'^CREATE TABLE (?:"compute_jobs"|compute_jobs)(?=\s)', 'CREATE TABLE compute_jobs_rebuild', schema, count=1)
    if count != 1 or "topic_id TEXT" not in rebuilt:
        raise ValueError("compute schema does not match its project migration")
    if "topic_id TEXT NOT NULL" not in rebuilt:
        return
    indexes = [row[0] for row in db.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name='compute_jobs' AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY name"
    )]
    db.execute(rebuilt.replace("topic_id TEXT NOT NULL", "topic_id TEXT", 1))
    db.execute("INSERT INTO compute_jobs_rebuild SELECT * FROM compute_jobs")
    db.execute("DROP TABLE compute_jobs")
    db.execute("ALTER TABLE compute_jobs_rebuild RENAME TO compute_jobs")
    for sql in indexes:
        db.execute(sql)
