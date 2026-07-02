"""Retrieval tools — surface prior resolutions as few-shot context.

v1 uses Postgres full-text search over the ticket's subject + description.
Vector / embedding search is deliberately deferred until we have eval
evidence that lexical recall is the bottleneck; customers tend to describe
the same problem with overlapping vocabulary, so lexical goes further than
it does in general search.
"""

from __future__ import annotations

FIND_SIMILAR_TICKETS_SCHEMA = {
    "name": "find_similar_tickets",
    "description": (
        "Retrieve previously-resolved helpdesk tickets whose subject or "
        "description overlaps with the query. Use this as few-shot context "
        "before deciding which tool to call — the returned tool sequence "
        "shows how a similar past issue was actually resolved. Do NOT use "
        "this to look up the current customer; use lookup_customer for that."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language description of the issue to match. "
                    "Usually the ticket subject or a phrase from the "
                    "description works best."
                ),
                "minLength": 3,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


def find_similar_tickets(env, ticket, query, limit=5):
    """Return the top-N lexically-similar resolved tickets with their tool history."""
    limit = max(1, min(int(limit or 5), 10))
    cr = env.cr
    # to_tsvector on ticket text; ts_rank ranks by classical FTS score.
    # Restrict to tickets whose AI resolution actually succeeded so the
    # tool sequence returned is a template for success, not failure.
    cr.execute(
        """
        SELECT
            t.id,
            ts_rank(
                to_tsvector('english', coalesce(t.name, '') || ' ' || coalesce(t.description, '')),
                plainto_tsquery('english', %s)
            ) AS rank
        FROM ai_helpdesk_ticket t
        WHERE t.id != %s
          AND t.ai_resolution_status = 'resolved'
          AND to_tsvector('english', coalesce(t.name, '') || ' ' || coalesce(t.description, ''))
              @@ plainto_tsquery('english', %s)
        ORDER BY rank DESC
        LIMIT %s
        """,
        (query, ticket.id, query, limit),
    )
    rows = cr.fetchall()
    if not rows:
        return {"ok": True, "data": {"tickets": []}}
    hits = env["ai.helpdesk.ticket"].sudo().browse([row[0] for row in rows])
    return {
        "ok": True,
        "data": {
            "tickets": [
                {
                    "id": hit.id,
                    "subject": hit.name,
                    "category": hit.category or "",
                    "tool_sequence": hit.action_ids.sorted("sequence").mapped("tool_name"),
                    "resolution_notes": (hit.ai_resolution_reasoning or "")[:400],
                }
                for hit in hits
            ],
        },
    }


RETRIEVAL_TOOLS = {
    "find_similar_tickets": {
        "schema": FIND_SIMILAR_TICKETS_SCHEMA,
        "callable": find_similar_tickets,
        "class": "read",
    },
}
