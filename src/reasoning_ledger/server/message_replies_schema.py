"""Replies: a topic message may name the message it answers in the same topic; older rows stay null.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    "ALTER TABLE messages ADD COLUMN reply_to TEXT",
    "CREATE INDEX messages_reply_to ON messages(organization_id,topic_id,reply_to)",
)
