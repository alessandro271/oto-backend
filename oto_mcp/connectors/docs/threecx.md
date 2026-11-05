## prerequisite — your 3cx credentials

the connector reads your 3cx phone system with its address and one of the two credentials below, chosen by `auth_mode`. the rights of that credential bound what is visible.
- `base_url` — the https address of the 3cx web client, the one in the address bar when you open 3cx in your browser (e.g. `https://your-company.3cx.fr`)
- `auth_mode=user`: `username` and `password` — a 3cx account that sees the calls and recordings of the desired groups (a group manager is enough); two-factor authentication must be disabled on it; a dedicated account is better than a personal one
- `auth_mode=api_client`: `client_id` and `client_secret` — an api client created in the 3cx admin console (integrations > api), if your license allows it; system admin role to see the whole phone system
- reading and changing the configuration needs an admin role on that credential (system admin, or system owner for system-wide settings)
fill in `base_url`, `auth_mode` and the matching pair in your oto connector keys under `3cx`

## usage — call traces and recordings

- `threecx_call(date_from=…, date_to=…)` one page of the call log; `recorded_only=true` keeps the recorded segments; continue with `skip=next_skip` as long as `next_skip` is not null
- `threecx_call(op="export", date_from=…, date_to=…)` the whole period as a csv file (`;` separator, durations in seconds): the call traces of a day or a week, instead of a daily export
- `threecx_recording(rec_id=…)` the audio of a recording, using the `SrcRecId` or `DstRecId` of a call log row; kept one day and returned as a short `source` (`{"kind":"transit","ref":…}`)
- to transcribe it: `transcription_create(source=<that source, as is>, _project=…)` — never a copied url; `link=true` adds a short-lived signed link, to listen to it

## usage — configuration (extensions, queues, ivrs, inbound rules, office hours, forwarding)

- `threecx_config(op="catalog")` every resource of your phone system; `op="schema", resource=…` its key, properties (allowed values included) and operations
- `threecx_config(op="read", resource=…, key=…, select=[…], filter=…)` the current setup, e.g. `resource="InboundRules"` (which public number rings where), `resource="Queues", key=…, navigation="Agents"`
- `threecx_act(op="update", resource=…, key=…, body={…})` a change, e.g. reroute a number: `resource="InboundRules", key=<Id>, body={"OfficeHoursDestination": {"To": "Queue", "Number": "8003"}}`
- **a preview by default**: before → after per property, nothing written; `dry_run=false` writes, then reads back what 3cx holds (`verified`)
- a change is **live at once** on real calls: check the preview with the person first
- `op="create"`, `op="delete"`, `op="call"` (an action listed by `op="schema"`) follow the same preview rule
- passwords, sip secrets, provisioning links, api and licence keys are shown as `***` and never written: the 3cx admin console sets them
- the call log and recordings are never written
