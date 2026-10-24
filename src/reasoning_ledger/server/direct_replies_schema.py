"""Replies in direct conversations: a direct message may name the message it answers in the same conversation.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    "ALTER TABLE direct_messages ADD COLUMN reply_to TEXT",
    "CREATE INDEX direct_message_reply_to ON direct_messages(organization_id,conversation_id,reply_to)",
)
