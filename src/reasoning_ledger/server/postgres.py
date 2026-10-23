"""PostgreSQL installation storage, parameter binding and portable row semantics.

The organization services use a deliberately small SQL dialect. This adapter
maps that dialect, not arbitrary SQLite databases, to PostgreSQL. Migrations
retain their ordering and immutable-history constraints on both backends.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from ..deployment.private import read_private
from .schema_operations import sqlite_migration

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/|\$(\w*)\$.*?\$\1\$", re.S)
_JSON_CALL = re.compile(r"\bjson_extract\(([^(),]+),\s*'(\$[^']*)'\)")
_JSON_INTEGER = {"actual_microusd", "prompt_tokens", "completion_tokens", "max_completion_tokens", "max_concurrency", "account_cycle"}
_JSON_FLOAT = {"input_kwh_per_million", "output_kwh_per_million", "grid_grams_co2e_per_kwh"}


def driver():
    try:
        import psycopg
    except ImportError:
        raise ValueError("PostgreSQL storage requires the postgres extra: uv sync --extra postgres") from None
    return psycopg


@lru_cache
def history_messages():
    from .migrations import MIGRATIONS

    return {match[1] for batch in MIGRATIONS for statement in batch
            if (match := re.search(r"RAISE\(ABORT,'([^']+)'\)", statement))}


@dataclass(frozen=True)
class PostgresConfig:
    dsn: str = field(repr=False)
    schema: str = "reasoning"

    @classmethod
    def read(cls, path: Path):
        try:
            value = json.loads(read_private(path, 65536))
            if not isinstance(value, dict) or set(value) != {"dsn", "schema"}:
                raise ValueError
            result = cls(**value)
            if not isinstance(result.dsn, str) or not isinstance(result.schema, str) or not _IDENTIFIER.fullmatch(result.schema):
                raise ValueError
            if result.schema.startswith("pg_") or result.schema in {"public", "information_schema"}:
                raise ValueError
            from psycopg.conninfo import conninfo_to_dict

            options = conninfo_to_dict(result.dsn)
            if "host" not in options:
                raise ValueError
            host = options.get("host", "")
            local = host.startswith("/") or host in {"", "localhost", "127.0.0.1", "::1"}
            # A hostaddr can override the apparent local hostname; multiple-host
            # configurations are remote unless every connection is verified.
            if options.get("hostaddr") or "," in host:
                local = False
            if not local and options.get("sslmode") != "verify-full":
                raise ValueError
            if options.get("service"):
                raise ValueError  # Do not resolve a second, unbound configuration.
            return result
        except ImportError:
            driver()
            raise AssertionError("unreachable") from None
        except Exception:
            # Neither credential-bearing connection strings nor parser/driver
            # diagnostics may be printed by the public CLI.
            raise ValueError("invalid or unprotected PostgreSQL configuration; remote connections require sslmode=verify-full") from None


class Row(Mapping):
    """SQLite-compatible named/scalar access without coupling services to a driver."""

    def __init__(self, names, values):
        self._values = tuple((int(value) if value == value.to_integral_value() else float(value))
                             if isinstance(value, Decimal) else value for value in values)
        self._named = dict(zip(names, self._values, strict=True))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._named[key]

    def __iter__(self):
        return iter(self._named)

    def __len__(self):
        return len(self._named)


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _unquoted(sql, transform):
    result, position = [], 0
    for match in _SQL_LITERAL.finditer(sql):
        result.extend((transform(sql[position:match.start()]), match.group()))
        position = match.end()
    result.append(transform(sql[position:]))
    return "".join(result)


def query(sql: str, *, bound=False) -> str:
    """Translate fixed application SQL; values remain separate driver parameters."""
    def json_call(match):
        column, path = match.groups()
        call = f"reasoning_json_extract({column},'{path}')"
        leaf = path.rsplit(".", 1)[-1]
        datatype = "BIGINT" if leaf in _JSON_INTEGER else "DOUBLE PRECISION" if leaf in _JSON_FLOAT else None
        return f"CAST({call} AS {datatype})" if datatype else call

    if sql == "INSERT INTO compute_job_sequences(sequence,job_id) SELECT rowid,id FROM compute_jobs ORDER BY rowid":
        sql = "INSERT INTO compute_job_sequences(sequence,job_id) SELECT ROW_NUMBER() OVER (ORDER BY created_at,id),id FROM compute_jobs"
    sql = _JSON_CALL.sub(json_call, sql)
    sql = re.sub(r"\bjson_each\(", "reasoning_json_each(", sql)
    sql = re.sub(r"\bjson_type\(", "reasoning_json_type(", sql)
    sql = re.sub(r"\bjson_object\(", "json_build_object(", sql)
    sql = re.sub(r"\bjson_array\(", "json_build_array(", sql)
    sql = re.sub(r"strftime\('%s',\s*'now'\)", "CAST(FLOOR(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)) AS BIGINT)", sql)
    sql = re.sub(r"([\w.]+)\s+COLLATE\s+NOCASE\b", r"LOWER(\1)", sql, flags=re.I)
    ignored = bool(re.match(r"\s*INSERT OR IGNORE\b", sql, re.I))
    if ignored:
        sql = re.sub(r"INSERT OR IGNORE\b", "INSERT", sql, count=1, flags=re.I).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    sql = _unquoted(sql, lambda part: re.sub(r"\bLIKE\b", "ILIKE", part, flags=re.I))
    sql = _unquoted(sql, lambda part: re.sub(r"\bMAX\(([\w.]+),", r"GREATEST(\1,", part, flags=re.I))
    sql = _unquoted(sql, lambda part: re.sub(r"\bMIN\(([\w.]+),", r"LEAST(\1,", part, flags=re.I))
    sql = _unquoted(sql, lambda part: re.sub(r"\?(\s+IS\s+(?:NOT\s+)?NULL)", r"CAST(? AS TEXT)\1", part, flags=re.I))
    if bound:
        # Escape literal percent signs before introducing psycopg placeholders.
        sql = sql.replace("%", "%%")
        sql = _unquoted(sql, lambda part: part.replace("?", "%s"))
    return sql


def migration(statement: str, *, deferred=None) -> list[str]:
    """Render the canonical schema's types and history guards in native SQL."""
    from .schema_operations import CLAIM_INDEX

    if statement == CLAIM_INDEX:
        return ["ALTER TABLE mathematical_records DISABLE TRIGGER mathematical_revision_update", statement,
                "ALTER TABLE mathematical_records ENABLE TRIGGER mathematical_revision_update"]
    if statement.lstrip().startswith("CREATE TRIGGER work_task_count_"):
        from .work_overview_schema import postgres_trigger

        return postgres_trigger(statement)
    if statement.lstrip().startswith("CREATE TRIGGER"):
        match = re.fullmatch(
            r"\s*CREATE TRIGGER (\w+) BEFORE (UPDATE|DELETE) ON (\w+)\s*"
            r"(?:WHEN (.*?)\s*)?BEGIN SELECT RAISE\(ABORT,'([^']+)'\); END\s*", statement, re.S
        )
        if match is None:
            raise ValueError("unsupported canonical schema trigger")
        name, operation, table, condition, message = match.groups()
        condition = condition or "TRUE"
        condition = re.sub(r"\bIS NOT\b", "IS DISTINCT FROM", condition)
        result = "OLD" if operation == "DELETE" else "NEW"
        return [
            f"CREATE FUNCTION {name}_guard() RETURNS trigger LANGUAGE plpgsql AS $reasoning$ "
            f"BEGIN IF {condition} THEN RAISE EXCEPTION USING ERRCODE='23000', MESSAGE='{message}'; END IF; "
            f"RETURN {result}; END $reasoning$",
            f"CREATE TRIGGER {name} BEFORE {operation} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_guard()",
        ]
    table = re.match(r"\s*CREATE TABLE (\w+)\s*\(", statement)
    if table:
        forward = re.compile(r",\s*(FOREIGN KEY\([^)]+\)\s+REFERENCES \w+\([^)]+\)\s+DEFERRABLE INITIALLY DEFERRED)")
        constraints = forward.findall(statement)
        if constraints:
            if deferred is None:
                raise ValueError("forward references require an atomic migration batch")
            deferred.extend(f"ALTER TABLE {table[1]} ADD {constraint}" for constraint in constraints)
            statement = forward.sub("", statement)
    statement = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", statement)
    statement = _unquoted(statement, lambda part: re.sub(r"\bINTEGER\b", "BIGINT", re.sub(r"\bBLOB\b", "BYTEA", part)))
    statement = re.sub(r"\s+COLLATE NOCASE\b", "", statement)
    statement = re.sub(r"(\w+) NOT GLOB '\*\[\^0-9a-f\]\*'", r"\1 !~ '[^0-9a-f]'", statement)
    result = [query(statement)]
    if re.match(r"\s*CREATE TABLE users\b", statement):
        result.append("CREATE UNIQUE INDEX users_email_nocase ON users(LOWER(email))")
    return result


_FUNCTIONS = (
    """CREATE OR REPLACE FUNCTION reasoning_json_path(document TEXT, path TEXT) RETURNS JSONB
        LANGUAGE SQL IMMUTABLE STRICT AS $reasoning$
        SELECT document::jsonb #> string_to_array(trim(both '.' from replace(replace(replace(path,'$',''),'[','.'),']','')),'.')
        $reasoning$""",
    """CREATE OR REPLACE FUNCTION reasoning_json_extract(document TEXT, path TEXT) RETURNS TEXT
        LANGUAGE SQL IMMUTABLE STRICT AS $reasoning$
        SELECT CASE jsonb_typeof(value) WHEN 'null' THEN NULL WHEN 'string' THEN value #>> '{}'
            WHEN 'boolean' THEN CASE WHEN value='true'::jsonb THEN '1' ELSE '0' END ELSE value::text END
        FROM (SELECT reasoning_json_path(document,path) AS value) v
        $reasoning$""",
    """CREATE OR REPLACE FUNCTION reasoning_json_type(document TEXT, path TEXT) RETURNS TEXT
        LANGUAGE SQL IMMUTABLE STRICT AS $reasoning$
        SELECT CASE jsonb_typeof(value) WHEN 'number' THEN CASE WHEN value::text ~ '^-?[0-9]+$' THEN 'integer' ELSE 'real' END
            WHEN 'boolean' THEN value::text ELSE jsonb_typeof(value) END
        FROM (SELECT reasoning_json_path(document,path) AS value) v
        $reasoning$""",
    """CREATE OR REPLACE FUNCTION reasoning_json_each(document TEXT, path TEXT) RETURNS TABLE(key TEXT,value TEXT)
        LANGUAGE SQL IMMUTABLE STRICT AS $reasoning$
        SELECT (ordinality-1)::text,value::text FROM jsonb_array_elements(reasoning_json_path(document,path)) WITH ORDINALITY
        $reasoning$""",
)


class Connection:
    dialect = "postgresql"

    def __init__(self, native):
        self.native = native

    def execute(self, sql, parameters=None):
        try:
            if parameters is not None:
                parameters = tuple(int(value) if isinstance(value, bool) else value for value in parameters)
            return self.native.execute(query(sql, bound=parameters is not None), parameters)
        except driver().IntegrityError as exc:
            # Existing organization services already translate this error to
            # scoped conflicts. The enclosing transaction still rolls back.
            message = exc.diag.message_primary
            if message not in history_messages():
                message = "database constraint refused this change"
            raise sqlite3.IntegrityError(message) from None

    def executemany(self, sql, parameters):
        cursor = self.native.cursor()
        try:
            cursor.executemany(query(sql, bound=True), (
                tuple(int(value) if isinstance(value, bool) else value for value in row) for row in parameters
            ))
        except driver().IntegrityError:
            raise sqlite3.IntegrityError("database constraint refused this change") from None
        return cursor

    def schema_version(self):
        return self.native.execute("SELECT version FROM reasoning_schema WHERE singleton=1").fetchone()[0]


class Database:
    def __init__(self, config: PostgresConfig):
        self.config = config

    def connect(self):
        try:
            native = driver().connect(self.config.dsn, autocommit=True, row_factory=row_factory, connect_timeout=8)
        except driver().Error:
            raise ValueError("could not connect to the configured PostgreSQL database") from None
        from psycopg.sql import SQL, Identifier

        native.execute(SQL("SET search_path TO {}, pg_catalog").format(Identifier(self.config.schema)))
        native.execute("SET statement_timeout TO '30s'")
        native.execute("SET lock_timeout TO '15s'")
        native.execute("SET idle_in_transaction_session_timeout TO '30s'")
        return native

    def lock(self, native, scope):
        identity = int.from_bytes(hashlib.sha256((self.config.schema + ":" + scope).encode()).digest()[:8], "big", signed=True)
        native.execute("SELECT pg_advisory_xact_lock(%s)", (identity,))

    @contextmanager
    def transaction(self, *, write=False, scope=None):
        with self.connect() as native, native.transaction():
            if write and scope is not False:
                # Legacy services retain their single-writer accounting contract.
                # General work/jobs choose a resource scope or native row locks.
                self.lock(native, scope or "legacy-writes")
            elif not write:
                native.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield Connection(native)

    def initialize(self, key_fingerprint, migrations):
        from psycopg.sql import SQL, Identifier

        with self.connect() as native, native.transaction():
            self.lock(native, "schema-initialization")
            native.execute(SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(self.config.schema)))
            native.execute("CREATE TABLE IF NOT EXISTS reasoning_schema (singleton BIGINT PRIMARY KEY CHECK(singleton=1), "
                           "version BIGINT NOT NULL, key_fingerprint TEXT NOT NULL)")
            native.execute("INSERT INTO reasoning_schema VALUES(1,0,%s) ON CONFLICT DO NOTHING", (key_fingerprint,))
            state = native.execute("SELECT version,key_fingerprint FROM reasoning_schema WHERE singleton=1").fetchone()
            version, fingerprint = state["version"], state["key_fingerprint"]
            if fingerprint != key_fingerprint:
                raise ValueError("PostgreSQL installation requires its original encryption.key")
            if version > len(migrations):
                raise ValueError("server database schema is newer than this release")
            for statement in _FUNCTIONS:
                native.execute(statement)
            for index in range(version, len(migrations)):
                deferred = []
                for statement in migrations[index]:
                    for rendered in migration(statement, deferred=deferred):
                        native.execute(rendered)
                        if statement.startswith("INSERT INTO compute_job_sequences(sequence,job_id)"):
                            native.execute("SELECT setval(pg_get_serial_sequence('compute_job_sequences','sequence'), "
                                           "COALESCE(MAX(sequence),1),COUNT(*)>0) FROM compute_job_sequences")
                for statement in deferred:
                    native.execute(statement)
                native.execute("UPDATE reasoning_schema SET version=%s WHERE singleton=1", (index + 1,))

    def snapshot(self, destination: Path, key_fingerprint: str, migrations, maximum: int):
        """Stream one consistent snapshot into the canonical portable archive DB.

        Connection credentials are not exported. Archive authentication and
        recovery fencing apply to the resulting SQLite snapshot.
        """
        from psycopg.sql import SQL, Identifier

        deadline = time.monotonic() + 300
        with self.transaction() as source, sqlite3.connect(destination) as target:
            state = source.execute("SELECT version,key_fingerprint FROM reasoning_schema WHERE singleton=1").fetchone()
            if state["version"] != len(migrations) or state["key_fingerprint"] != key_fingerprint:
                raise ValueError("PostgreSQL snapshot needs the matching schema and original encryption.key")
            target.execute("BEGIN")
            for batch in migrations:
                for statement in batch:
                    sqlite_migration(target, statement)
            target.execute(f"PRAGMA user_version={len(migrations)}")
            expected = {row[0] for row in target.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name<>'sqlite_sequence'")}
            actual = {row[0] for row in source.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=? AND table_name<>'reasoning_schema'",
                (self.config.schema,),
            )}
            if actual != expected:
                raise ValueError("PostgreSQL snapshot schema tables differ from this release")
            target.execute("DELETE FROM installation")  # The only canonical seed row.
            for table in sorted(expected):
                columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
                actual_columns = [row[0] for row in source.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                    (self.config.schema, table),
                )]
                if columns != actual_columns:
                    raise ValueError("PostgreSQL snapshot schema columns differ from this release")
                if table == "work_task_counts":
                    # Insertion of canonical task rows rebuilds derived counts.
                    # Copying these too would count every branch twice.
                    continue
                insert = f'INSERT INTO "{table}" VALUES({",".join("?" for _ in columns)})'
                with source.native.cursor(name="snapshot_" + table) as cursor:
                    cursor.execute(SQL("SELECT * FROM {}").format(Identifier(table)))
                    while rows := cursor.fetchmany(64):
                        target.executemany(insert, (tuple(row[column] for column in columns) for row in rows))
                        pages = target.execute("PRAGMA page_count").fetchone()[0]
                        page_size = target.execute("PRAGMA page_size").fetchone()[0]
                        if time.monotonic() > deadline or pages * page_size > maximum:
                            raise ValueError("PostgreSQL snapshot exceeds its time or byte allowance")
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("PostgreSQL snapshot has inconsistent foreign-key references")
            counts = "SELECT program_id,status,total FROM work_task_counts WHERE total>0 ORDER BY program_id,status"
            source_counts = [tuple(row[key] for key in ("program_id", "status", "total")) for row in source.execute(counts)]
            if source_counts != target.execute(counts).fetchall():
                raise ValueError("PostgreSQL snapshot has inconsistent work status counters")
