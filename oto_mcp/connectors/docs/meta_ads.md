## prerequisite — a Meta business portfolio and an ad account

oto reads ad accounts once you authorize it on Facebook. **Which ones** depends on the login configuration the instance operator set (`config_id`, below):

- **user-token configuration** (the setup while the Meta app is on standard access): every ad account your Facebook account can see
- **system-user configuration** (once the app has advanced access): only the ad accounts you tick during the authorization. To add one later, reconnect and tick it
- either way, `meta_ads_accounts` lists exactly what your connection reaches
- you need a Facebook account with access to the ad accounts (directly or through a **business portfolio** in Meta Business Suite)
- ⚠️ **beta, testers only**: until our Meta app passes Meta's review, it runs with **standard access** — only people with a role on the app (testers) can authorize it, and Meta limits the number of calls heavily. Ask the operator to add you as a tester, then accept the invite at developers.facebook.com/requests. Big reports may hit the limit — wait a few minutes and retry
- with the standard-access setup (user token), the connection lasts about **60 days**: then the card goes "to reconnect". After the review, the system-user setup does not expire

## setup — the callback URL to declare at Meta

operator only. You need a **Meta app** of type *Business* with the **Marketing API** product and **Facebook Login for Business**. Create a login *configuration* with:

- token type: **user access token** while the app is on standard access (a system-user token gets NO permission there: every call answers "(#200) Missing Permissions"); **system-user access token** (never expires, assets: **ad accounts**) once the app has advanced access
- permissions: `ads_read`, `business_management`

and declare this callback URL, byte for byte:

{{callback:/api/meta_ads/oauth/callback}}

then set the three app values at platform scope, once per instance:

- `oto_admin_connector_setting(op="set", connector="meta_ads", key="app_id", value="…")`
- `oto_admin_connector_setting(op="set", connector="meta_ads", key="app_secret", value="…")`
- `oto_admin_connector_setting(op="set", connector="meta_ads", key="config_id", value="…")`

until they are set, the card stays visible and « connect » refuses **naming the missing key**. `app_secret` is a real secret: never returned by an API, never logged. Preprod and prod have different callback URLs — declare both.

## usage — accounts, campaigns, performance

read-only: nothing is created, edited, paused or spent.

- `meta_ads_accounts` first: it lists the ad accounts this connection reaches, with currency and timezone — amounts are in the **account currency**
- `meta_ads_objects` browses campaigns, ad sets and ads of an account (filter with `effective_status=["ACTIVE"]`), or reads one object by id
- `meta_ads_insights` returns performance: spend, impressions, reach, clicks, CPC, CTR, conversions (`actions`). Use `level` to get one row per campaign/ad set/ad, `time_increment=1` for daily rows, `breakdowns` for age, gender, country or placement
- for long ranges or ad-level reports over a whole account, use `mode="async"`: if the report is not done in ~25 s, call again with the returned `report_run_id`

## note — what these numbers are

- figures are Meta's, with Meta's attribution: by default 7-day click and 1-day view. Pass `action_attribution_windows` to change it
- `reach` is deduplicated: it cannot be summed across days or campaigns
- recent days can still move for up to 28 days (late conversions)
