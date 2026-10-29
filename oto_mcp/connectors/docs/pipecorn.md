## prerequisite — a Pipecorn API key

create a key in Pipecorn (Settings → API keys) and paste it into oto. Reference: [Pipecorn authentication](https://docs.pipecorn.com/essentials/authentication).
- a **test key** (`pc_test_…`) answers every lookup with sample data and spends nothing: a good way to try the connector before your live key
- your own key: every lookup spends **your** Pipecorn credits; each organization or person sets its own key

## usage — a phone, an email

- a lookup is **asynchronous**: `pipecorn(...)` submits it and answers at once with an `enrichment_id`; `pipecorn(op="result", enrichment_id=…, find=[…])` reads it (free) — `done: false` while Pipecorn is still looking, read again a few seconds later (usually under a minute)
- "find Jane's mobile" → `pipecorn(find=["phone"], linkedin_url="https://www.linkedin.com/in/jane-doe/", phone_type="mobile")`, then `op="result"`
- "her work email" → `pipecorn(find=["email"], linkedin_url=…)`, or without a profile `pipecorn(find=["email"], firstname="Jane", lastname="Doe", domain="acme.com")`
- both at once → `find=["email", "phone"]`: one lookup, each result billed when found
- phones from some countries only → `phone_country_codes=["FR", "BE"]`
- "how many credits are left?" → `pipecorn(op="credits")`: `credits.enrichment` is what lookups spend
- for a person's phone, prefer `waterfall_phone`: Pipecorn first, submitted and read for you within the call's time budget, then your other providers. Its table mode `waterfall_phone_act(op="update_rows")` leaves Pipecorn out, like every asynchronous step

## note — what can mislead

- you pay **per result found**, in Pipecorn credits, when the lookup finishes: 3 a work email, 30 a phone — and a phone only when a **mobile** is found; a miss costs nothing. The finished result says what it cost (`pipecorn_credits`); reading it again costs nothing more
- a **third party's personal email** is never looked up: `find=["personal_email"]` is refused, whatever the key
- pass the same `find` to `op="result"` as to the submission: only what was asked is counted
- a lookup can end `failed` when the credits ran out while it ran: nothing found, nothing billed
- a miss is **not an error**: empty fields, `phone_status: "not_found"`
- a number comes back as Pipecorn writes it, with or without its `+`: no country is guessed for it
- `phone_type="mobile"` keeps looking until a provider has a mobile and drops landlines; without it, a landline can come back, unbilled
- a number your Pipecorn workspace suppressed comes back as the text `"Contacts not shared"`, not a number
- a lookup is **never retried** automatically
