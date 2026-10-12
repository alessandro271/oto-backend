## prerequisite — a DataForSEO API login, or the shared key

DataForSEO is pay-as-you-go: no plan, no seat, a prepaid USD balance drawn per call (minimum top-up $50, the balance does not expire).
- your own key: in the [DataForSEO dashboard](https://app.dataforseo.com/api-access), copy the **API login** and the **API password** — not your sign-in password — into oto
- the platform key, when the instance has one, serves the orgs it is granted to; the super-admin posts it as ONE value, `login:password`
- a new DataForSEO account must be verified in its dashboard before the data endpoints answer (until then: HTTP 403, code 40104; the balance still reads)
- `dataforseo_account(op="balance")` reads the balance of YOUR key; it is refused on the shared key, whose balance is the platform's

## usage — keywords, domains, backlinks, AI visibility, audits, technologies

- "how many searches for X, how hard is it?" → `dataforseo_keywords(op="overview", keywords=[…], location=2250, language="fr")`
- "find keyword ideas around X" → `op="ideas"` (same topic), `op="suggestions"` (contains X), `op="related"` (Google's related searches); "is X growing?" → `op="trends"`
- "what does this site rank for, how much traffic, and since when?" → `dataforseo_domain(op="overview" | "keywords" | "pages" | "history", target="example.com")`
- "who are its competitors, what do they rank for that we don't?" → `op="competitors"`, then `op="gap", target2=…, shared_only=false`; "who wins on THESE keywords?" → `op="serp_competitors", keywords=[…]`
- "traffic of these 200 domains" → `dataforseo_domain(op="traffic", targets=[…])`, one call
- "who links to this site? who links to my competitors but not to me?" → `dataforseo_backlinks(op="summary")`, `op="referring_domains"`, then `op="gap", targets=[competitors], exclude_targets=[my site]`; "what did we gain or lose?" → `op="new_lost", date_from=…`
- "do ChatGPT and Google's AI answers cite us?" → `dataforseo_ai_visibility(op="metrics", domain=…)`, then `op="mentions"` for the answers themselves; "ask Perplexity what it recommends" → `op="ask", engine="perplexity", model=…` (`op="models"` lists them)
- "what does the web say about our brand?" → `dataforseo_web_mentions(op="summary", keyword=…)`
- "audit this page / this site" → `dataforseo_site_audit(op="page" | "lighthouse", url=…)`; a whole site: `op="crawl_start"`, `op="crawl_status"` until `finished`, then `op="crawl_results", report=…`
- "what is this site built with? which French sites run Shopify?" → `dataforseo_technologies(op="domain", target=…)`, `op="domains_using", technologies=["Shopify"], filters=[["country_iso_code", "=", "FR"]]`

`location` is a DataForSEO code (2250 France, 2840 United States, 2826 United Kingdom) or an English name; `dataforseo_account(op="locations", query="…")` lists them. Some ops require a location and a language: the tool says which.

## note — cost and default view

every answer carries `cost_usd`, the real amount DataForSEO charged; a refused call costs nothing. List ops are billed per returned row, so they default to `limit=20` (max 1000).
- most calls cost about $0.01–0.02; AI-mention ops about $0.10, domain `history` about $0.13; a site crawl about $0.00015 per page, paid at start, and reading its results is free
- answers are trimmed by default (nulls, per-month series, SERP snapshots and breakdowns removed); `full=true` returns DataForSEO's result as is
- `filters` and `order_by` use DataForSEO's syntax, e.g. `["keyword_info.search_volume", ">", 100]` and `["keyword_info.search_volume,desc"]`; other documented fields go in `options`
- figures are DataForSEO's estimates (its own index and clickstream), not the site's analytics; for the numbers a customer already trusts in Ahrefs or Ubersuggest, use those connectors with their own account
- the raw Google results page for one query (where a site ranks, today) is `serper_search`
