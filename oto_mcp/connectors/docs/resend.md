## prerequisite — a resend api key with full access

the key is your organization's, set by an org admin on the **resend** connector. it serves two things: the platform's email sending (`email_send`, from a domain verified in your resend account) and the contact list your broadcasts go to.
- in [resend](https://resend.com/api-keys), create an api key with permission **full access**
- a **sending access** key keeps sending emails, but resend refuses it on contacts, segments and topics: the tools then say so. a full-access key also sends, so replacing the key never breaks sending
- byo-only: your organization's key, no platform key

## usage — keep a broadcast audience up to date

- "add these people to the newsletter segment" → `resend_contact(op="upsert", contacts=[{"email": …, "first_name": …}], segment_ids=["…"])` — up to 50 per call, with a receipt; `dry_run=true` first to preview
- "who is in the segment?" → `resend_contact(op="list", segment_id="…")` (pass `next_after` as `after` for the next page)
- "who unsubscribed?" → `resend_contact(op="list", segment_id="…", unsubscribed=true)`
- "what do we know about this person?" → `resend_contact(op="get", contact="<email or id>")` — with segments and topic subscriptions
- "this person asked to stop receiving the newsletter" → `resend_contact(op="update", contact="…", unsubscribed=true)`
- "list the segments" / "create a segment" → `resend_segment(op="list")` / `resend_segment(op="create", name="…")`
- "which topics can people subscribe to?" → `resend_topic(op="list")`

## note — what the tools will not do

- ⚠️ **an upsert never resubscribes** someone who unsubscribed: the receipt lists them under `kept_unsubscribed`. resubscribing is an explicit `op="update"` with `unsubscribed=false`, on the person's own request
- ⚠️ **deleting a contact drops their opt-out**: to stop emailing someone, set `unsubscribed=true` instead
- topics are read-only: the recipient changes their own topic subscriptions from the unsubscribe page
- no broadcast is created or sent from here
