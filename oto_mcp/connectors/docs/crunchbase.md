## prerequisite — connect your crunchbase session

Crunchbase has no public API key: oto calls it from a browser logged in to your [Crunchbase](https://www.crunchbase.com) account. connect it once from the oto dashboard (**Connect** button): a hosted browser window opens, log in there as usual (SSO, captcha, second factor included). the session is then kept in the vault.
- without a connected session, the `crunchbase_*` tools return a message pointing to `crunchbase_connect_start`
- an expired session marks the connector "to reconnect": log in again the same way

## usage — crunchbase companies, funding and people

fetch company data, their funding rounds and people profiles.
- "Crunchbase record for `anthropic` (headcount, location, founders)"
- "list this company's funding rounds (date, type, amount, investors)"
- "search companies on `vector database`"
- "Crunchbase profile of this person"
