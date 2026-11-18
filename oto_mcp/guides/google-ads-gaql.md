---
title: Google Ads GAQL (google_ads_search)
description: accounts, managers, GAQL grammar, dates, money in micros, ready queries and errors — read before a non-trivial Google Ads query
---

# Google Ads GAQL

The `google_ads_search` docstring stays short; this guide holds what makes a query fail or mislead.

## The safe sequence

1. `google_ads_customers` → the ids your Google user opens directly.
2. Name, currency, time zone of an account: `SELECT customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager FROM customer`.
3. A **manager** account (`customer.manager = true`) holds no campaigns: list its clients with `SELECT customer_client.id, customer_client.descriptive_name, customer_client.level, customer_client.manager, customer_client.status FROM customer_client WHERE customer_client.level <= 1`, then query each client with `customer_id=<client>` and `login_customer_id=<manager>`.
4. Unsure of a field? `google_ads_fields(resource="…")` — never guess a name.

## Grammar

- `SELECT <fields> FROM <resource> [WHERE <cond> AND …] [ORDER BY <field> [ASC|DESC]] [LIMIT n]`.
- Full field names only (`campaign.name`), no `*`, no joins, no `OR` (only `AND`), no arithmetic.
- Strings in single quotes: `campaign.status = 'ENABLED'`; enums are UPPER_CASE names; `IN ('ENABLED', 'PAUSED')`, `LIKE '%brand%'`, `CONTAINS ANY (…)` on repeated fields.
- Fields of a related resource can be selected when `google_ads_fields` lists them under `related` (e.g. `campaign.name` from `ad_group`).

## Dates

- `segments.date DURING LAST_7_DAYS` — also `TODAY`, `YESTERDAY`, `LAST_14_DAYS`, `LAST_30_DAYS`, `THIS_WEEK_MON_TODAY`, `LAST_WEEK_MON_SUN`, `THIS_MONTH`, `LAST_MONTH`, `LAST_BUSINESS_WEEK`.
- Or `segments.date BETWEEN '2026-01-01' AND '2026-01-31'` (both inclusive, `YYYY-MM-DD`).
- Metrics without any date filter cover the whole account history — always filter on a date for performance questions.
- Selecting `segments.date` (or `segments.week`, `segments.month`) splits rows per period; without it, one total row per resource.
- Days are in the **account time zone**; the last ~3 days of conversions still move.

## Money and numbers

- Cost and bids are in **micros** of the account currency: `metrics.cost_micros / 1 000 000`. Say the currency (`customer.currency_code`).
- The response names columns in camelCase (`metrics.costMicros`) and 64-bit numbers come as strings.
- `metrics.conversions` is fractional (data-driven attribution); `metrics.conversions_value` is in currency units, not micros.
- `metrics.ctr`, `metrics.average_cpc` are computed per row: never sum or average them across rows — recompute from clicks, impressions and cost.

## Ready queries

| Question | Query |
|---|---|
| campaigns, 30 days | `SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, metrics.cost_micros, metrics.impressions, metrics.clicks, metrics.conversions, metrics.conversions_value FROM campaign WHERE segments.date DURING LAST_30_DAYS ORDER BY metrics.cost_micros DESC` |
| daily spend | `SELECT segments.date, metrics.cost_micros, metrics.clicks, metrics.conversions FROM customer WHERE segments.date DURING THIS_MONTH ORDER BY segments.date` |
| ad groups | `SELECT campaign.name, ad_group.name, ad_group.status, metrics.cost_micros, metrics.clicks, metrics.conversions FROM ad_group WHERE segments.date DURING LAST_30_DAYS` |
| ads | `SELECT ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.status, metrics.impressions, metrics.clicks FROM ad_group_ad WHERE segments.date DURING LAST_30_DAYS` |
| keywords | `SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions FROM keyword_view WHERE segments.date DURING LAST_30_DAYS ORDER BY metrics.cost_micros DESC LIMIT 100` |
| search terms without conversion | `SELECT search_term_view.search_term, metrics.clicks, metrics.cost_micros, metrics.conversions FROM search_term_view WHERE segments.date DURING LAST_7_DAYS AND metrics.conversions = 0 AND metrics.clicks > 0 ORDER BY metrics.cost_micros DESC LIMIT 100` |
| by device | add `segments.device` to the campaign query |
| budgets | `SELECT campaign.name, campaign_budget.amount_micros, campaign_budget.delivery_method FROM campaign WHERE campaign.status = 'ENABLED'` |

## Auditing an account (read-only)

An audit is a dozen aggregated queries, not a crawl: each query below is ONE Google request. Run them once per account, `LIMIT` the long lists, and reuse the answers rather than re-asking.

| Audit point | Query |
|---|---|
| account settings | `SELECT customer.descriptive_name, customer.currency_code, customer.time_zone, customer.auto_tagging_enabled, customer.optimization_score, customer.test_account FROM customer` |
| conversion tracking | `SELECT conversion_action.name, conversion_action.status, conversion_action.type, conversion_action.category, conversion_action.primary_for_goal, conversion_action.counting_type, metrics.all_conversions FROM conversion_action WHERE segments.date DURING LAST_30_DAYS` |
| campaign setup | `SELECT campaign.name, campaign.status, campaign.advertising_channel_type, campaign.bidding_strategy_type, campaign.network_settings.target_search_network, campaign.network_settings.target_content_network, campaign_budget.amount_micros FROM campaign WHERE campaign.status = 'ENABLED'` |
| lost impression share | `SELECT campaign.name, metrics.search_impression_share, metrics.search_budget_lost_impression_share, metrics.search_rank_lost_impression_share, metrics.cost_micros FROM campaign WHERE segments.date DURING LAST_30_DAYS AND campaign.advertising_channel_type = 'SEARCH'` |
| quality score | `SELECT ad_group.name, ad_group_criterion.keyword.text, ad_group_criterion.quality_info.quality_score, ad_group_criterion.quality_info.creative_quality_score, ad_group_criterion.quality_info.post_click_quality_score, ad_group_criterion.quality_info.search_predicted_ctr, metrics.cost_micros FROM keyword_view WHERE segments.date DURING LAST_30_DAYS ORDER BY metrics.cost_micros DESC LIMIT 200` |
| negative keywords | `SELECT campaign.name, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type FROM campaign_criterion WHERE campaign_criterion.negative = TRUE AND campaign_criterion.type = 'KEYWORD' LIMIT 500` |
| ads: strength and approval | `SELECT ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.ad_strength, ad_group_ad.policy_summary.approval_status, ad_group_ad.status FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED'` |
| landing pages | `SELECT landing_page_view.unexpanded_final_url, metrics.clicks, metrics.cost_micros, metrics.conversions FROM landing_page_view WHERE segments.date DURING LAST_30_DAYS ORDER BY metrics.cost_micros DESC LIMIT 50` |
| recent changes (≤ 30 days) | `SELECT change_event.change_date_time, change_event.change_resource_type, change_event.resource_change_operation, change_event.user_email, change_event.client_type FROM change_event WHERE change_event.change_date_time >= '<YYYY-MM-DD>' AND change_event.change_date_time <= '<YYYY-MM-DD>' ORDER BY change_event.change_date_time DESC LIMIT 100` |

Plus the "search terms without conversion" and "keywords" queries above. Nothing here can modify the account: the tools only read.

## Budget

Every Google Ads call of the platform draws on ONE Google quota (the platform's Google Cloud project), so oto caps the Google requests: per org per minute and for the whole platform per day. A refusal says which, when to retry, and that nothing was sent. Stay well under it: aggregated queries with `ORDER BY … LIMIT`, `google_ads_fields` once per resource (its answer is kept), no paging through raw rows — the next slices of a page already read cost nothing for a few minutes, a new Google page costs one request.

## Paging

Each call returns at most `max_rows` rows (200 by default). `page_token` continues the SAME query (send it unchanged); it expires after about two hours. Prefer `ORDER BY … LIMIT n` and aggregation (fewer segments) over paging through raw rows.

## Errors

| Message says | Meaning | Do |
|---|---|---|
| only approved for TEST accounts | the platform's Google Cloud project has Test access | tell the platform's administrator; nothing to do on the account |
| through a MANAGER account | access goes through an MCC | pass `login_customer_id=<manager id>` |
| not a user of any Google Ads account | the connected Google account has no Google Ads access | connect the right Google account, or get it invited |
| refused the request: … | GAQL error (unknown field, incompatible segment, bad date) | check `google_ads_fields`, fix the query |
| quota or rate limit | Google's operations quota | wait, then send fewer, larger queries |
| read budget reached | oto's cap on the shared quota (see Budget) | retry when it says; fewer, larger queries |
