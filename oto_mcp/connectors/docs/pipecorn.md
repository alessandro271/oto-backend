## prerequisite — a Pipecorn API key

create a key in Pipecorn (Settings → API keys) and paste it into oto. Reference: [Pipecorn authentication](https://docs.pipecorn.com/essentials/authentication).
- a **test key** (`pc_test_…`) answers every lookup with sample data and spends nothing: a good way to try the connector before your live key
- your own key: every lookup spends **your** Pipecorn credits; each organization or person sets its own key

## usage — a phone, an email, a profile, a company

- a lookup is **asynchronous**: `pipecorn(...)` submits it and answers at once with an `enrichment_id`; `pipecorn(op="result", enrichment_id=…, find=[…])` reads it (free) — `done: false` while Pipecorn is still looking, read again a few seconds later (usually under a minute)
- "find Jane's mobile" → `pipecorn(find=["phone"], linkedin_url="https://www.linkedin.com/in/jane-doe/", phone_type="mobile")`, then `op="result"`
- "her work email" → `pipecorn(find=["email"], linkedin_url=…)`, or without a profile `pipecorn(find=["email"], firstname="Jane", lastname="Doe", domain="acme.com")`
- both at once → `find=["email", "phone"]`: one lookup, each result billed when found
- phones from some countries only → `phone_country_codes=["FR", "BE"]`
- "how many credits are left?" → `pipecorn(op="credits")`: `credits.enrichment` is what lookups spend
- "what does Jane do now?" → `pipecorn(op="person", linkedin_url=…)`: current title and company (with its domain), past positions, tenure — no email nor phone
- "tell me about acme.com" → `pipecorn(op="company", domain="acme.com")`: industry, headcount and its growth, department headcount, revenue range, headquarters, funding, web traffic
- "what does acme.com run?" → `pipecorn(op="stack", domain="acme.com")`: technologies with a confidence
- "is acme.com hiring salespeople in France?" → `pipecorn(op="jobs", domain="acme.com", job_title_keywords=["sales"], job_country_codes=["FR"], limit=20)`: open postings with their hiring team — billed per job, so bound `limit`
- for a contact, prefer the waterfalls: Pipecorn is the first step of `waterfall_phone` and `waterfall_email` (submitted and read for you, up to 20 s, then your other providers), and a step of `waterfall_person`, `waterfall_company`, `waterfall_tech`. Their table modes (`op="update_rows"`) leave the asynchronous phone and email lookups out

## note — what can mislead

- you pay **per result found**, in Pipecorn credits: 3 a work email, 30 a phone (only a **mobile**), 1 a profile or a company, 1 per job posting; a technology stack is 3 per request, found or not. A miss costs nothing. A contact lookup is billed when it finishes; `pipecorn_credits` says what a lookup cost, and reading a result again costs nothing more
- in `waterfall_email` and `waterfall_phone`, a Pipecorn lookup still running after its 20 s is read once more at the end: an email or a mobile it finds is **billed even if another provider found first**
- a **third party's personal email** is never looked up: `find=["personal_email"]` is refused, whatever the key; the profile, company and job lookups come without any email or phone
- the profile, company, technology and job lookups need **your own** Pipecorn key
- pass the same `find` to `op="result"` as to the submission: only what was asked is counted
- a lookup can end `failed` when the credits ran out while it ran: nothing found, nothing billed
- a miss is **not an error**: empty fields, `phone_status: "not_found"`
- a number comes back as Pipecorn writes it, with or without its `+`: no country is guessed for it
- `phone_type="mobile"` keeps looking until a provider has a mobile and drops landlines; without it, a landline can come back, unbilled
- a number your Pipecorn workspace suppressed comes back as the text `"Contacts not shared"`, not a number
- a lookup is **never retried** automatically
