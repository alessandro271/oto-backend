## prerequisite — claap api key

a Claap workspace **admin** creates a key in Claap → Settings → **API & Webhooks**, then pastes it into oto (format `cla_…`).
- byo-only: no shared oto key — these are your company's recorded meetings, each organization sets its own
- ⚠️ **it is a workspace key that acts as admin**: it does not represent any particular person
- **private** recordings are only visible if the key was created with **Full access**
- the "test connection" button reads the key's workspace (`/v1/workspaces/mine`): no meeting data is touched

## usage — recorded meetings, and what was taken from them

- "my latest meetings" → `claap_recordings(op="list", recorder_email="me@…")` — without `recorder_email`, the list covers **the whole workspace**
- "the summary of this meeting" → `claap_recordings(op="get", recording_id="…")`: `keyTakeaways`, `outlines`, `actionItems` and AI fields (`aiFields`) are there
- "what was said, word for word" → `claap_recordings(op="transcript", recording_id="…")` (readable text by default; `format="json"` for timed segments, much heavier)
- "this week's Zoom meetings" → `claap_recordings(op="list", created_after="…", sources=["Zoom"])`
- "the meetings in the *Demos* view" → `claap_recording_views(op="list")` to find its id, then `claap_recordings(op="list", view_id="…")`
- import a meeting recorded elsewhere → `claap_recordings(op="create", author_email="…", video_url="https://…")`, or from its transcript alone: `transcript={"segments": [{"start": 0.5, "end": 1.5, "speakerId": "1", "text": "…"}], "speakers": [{"speakerId": "1", "name": "…"}]}`
- delete → `claap_recordings(op="delete", recording_id="…", dry_run=True)` first: deletion is **permanent**

## note — what is misleading

- ⚠️ **the list is not "my meetings"**: the key sees everything visible in global search, colleagues' meetings included. Filter by `recorder_email` for one person
- ⚠️ **a 404 on `op="get"` does not necessarily mean "does not exist"**: a private recording, or one in a folder hidden from search, returns 404 to the API
- **`op="get"` has two shapes**: only `state="Ready"` carries the summary and transcripts; `Empty` / `Uploaded` / `Failed` return a thin record. A freshly created recording goes `Empty` → `Uploaded` → `Ready` (or `Failed`): read it again later
- **video and transcript URLs are signed and expire after 24 h**: never store them, re-read the recording to get fresh ones
- **`labels` only filters together with `channel_id`** (a folder) — a Claap rule, refused before the call
- the **transcript you provide** on creation does not have the shape of the transcript you read: `start` / `end` / `speakerId` when writing, versus `startedAt` / `endedAt` / `speaker` when reading. Copying a read transcript as-is is refused
- **creation consumes the Claap plan's recording quota**: once reached ⇒ 403 "Recording quota exceeded", nothing is created — this is fixed on the Claap subscription side
- if the transcript upload fails after creation, **the recording already exists, empty**: the message gives its id, delete it before retrying

## note — usage limits

3 requests/s per endpoint, and **3,000 per day for the whole workspace**, shared by all its keys. Reads are retried on a 429; a create or a delete never is (the API has no idempotency key, a replay would create a duplicate).

## note — scope

this connector covers **recordings** (and reading recording views). The Claap API also exposes deals, companies, contacts, folders, AI fields, automations and users: not served here.
