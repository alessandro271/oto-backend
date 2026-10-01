## prerequisite — your typeform personal access token

in Typeform: **Account → Personal tokens → Generate a new token**, then paste it into oto (format `tfp_…`). Choose the scopes **forms:read**, **responses:read** and **workspaces:read** — the connector only reads.
- byo-only: no shared oto key. The token acts as the account that created it and sees what that account sees
- **data center**: leave it empty (or `us`) unless the account stores its responses in the EU — then `eu` (`api.eu.typeform.com`) or `eu2` (`api.typeform.eu`, the newer EU data center, whose tokens are distinct). Ask the account owner if unsure
- the "test connection" button lists one form (`GET /forms?page_size=1`): it checks the token and its `forms:read` scope, nothing is written

## usage — read forms and their responses

three tools, all read only:
- "which forms do we have?" → `typeform_forms(op="list")`, or `typeform_forms(op="list", search="NPS")`; by workspace → `typeform_workspaces()` then `typeform_forms(op="list", workspace_id="…")`
- "what does this form ask?" → `typeform_forms(op="get", form_id="…")`: each question with its id, ref, type, title and choices
- "the latest responses" → `typeform_responses(form_id="…")` — 25 newest, each as `{question title: value}`
- "responses since Monday" → `typeform_responses(form_id="…", since="2026-09-28T00:00:00")`
- "everything" → page with `before=<next_before>` until `next_before` is gone; `total_items` says how many there are
- "who started but did not finish?" → `typeform_responses(form_id="…", response_type=["partial"])`
- "the responses mentioning Lyon" → `typeform_responses(form_id="…", query="Lyon")`

## note — what is misleading

- ⚠️ **a wrong data center does not fail, it returns nothing**: responses read outside the account's data center come back empty. `typeform_responses` reads the form first and refuses when the form's responses live on another host, naming the setting to change. With `titles=False` that check is skipped
- **`response_type` also changes what the dates filter**: `completed` (default) filters on submission time, `partial` on the last save, `started` on landing
- **the most recent responses (~30 minutes) may not be listed yet** — Typeform's own lag
- **answers carry no question text**: the tool joins them with the form's questions. Two questions with the same title are keyed `title [field id]`, never merged
- **file uploads answer with a URL** (`file_url`): it points to Typeform and needs the token to download — not served here
- `full=True` returns Typeform's raw payload (metadata, tokens, field types) when the readable view is not enough

## note — scope

read only: workspaces, forms and their questions, responses. Creating or editing forms, deleting responses, webhooks, themes, images and translations are not served.
