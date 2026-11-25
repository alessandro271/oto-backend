## prerequisite — an API key, and where the API lives

the Custom API connector connects a tool that has **no card of its own** in the catalogue (a payroll, an ERP, an internal service) as long as it offers an API. Gather, from the tool's API documentation:
- the **base URL** of its API (e.g. `https://api.acme.test/v1`) — the paths of its documentation are relative to it
- how it authenticates: a bearer token, a key in a header (and the header's name) or in a query parameter, a basic login, OAuth2 client credentials, a session (an account's login and password, for a service with no API key), or nothing
- the key itself, created in the tool's settings — **paste it only in this form**: never in a chat, never in an email. If a key was pasted in a conversation, revoke it at the provider and create a new one
- optionally, the path of its OpenAPI description in JSON (e.g. `/openapi.json`), to import its operations

## setup — connect it, or have it connected

- fill in the card yourself, or ask the agent: `api(op="setup_link", label="Acme Payroll", base_url=…, auth_mode="bearer")` returns a link to this form, pre-filled with the name (never an address or an auth mode), to send to whoever holds the key: they type the base URL and pick the auth mode themselves (a link may come from a manipulated agent, so the form never takes an address or a mode from it), then paste the key
- several APIs, or several accounts of the same one: each is a named account of the card, chosen on every call with `_account`
- an internal address (loopback, private network) is refused until the platform operator declares it as a named exception

## usage — configure it once, every member uses it

- the org admin configures the tool **once**: the card (base URL, auth, key), then its **definition** on this page or via the agent with `api_act(op="set_definition")` — a display name, a `description` of what the tool holds (read by every member's agent), a logo domain, constant non-secret `headers` (e.g. an API version), saved `variables` (e.g. your organization id, filled into any action argument of the same name) and its **actions**
- an action: `name` (snake_case), `description` (what the agent reads to choose it), `method` (GET, POST, PUT, PATCH, DELETE), `path` relative to the base URL with `{param}` placeholders, `input` (a JSON schema of the arguments), and optionally `in` to send an argument to `path`, `query` or `body` — by default a placeholder goes in the path, the rest in the query for GET/DELETE and in the body otherwise
- import actions instead of typing them: `api_act(op="import", source="doc_path"|"url"|"inline")` converts an OpenAPI/Swagger JSON document into candidates without saving them
- every member's agent then finds it by name (`oto_connector(name="Acme Payroll")`), lists the org's custom connectors with `api(op="list")`, and calls an action with `api_act(op="call", operation="list_collaborators", args={"page": 1})` — `_account` is only needed when two custom connectors share an action name; `dry_run=true` shows the request without sending it
- for anything no action describes: `api_act(op="request", method="PATCH", path="/…", body={…})`, or `api_get`/`api_post`; `api_doc()` reads the configured documentation route
- two tools: `api` only reads (the custom connectors, an action's input, the setup link); `api_act` calls the API, imports and saves — the one your MCP client asks you to confirm
- reuse a definition in another org: export it from this page (never a secret), connect the tool there with its own key, then import the definition

## note — what the connector does and does not do

- oto calls the API directly with the key you stored: what a POST, PUT, PATCH or DELETE does is decided by the target API and the rights of the key — give it the narrowest rights the tool allows
- redirects are never followed (the key would leave with them); header and cookie parameters, form bodies and YAML documents are not imported — they are listed as skipped, with the reason
- session mode: oto signs in with the account's login and password as the card's `session` steps describe (a JSON login, then optionally the URL the login answer names and its redirects), keeps the session cookie encrypted in the vault, and signs in again once when the service asks; it only reads (GET), and every sign-in step stays on the base URL's host, the hosts the card lists or its declared domain. The login body is JSON only. A refused login marks the card red: set the login and password again
- per-member sign-in (`oauth2_user`): the sign-in server and the API may live on different domains (an identity provider in front of an internal API is the usual case). Its authorization and token endpoints must share one origin, and a declared `resource` must designate the API itself; before leaving for the sign-in page the member is shown both hosts — where they sign in and where their access is used. A sign-in server that ignores `resource` issues a token valid for any API that accepts it: trust in an app's configuration therefore rests on the org admin who set it up, and on the member's attention to the hosts shown at sign-in. A client that starts the sign-in (`POST …/connect`) MUST show `sign_in_host` and `api_host` to the member before any redirection to `auth_url`, and redirect only after the member confirms
