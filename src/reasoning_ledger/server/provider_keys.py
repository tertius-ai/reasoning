"""The active organization credential, selected consistently across execution and reporting."""


def active_key(db, organization):
    return db.execute(
        "SELECT p.* FROM provider_keys p LEFT JOIN audit_events a ON a.organization_id=p.organization_id "
        "AND a.target_id=p.id AND a.action='provider-key.create' "
        "WHERE p.organization_id=? AND p.provider='openrouter' AND p.disabled_at IS NULL "
        "ORDER BY a.sequence DESC,p.created_at DESC,p.id DESC LIMIT 1", (organization,),
    ).fetchone()
