## prerequisite — you need a Slack app installed on your workspace

oto does not yet have a published Slack app ("connect in one click"): you create **your** app in your workspace and paste its tokens here. A one-time task per workspace, ~5 minutes.
- **bot token** (`xoxb-`): read channels, post under the app's identity. This is the default token.
- **user token** (`xoxp-`): post **in your name**, and search (`search:read` only exists as a user token). Optional.
- either one is enough. With **both**, oto reads with the bot and makes you **choose who writes** on each send: `author="me"` (in your name) or `author="app"` (under the app's name) — with no choice, the send is refused rather than going out under the wrong name. The response says who wrote and in which channel, and whether that channel is shared externally.
- otherwise, an admin can grant you your org's platform key

## setup — create the app by pasting a manifest (the shortest way)

1. go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → choose **From a manifest** (not "AI agent" nor "Starter app": those are conversational-app templates, unrelated)
2. choose the workspace. The editor that opens is **already filled** with an example (`display_information: name: Demo App`): **select all and replace it** — pasting over it without clearing welds the two manifests together and Slack refuses ("Nested mappings are not allowed in compact mappings"). The manifest to put in its place, which already declares all the scopes the oto tools need:

```yaml
display_information:
  name: Oto
  description: Reads and writes in Slack for your Oto agent
features:
  bot_user:
    display_name: Oto
    always_online: false
oauth_config:
  scopes:
    bot:
      - channels:read
      - channels:history
      - channels:join
      - groups:read
      - groups:history
      - im:read
      - im:history
      - im:write
      - mpim:read
      - mpim:history
      - users:read
      - users:read.email
      - chat:write
      - reactions:write
      - files:read
      - usergroups:read
      - usergroups:write
    user:
      - search:read
      - chat:write
      - usergroups:read
      - usergroups:write
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

3. **Create**, then **Install to Workspace** and authorize
4. in **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-`) and, if you want to post in your name, the **User OAuth Token** (`xoxp-`)
5. paste them here — nothing else to configure

*(without a manifest: **Blank app**, then declare the same scopes by hand in OAuth & Permissions before installing. The manifest avoids exactly this step.)*

⚠️ **a scope does not replace channel membership.** without membership, Slack answers `not_in_channel` — which wrongly looks like a token problem. two cases, and only one can be automated:
- **public channel**: `slack_join_channel` gets the app in on its own (this is what the manifest's `channels:join` scope above is for)
- **private channel**: no Slack API lets you invite yourself. a human who is already a member must type `/invite @Oto` in the channel — oto cannot do it for them, and says so instead of letting you believe it's an outage

Slack reference: [create an app from a manifest](https://api.slack.com/reference/manifests) · [install with oauth v2](https://api.slack.com/authentication/oauth-v2)

## setup — a second workspace, a second installation

a Slack token is issued **per installation**: two workspaces = two independent sets of tokens. Redo the installation in the second workspace (same manifest), then set its tokens as a second workspace of the connector (section "several workspaces").

## setup — start an agent on every Slack message (Event Subscriptions)

the same app can **start a hosted agent** each time someone writes in a channel it is in — no relay, no polling. Slack posts each event to the agent's address and signs it with the app's **Signing Secret**.
1. create a **webhook** agent (`oto_trigger op=create kind=webhook`, usually `payload_mode="inline"` so it sees the channel and message); copy its `hook_url`
2. on the agent's page, set **how the sender proves who it is** to **Slack** and paste the app's **Signing Secret** (api.slack.com/apps → your app → **Basic Information** → App Credentials — *not* the `xoxb-` token). It is stored encrypted and never shown again; it is set on the screen, never through a tool
3. in the app: **Event Subscriptions** → **Enable Events** → **Request URL** = the agent's `hook_url`. Slack shows **Verified** within seconds: the agent answers Slack's challenge once the signature checks out
4. **Subscribe to bot events**: `message.channels` (public channels), `message.groups` (private channels, Slack Connect included), optionally `message.im` — the manifest above already has the matching `*:history` scopes. **Save Changes**, and reinstall the app if Slack asks
5. optional but recommended, a **filter** so internal chatter costs no run: `oto_trigger op=update hook_filter={"is_ext_shared_channel": [true]}` (only channels shared with another company), or `{"event.channel": ["C…", "C…"]}` (only those channels). A filtered event gets a success and starts nothing

what never starts a run, whatever the filter: messages from bots — **the agent's own posts included**, so an agent that writes in a channel its app listens to does not trigger itself —, edits, deletions, joins, and Slack's retries of an event already accepted (same `event_id`).

⚠️ **one Request URL per app**: every subscribed event goes to that one agent. Two agents on the same events = two Slack apps, or one agent whose procedure routes.
⚠️ **a paused agent answers 409**, which Slack counts as a failure: after many failures in an hour Slack turns the subscription off (it says so on the Event Subscriptions page — switch it back on there).
⚠️ the app only receives events of channels it is a **member** of: `/invite` it in each private or Slack Connect channel, as above.

Slack reference: [Events API](https://api.slack.com/apis/events-api) · [verifying requests from Slack](https://api.slack.com/authentication/verifying-requests-from-slack)

## usage — what you can do

send and read slack messages from claude — in your name or under the app's, depending on the tokens set.
- "send a message in #general" → `slack_post_message`
- "dm jean by email" → `slack_find_user_by_email` then `slack_open_dm` then `slack_post_message`
- "read the latest messages of this channel" → `slack_read_history` (top-level messages; `oldest`/`latest` to read only a window)
- "what was said in this thread?" → `slack_read_thread` — a thread's **replies**, which `slack_read_history` does not return (it only shows the count)
- "get oto into #channel" → `slack_join_channel` (public channels only)
- "react 👍 to this message" → `slack_add_reaction`
- "who is in @sales?" → `slack(op="usergroups")` (all groups), `slack(op="usergroup_members", usergroup=…)` (one group's members)
- "create @support / add jean to @sales / rename a group" → `slack_act` (`op="create_usergroup"`, `"add_usergroup_members"`, `"update_usergroup"`…; `dry_run=true` shows the member diff first)

⚠️ **user groups**: they require a paid Slack plan. An app installed before these scopes were added must be **reinstalled** (OAuth & Permissions → add `usergroups:read` / `usergroups:write` → Reinstall), then its new tokens set here. Writing with the **bot** token only works when the workspace lets **everyone** manage user groups; otherwise write with `author="me"` and the user token of a member allowed to (an admin, by default). Slack replaces a group's member list as a whole and refuses an empty one — a group is emptied by disabling it.
