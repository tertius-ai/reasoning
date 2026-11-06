"""Private side chats beside a topic, their model replies, and the groups brought from them into the topic.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

Side-chat messages live in their own table, so no topic, search, webhook, export or knowledge path reads them.
A side chat's model reply is an ordinary run of the topic, marked private by ``side_chat_runs``; run listings,
live updates, research views and exports leave marked runs out.
"""

SCHEMA = (
    """CREATE TABLE side_chat_messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, organization_id TEXT NOT NULL,
        topic_id TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id),
        sender_kind TEXT NOT NULL CHECK(sender_kind IN ('human','assistant')),
        content TEXT NOT NULL, client_id TEXT NOT NULL, created_at INTEGER NOT NULL,
        assistant_model TEXT, inference_run_id TEXT, reply_to TEXT, through_sequence INTEGER NOT NULL DEFAULT 0,
        UNIQUE(organization_id,owner_id,client_id),
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""",
    "CREATE INDEX side_chat_message_page ON side_chat_messages(organization_id,topic_id,owner_id,sequence)",
    """CREATE TABLE side_chat_runs (
        request_id TEXT PRIMARY KEY REFERENCES llm_requests(id), organization_id TEXT NOT NULL, topic_id TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES users(id), question_id TEXT NOT NULL REFERENCES side_chat_messages(id),
        through_sequence INTEGER NOT NULL,
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""",
    "CREATE INDEX side_chat_run_owner ON side_chat_runs(organization_id,topic_id,owner_id)",
    """CREATE TABLE side_chat_brought (
        message_id TEXT PRIMARY KEY REFERENCES messages(id), organization_id TEXT NOT NULL, topic_id TEXT NOT NULL,
        owner_id TEXT NOT NULL REFERENCES users(id), side_message_ids_json TEXT NOT NULL,
        omitted_count INTEGER NOT NULL DEFAULT 0 CHECK(omitted_count>=0), created_at INTEGER NOT NULL,
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""",
)
