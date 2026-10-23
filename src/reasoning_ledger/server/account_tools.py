"""Research-account tools and schemas shared by native agents and connected bots."""

from .research_accounts import FIELDS, LINKS, ResearchAccounts


def schemas():
    text = {field: {"type": "string", "maxLength": maximum} for field, maximum in FIELDS.items()}
    links = {
        field: {"type": "array", "maxItems": 40, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 100}}
        for field in LINKS
    }

    def obj(properties, required=()):
        return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

    return {
        "account_read": obj(
            {
                "revision": {"type": "integer", "minimum": 1},
                "field": {"enum": [*FIELDS, "links"]},
                "offset": {"type": "integer", "minimum": 0},
                "length": {"type": "integer", "minimum": 1, "maximum": 12000},
            }
        ),
        "account_update": obj(
            {
                "changes": obj(text | links),
                "expected_revision": {"type": "integer", "minimum": 0},
                "expected_links": {"type": "object", "maxProperties": 120, "additionalProperties": {"type": ["string", "integer"]}},
            },
            ["changes", "expected_revision"],
        ),
        "account_request_review": obj({"revision": {"type": "integer", "minimum": 1}}, ["revision"]),
    }


DESCRIPTIONS = {
    "account_read": "Read this topic's pinned, versioned mathematical account. "
    "Read exact revisions and field slices for a large account; field=links returns its exact evidence pins.",
    "account_update": "Maintain the current question, hypotheses, coherent argument, sources, failed routes, gaps and next steps. "
    "Send only changed fields, preserving omitted fields. expected_revision=0 starts an account. Link reusable claim/reference/entry IDs; "
    "their exact revisions must be supplied in expected_links. Saving does not establish mathematics. "
    "Read the full field before replacing it.",
    "account_request_review": "Request fresh-context critique of an exact account revision. First retain a useful answer/checkpoint. "
    "At most two reviews per approach; repeated unchanged arguments reuse their review. Yield after queuing; do not poll. "
    "A favorable critique does not promote ledger status.",
}


def read_account(accounts, user, org, topic, revision=None, field=None, offset=0, length=12000):
    value = accounts.read(user, org, topic, revision)
    if value is None or field is None:
        return value
    body = value.pop("body")
    value.pop("history", None)
    value.pop("linked_changes", None)
    from .research_context import encoded

    text = encoded(body.get("pins", {})) if field == "links" else body[field]
    value.update(
        {
            "field": field,
            "text": text[offset : offset + length],
            "offset": offset,
            "total_characters": len(text),
            "truncated": offset + length < len(text),
            "linked_record_count": sum(len(items) for items in body.get("pins", {}).values()),
        }
    )
    return value


def register(research):
    accounts = ResearchAccounts(research.store)
    inputs = schemas()
    research.registry.tool(name="account_read", description=DESCRIPTIONS["account_read"], input_schema=inputs["account_read"])(
        lambda **args: read_account(accounts, research.user, research.org, research.topic, **args)
    )
    research.registry.tool(
        name="account_update", description=DESCRIPTIONS["account_update"], risk="write", input_schema=inputs["account_update"]
    )(
        lambda changes, expected_revision, expected_links=None: accounts.update(
            research.user,
            research.org,
            research.topic,
            changes,
            expected_revision,
            "account-" + research.row["id"] + "-" + str(research.sequence),
            run_id=research.row["id"],
            expected_links=expected_links,
        )
    )
    from .research_reviews import ResearchReviews

    research.registry.tool(
        name="account_request_review",
        description=DESCRIPTIONS["account_request_review"],
        risk="write",
        input_schema=inputs["account_request_review"],
    )(lambda revision: ResearchReviews(research.inference).request(research, account_revision=revision))
