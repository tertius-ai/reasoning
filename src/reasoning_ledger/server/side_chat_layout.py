"""A person's side chat layout, kept with the account so it is the same on every device they sign in on.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

Sizes are shares of the conversation area, so they carry across screens. A wide window and a phone keep separate
parts: ``desktop`` holds the mode (beside the topic, full, or floating), the width beside the topic, and the
floating panel's place; ``phone`` holds the height of the sheet. The routes validate the record's shape.
"""

import json
import time

DEFAULT = {"desktop": {"mode": "side", "width": None, "floating": None}, "phone": {"height": 0.6}}


def side_chat_layout(store, user: str, value: dict | None = None) -> dict:
    """The person's layout, after storing `value` when one is given; the default until they change it."""
    with store.transaction(write=value is not None) as db:
        if value is not None:
            db.execute(
                "INSERT INTO user_side_chat_layouts(user_id,layout_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET layout_json=excluded.layout_json, updated_at=excluded.updated_at",
                (user, json.dumps(value, separators=(",", ":")), int(time.time())),
            )
        row = db.execute("SELECT layout_json FROM user_side_chat_layouts WHERE user_id=?", (user,)).fetchone()
    return json.loads(row[0]) if row else json.loads(json.dumps(DEFAULT))
