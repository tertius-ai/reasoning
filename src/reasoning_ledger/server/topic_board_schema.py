"""Topic boards: a question, idea cards around it, the routes that lead to it, and boards inside cards.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

A board belongs to a topic and is shared with its readers. A card may open into a board of its own
(``parent_card_id``). Links run from a card to another card or, with no target, to the board's question.
The model sets each card's rank, maturity, theme, promise, effort and reason, and adds gap cards and routes,
only when someone asks it to arrange the board; ``board_runs`` marks those runs.
"""

SCHEMA = (
    """CREATE TABLE topic_boards (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, topic_id TEXT NOT NULL,
        parent_card_id TEXT UNIQUE, question TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, arranged_at INTEGER,
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""",
    "CREATE INDEX topic_board_page ON topic_boards(organization_id,topic_id,created_at)",
    """CREATE TABLE board_cards (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, board_id TEXT NOT NULL REFERENCES topic_boards(id),
        kind TEXT NOT NULL CHECK(kind IN ('idea','gap')), text TEXT NOT NULL,
        author_kind TEXT NOT NULL CHECK(author_kind IN ('human','assistant')), created_by TEXT NOT NULL REFERENCES users(id),
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
        rank INTEGER CHECK(rank IS NULL OR rank>=1),
        maturity TEXT CHECK(maturity IS NULL OR maturity IN ('idea','exploring','settled')),
        theme TEXT, promise REAL CHECK(promise IS NULL OR (promise>=0 AND promise<=1)),
        effort REAL CHECK(effort IS NULL OR (effort>=0 AND effort<=1)), reason TEXT,
        statements_json TEXT NOT NULL DEFAULT '[]'
    )""",
    "CREATE INDEX board_card_page ON board_cards(board_id,created_at)",
    """CREATE TABLE board_links (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, board_id TEXT NOT NULL REFERENCES topic_boards(id),
        source_card_id TEXT NOT NULL REFERENCES board_cards(id), target_card_id TEXT REFERENCES board_cards(id),
        kind TEXT NOT NULL CHECK(kind IN ('route','evidence')), created_by TEXT NOT NULL REFERENCES users(id),
        author_kind TEXT NOT NULL CHECK(author_kind IN ('human','assistant')), created_at INTEGER NOT NULL
    )""",
    "CREATE INDEX board_link_page ON board_links(board_id,created_at)",
    """CREATE TABLE board_runs (
        request_id TEXT PRIMARY KEY REFERENCES llm_requests(id), organization_id TEXT NOT NULL,
        board_id TEXT NOT NULL REFERENCES topic_boards(id), applied_at INTEGER
    )""",
    "CREATE INDEX board_run_board ON board_runs(board_id)",
    "ALTER TABLE side_chat_messages ADD COLUMN card_id TEXT",
)
