"""More human reactions: the emoji check on chat_reactions widens from three choices to the canonical list.

Each person still keeps one reaction per target. The first six are the quick choices; clients use the same order.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

EMOJIS = ("up", "down", "heart", "laugh", "surprised", "sad", "upside_down", "party", "thinking", "eyes", "check", "cross",
          "fire", "idea", "pray", "clap", "rocket", "hundred", "star", "question", "wave", "smile", "wink", "confused")
# A model's, bot's or workflow's reply takes only these: useful, not useful, and confidently wrong.
MODEL_REPLY_EMOJIS = ("up", "down", "upside_down")

# PostgreSQL names an inline column check <table>_<column>_check and runs these as written; SQLite rebuilds the
# table for each statement (schema_operations.sqlite_migration), keeping every row and index.
DROP_CHECK = "ALTER TABLE chat_reactions DROP CONSTRAINT chat_reactions_emoji_check"
ADD_CHECK = ("ALTER TABLE chat_reactions ADD CONSTRAINT chat_reactions_emoji_check CHECK(emoji IN ("
             + ",".join(f"'{emoji}'" for emoji in EMOJIS) + "))")

SCHEMA = (DROP_CHECK, ADD_CHECK)
