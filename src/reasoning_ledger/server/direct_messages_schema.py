"""One-to-one direct conversations, kept apart from the project/channel/topic hierarchy.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    """CREATE TABLE direct_conversations (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
        first_user_id TEXT NOT NULL REFERENCES users(id), second_user_id TEXT NOT NULL REFERENCES users(id),
        created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
        CHECK(first_user_id < second_user_id),
        UNIQUE(organization_id,id), UNIQUE(organization_id,first_user_id,second_user_id)
    )""",
    "CREATE INDEX direct_conversation_second ON direct_conversations(organization_id,second_user_id)",
    """CREATE TABLE direct_messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, organization_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, author_id TEXT NOT NULL REFERENCES users(id), content TEXT NOT NULL,
        client_id TEXT NOT NULL, created_at INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, edited_at INTEGER,
        UNIQUE(organization_id,id), UNIQUE(organization_id,author_id,client_id),
        FOREIGN KEY(organization_id,conversation_id) REFERENCES direct_conversations(organization_id,id)
    )""",
    "CREATE INDEX direct_message_page ON direct_messages(organization_id,conversation_id,sequence)",
    """CREATE TABLE direct_message_revisions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        organization_id TEXT NOT NULL, message_id TEXT NOT NULL, revision INTEGER NOT NULL,
        content TEXT NOT NULL, edited_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
        UNIQUE(message_id,revision),
        FOREIGN KEY(organization_id,message_id) REFERENCES direct_messages(organization_id,id)
    )""",
    "CREATE INDEX direct_message_revision_updates ON direct_message_revisions(organization_id,sequence)",
    """CREATE TRIGGER direct_message_revision_immutable_update BEFORE UPDATE ON direct_message_revisions
        BEGIN SELECT RAISE(ABORT,'message revisions are immutable'); END""",
    """CREATE TRIGGER direct_message_revision_immutable_delete BEFORE DELETE ON direct_message_revisions
        BEGIN SELECT RAISE(ABORT,'message revisions are immutable'); END""",
    """CREATE TABLE direct_conversation_state (
        organization_id TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL,
        last_read_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_read_sequence>=0),
        muted INTEGER NOT NULL DEFAULT 0 CHECK(muted IN (0,1)),
        PRIMARY KEY(organization_id,conversation_id,user_id),
        FOREIGN KEY(organization_id,conversation_id) REFERENCES direct_conversations(organization_id,id),
        FOREIGN KEY(organization_id,user_id) REFERENCES memberships(organization_id,user_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE direct_blocks (
        organization_id TEXT NOT NULL REFERENCES organizations(id), user_id TEXT NOT NULL REFERENCES users(id),
        blocked_user_id TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
        CHECK(user_id <> blocked_user_id),
        PRIMARY KEY(organization_id,user_id,blocked_user_id)
    )""",
)
