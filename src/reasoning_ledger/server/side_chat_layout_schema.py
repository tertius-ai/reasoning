"""Where each person keeps their side chat: one layout for the account, and whether it is open in each topic.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

The layout is one JSON record per person (see ``side_chat_layout``). Whether the side chat is open is kept per topic
beside the person's read position, so the topic list says it before the topic is drawn.
"""

SCHEMA = (
    """CREATE TABLE user_side_chat_layouts (
        user_id TEXT PRIMARY KEY REFERENCES users(id), layout_json TEXT NOT NULL, updated_at INTEGER NOT NULL
    )""",
    "ALTER TABLE topic_subscriptions ADD COLUMN side_chat_open INTEGER NOT NULL DEFAULT 0 CHECK(side_chat_open IN (0,1))",
)
