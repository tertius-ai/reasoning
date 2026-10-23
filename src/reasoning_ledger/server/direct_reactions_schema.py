"""Participant-only reactions and change cursors for direct conversations."""

from .reaction_emoji_schema import EMOJIS

SCHEMA = (
    """CREATE TABLE direct_message_reactions (
        organization_id TEXT NOT NULL, message_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
        emoji TEXT NOT NULL CHECK(emoji IN (""" + ",".join(repr(emoji) for emoji in EMOJIS) + """)),
        PRIMARY KEY(message_id,user_id),
        FOREIGN KEY(organization_id,message_id) REFERENCES direct_messages(organization_id,id)
    )""",
    """CREATE TABLE direct_reaction_changes (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
        FOREIGN KEY(organization_id,message_id) REFERENCES direct_messages(organization_id,id),
        FOREIGN KEY(organization_id,conversation_id) REFERENCES direct_conversations(organization_id,id)
    )""",
    "CREATE INDEX direct_reaction_cursor ON direct_reaction_changes(organization_id,conversation_id,sequence)",
)
