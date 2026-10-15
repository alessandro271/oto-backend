"""Resend tools — segments and topics.

The second module of the `resend` connector (`tools/resend.py` holds the
contacts; `resend_socle.py` the shared base, key and refusals included).

- `resend_segment` — list, read, create the groups a Broadcast is sent to;
- `resend_topic` — list, read, READ-ONLY: the recipient sets their own topic
  subscriptions, from the unsubscribe page.

Client calls are written out in plain form (`_client().list_segments(…)`):
that is what the version-skew probe (`test_tools_client_methods_exist`) reads.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .lecture import LECTURE
from .resend_socle import _client, bad_op, need, page_out, run, slim


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def resend_segment(
        op: Literal["list", "get", "create"] = "list",
        segment_id: Optional[str] = None,
        name: Optional[str] = None,
        limit: int = 50,
        after: Optional[str] = None,
        full: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Resend segments — the groups of contacts a Broadcast is sent to. A segment
        is internal: recipients never see its name. Its members are read with
        `resend_contact(op="list", segment_id=…)`; a contact joins one through
        `resend_contact(op="upsert", segment_ids=[…])` or `op="add_to_segment"`.

        `op`: **"list"** (default) the account's segments, paginated (`limit`,
        `after`); **"get"** one segment (`segment_id`); **"create"** a segment
        (`name`). No delete here: a segment is removed from the Resend dashboard.

        Needs a Resend key with Full access (a sending-only key is refused, and the
        error says how to fix it). Returns — list: `{segments: [{id, name,
        created_at}], has_more, next_after}`, pass `next_after` as `after` for the
        next page, `full=true` returns Resend's raw page; get: the segment; create:
        `{id, name}`, or `{dry_run, would_create}`.

        Args:
            op: list (default) | get | create.
            segment_id: op="get" — the segment id.
            name: op="create" — the segment name.
            limit: op="list" — page size, 1 to 100 (default 50).
            after: op="list" — cursor: the `next_after` of the previous page.
            full: op="list"/"get" — Resend's raw response instead of the trimmed view.
            dry_run: op="create" — preview, no call.
        """
        if op == "list":
            return run(lambda: page_out(
                "segments", _client().list_segments(limit=limit, after=after), full, slim))
        if op == "get":
            need(segment_id, "segment_id", op)
            segment = run(lambda: _client().get_segment(segment_id))
            return segment if full else slim(segment)
        if op == "create":
            need(name, "name", op)
            if dry_run:
                return {"dry_run": True, "would_create": {"name": name.strip()}}
            return slim(run(lambda: _client().create_segment(name)))
        raise bad_op(op, "list | get | create")

    @mcp.tool(annotations=LECTURE)
    def resend_topic(
        op: Literal["list", "get"] = "list",
        topic_id: Optional[str] = None,
        limit: int = 50,
        after: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Resend topics, read-only — the subjects a contact opts in to or out of
        (e.g. "Product updates"), shown to the recipient on the unsubscribe page.
        The RECIPIENT changes their subscription; one contact's subscriptions are in
        `resend_contact(op="get")`. A Broadcast scoped to a topic skips the contacts
        who opted out of it.

        `op`: **"list"** (default) the account's topics, paginated (`limit`,
        `after`); **"get"** one topic (`topic_id`).

        Needs a Resend key with Full access (a sending-only key is refused).
        Returns — list: `{topics: [{id, name, description, default_subscription,
        visibility, created_at}], has_more, next_after}`; get: the topic.

        Args:
            op: list (default) | get.
            topic_id: op="get" — the topic id.
            limit: op="list" — page size, 1 to 100 (default 50).
            after: op="list" — cursor: the `next_after` of the previous page.
            full: Resend's raw response instead of the trimmed view.
        """
        if op == "list":
            return run(lambda: page_out(
                "topics", _client().list_topics(limit=limit, after=after), full, slim))
        if op == "get":
            need(topic_id, "topic_id", op)
            topic = run(lambda: _client().get_topic(topic_id))
            return topic if full else slim(topic)
        raise bad_op(op, "list | get")
