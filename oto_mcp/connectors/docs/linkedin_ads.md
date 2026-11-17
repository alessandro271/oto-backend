## prerequisite — a role on the ad account in Campaign Manager

oto reads the LinkedIn ad accounts **you** have a role on: you sign in with LinkedIn and authorize oto.

- the token only asks `r_ads` and `r_ads_reporting`: **read-only by construction** — nothing can be created, edited, paused or spent, whatever the agent does
- oto sees an ad account only if **your** LinkedIn member has a role on it in Campaign Manager. **VIEWER is enough** (and is the right one for an audit)
- to read someone else's account (a client, a prospect): its owner opens Campaign Manager → the account → **Manage access** → adds your LinkedIn member as **Viewer**. Then `linkedin_ads(op="account_roles")` lists it
- the sign-in lasts until it expires or you remove it from your LinkedIn settings (permitted services); when it lapses, the card says « reconnect »

## setup — the LinkedIn application and the callback URL

operator only. Create an app on the **LinkedIn Developer Portal**, associated with the company's LinkedIn Page, and request the **Advertising API** product (Products tab → access form; LinkedIn reviews it). Once approved, the Auth tab lists `r_ads` and `r_ads_reporting`. Declare this redirect URL there, byte for byte:

{{callback:/api/linkedin_ads/oauth/callback}}

then set the two values at platform scope, once per base:

- `oto_admin_connector_setting(op="set", connector="linkedin_ads", key="client_id", value="…")`
- `oto_admin_connector_setting(op="set", connector="linkedin_ads", key="client_secret", value="…")`

until they are set, the card stays visible and « connect » refuses **naming the missing key**. `client_secret` is a real secret: never returned by an API, never logged. Each instance has its own callback URL — declare each one.

- with programmatic refresh tokens (granted to approved Marketing API apps), a sign-in lasts a year; without, it lasts the 60 days of one access token
- the app's call limits are **shared by every org**: they are shown in the app's Analytics tab, and LinkedIn emails the app admins at 75 %

## usage — accounts, campaigns, ads, performance

read-only. One tool, `linkedin_ads`, the read in `op`.

- `op="account_roles"` first: the ad accounts you have a role on, with the role; the digits of `urn:li:sponsoredAccount:…` are the `account_id` of every other read
- `op="list_campaign_groups"` / `list_campaigns` / `list_creatives` — the account's tree. A campaign comes with its objective, cost type, bid (`unitCost`), `dailyBudget` / `totalBudget`, schedule, serving statuses and a `targeting_summary`; `full=True` gives the raw `targetingCriteria`. A creative comes with `isServing`, `servingHoldReasons` and its review status
- `op="analytics"` — performance: `pivots=["CAMPAIGN"]`, `start="2026-01-01"`, `account_ids=[…]`. One pivot may be an audience one (`MEMBER_JOB_FUNCTION`, `MEMBER_SENIORITY`, `MEMBER_COMPANY_SIZE`, `MEMBER_INDUSTRY`, `MEMBER_COUNTRY_V2`, `MEMBER_COMPANY`), alone; up to 3 non-audience pivots together. `time_granularity` ALL, MONTHLY, YEARLY, or DAILY over at most 92 days
- lists come one page per call (`next_page_token` → `page_token`)

calls are paced **per person** — the app's quota is shared — and a refusal says how long to wait. One report with several ids and a coarse granularity beats a loop of small ones.

## note — what these numbers are

- amounts are in the **account currency** (`costInLocalCurrency`); `costInUsd` exists too
- an **empty** report means no activity **or** no access to that data: LinkedIn does not refuse, it answers nothing — check `account_roles`
- audience pivots are approximate (privacy), keep only the top 100 values per creative per day, drop values under 3 events, and lag 24-48 h; `approximateMemberReach` is not available with them, nor beyond 92 days
- daily performance data is kept 6 months, monthly data 2 years, account/campaign performance 10 years, audience data 2 years
- `pivotValues` are URNs (`urn:li:sponsoredCampaign:…`, `urn:li:organization:…`, `urn:li:geo:…`): match campaign and creative URNs with the list reads; names of geo, title or company URNs are not resolved here
