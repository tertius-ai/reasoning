"""Direct conversations between a member and their own bot: a bot run belongs to a topic or to such a conversation."""

from .schema_operations import BOT_DIRECT_COLUMN, BOT_DIRECT_RUNS

SCHEMA = (
    *BOT_DIRECT_RUNS,
    BOT_DIRECT_COLUMN,
    "CREATE INDEX IF NOT EXISTS bot_run_direct ON bot_runs(direct_conversation_id,created_at)",
)
