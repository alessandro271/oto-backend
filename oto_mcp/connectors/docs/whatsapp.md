## prerequisite — connect your WhatsApp account

your WhatsApp is connected as a **linked device** — like WhatsApp Web, but the session runs on a hosted browser.
- the subscription key lives on the **Unipile account** connector (your BYO key, or the platform's with the **hosted messaging** option granted by an admin);
- then "Connect my WhatsApp account" here. no cookie to paste, no extension.

## usage — read and reply from your account

`whatsapp_chat(op=list|read|send)` — you act as yourself, under your own account.
- "read my latest WhatsApp conversations"
- "reply to [contact]: let's meet Thursday at 2pm"
- "summarize the thread with [contact] since Monday"

## note — the 100 most recent threads

`op=list` returns at most 100 threads per call, newest first, and Unipile does not paginate WhatsApp: an older thread is read by its `chat_id`, not reached by the list. a message whose photo, voice note or poll Unipile did not pass on is marked `content_unavailable` — its content is unknown, not empty.

## note — the phone stays the master of the account

your phone stays the master of the account: if it unlinks the device, the session dies and the card goes back to needing a connection.
