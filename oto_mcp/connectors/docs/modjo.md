## prerequisite — your Modjo API key

an **admin or a manager** creates it in Modjo: **Settings → Integrations → "Public API"**, then pastes it into oto.
- byo-only: no shared oto key — these are your company's recorded calls, each organization sets its own
- ⚠️ **it is a workspace key**: it does not represent any particular person and reads **every member's calls**
- the "test connection" button reads one tag of the workspace (`GET /tags`): no call data is touched, nothing is written

## usage — calls and what Modjo drew from them

- "my calls this week" → `modjo_workspace(op="users", email="me@…")` for my id, then `modjo_calls(op="list", user_id=…, date_from="2026-10-05T00:00:00Z", expand=["contacts", "deal"])` — without `user_id`, the list covers **the whole workspace**
- "the summary of this call" → `modjo_calls(op="summaries", call_id="…")`, or everything at once: `modjo_calls(op="get", call_id="…", expand=["summaries", "next-steps", "notes", "contacts"])`
- "what was said, word for word" → `modjo_calls(op="transcript", call_id="…")` — readable lines by slices of 30,000 characters; pass `next_offset` back as `offset`
- "the next steps agreed with Acme" → `modjo_crm(op="accounts", name="Acme")` for the id, then `modjo_calls(op="list", account_id=…)`, then `op="next_steps"` on the call
- "where does this deal stand?" → `modjo_crm(op="deal_summary", deal_id=…)`
- "did the prospect mention a budget?" → `modjo_ask(scope_type="call", scope_id="…", question="Did the prospect mention a budget? How much?", answer_schema={"type": "object", "properties": {"mentioned": {"type": "boolean"}, "amount": {"type": "string"}}})` — up to 5 minutes
- "this month's coaching reviews" → `modjo_reviews(start_date_time="2026-10-01T00:00:00Z", order="asc")`
- tag a call → `modjo_workspace(op="tags")` for the id, then `modjo_calls(op="add_tag", call_id="…", tag_id=…)`
- import a call recorded elsewhere → `modjo_calls(op="upload", media_url="https://…", date="…", participants=[{"email": "me@…", "type": "user"}, {"email": "client@…", "type": "contact"}])` returns a **preview**; the same call with `confirm=True` imports

## note — what is misleading

- ⚠️ **the list is not "my calls"**: the key sees the whole workspace. Filter by `user_id` for one person
- **a call is analysed 5 to 30 minutes after it is recorded or imported**: `status` goes `processing` → `completed` (or `failed`); until then transcript, summaries and next steps are empty, and `modjo_ask` answers 409
- ⚠️ **an imported call cannot be deleted through the API**, and Modjo logs it in the connected CRM (it updates the activity `crm_activity_id` when given, creates one otherwise) — hence the preview. At least one `user` participant must be a licensed member, otherwise Modjo refuses it: the preview says whether they are
- **the recording URL (`op="recording"`) is signed for one hour**: never store it, ask again for a fresh one. A 410 means the retention policy deleted it
- **a list only expands light relations** (participants, deal, account, users, tags, AI scoring): summaries, notes, next steps, topics and CRM answers are expanded on one call (`op="get"`), the transcript only through `op="transcript"`
- **notes**: only published ones are returned. **CRM answers**: only the values actually pushed to the CRM
- `modjo_ask` is refused (403) when the feature is not enabled for the workspace — it is switched on by Modjo

## note — usage limits

**100 requests per minute for the whole workspace**, all keys together, failed requests included. On a 429 the client waits for the window to reset, then retries a read once more; an import or a question to the AI is never replayed.

## note — scope

calls, reviews, the CRM objects Modjo mirrors, and the workspace directory — **read**; tags of a call and the import of a call — **write**. Administering members, teams and webhooks is not served here.
