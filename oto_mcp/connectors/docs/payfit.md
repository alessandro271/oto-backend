## prerequisite — your payfit api key

log in to PayFit **as a company admin**, then **Integrations → API** ([app.payfit.com/integrations/hub/api](https://app.payfit.com/integrations/hub/api)) → "Create a key": give it an explicit label and tick the scopes you need (below), then copy it — it is not shown again afterwards. paste it into your oto connector keys under `payfit`.
- **the key decides what you will see and do**: oto serves everything the API exposes for reading, but a field that a missing scope does not return does not exist for anyone. for full HR and finance steering: `collaborators:read`, `collaborators:management:read`, `collaborators:contracts:read`, `collaborators:personal:read`, `collaborators:legal-identity:read`, `contracts:read`, `contracts:payslips:read`, `time:read`, `accounting:read`, `health-insurance:read`, `collaborators:meal-vouchers:read`
- **sensitive** scopes, to be ticked only if you need them: `collaborators:social-security:read` (NIR), `collaborators:bank-info:read` (IBAN), `payment-files:read` (payment file)
- **write scopes, only for what the agent must do in PayFit**: `time:write` (record or cancel an absence, sick leave included), `collaborators:write` (create a collaborator), `collaborators:contracts:write` (create their contract), `health-insurance:write` (mutuelle and prévoyance affiliation, regularization). a key without them stays read-only, whatever oto allows
- **reading only, on oto's side**: an org administrator (or a team lead, for their team) sets the `read` access level — `oto_connector_access_level(op="set", name="payfit", level="read")` (`scope="group"` for a team) — and `payfit_act` is no longer served, whatever the key carries
- the key opens **only your company**; oto finds its identifier on its own (by introspection), you have no identifier to enter
- whether API access is included or paid depending on the PayFit plan is not publicly documented; partner access (OAuth) is another route, by application
- BYO only: no shared oto key

## setup — a group of companies, one key per company

a PayFit key opens **only one company**, and the API has no group view: two companies = two independent keys. create a key in each PayFit company, then set each one as a company of the connector (section "multiple companies").
- ⚠️ **consolidation is done on your side, not at PayFit**: a total returned without looping over each `_account` would be the figure of a single company presented as the group's

## usage — from the directory to financial steering

start with `payfit_company()`: its `country` says whether the French variants apply (FR contracts, meal vouchers, worked time, health insurance), and `key_scopes` / `writable_ops` say what the key may do — `create_absence` is only there if the key carries `time:write`. read them before a write; a missing scope means a new key, created in PayFit.
- "who works here, who is their manager?" → `payfit_collaborator()` (next page: `cursor=<next_cursor>`)
- "what position, what contract type, what collective agreement, days-based package?" → `payfit_contract(fr=True)` (`natureContratDsn`: 01 CDI, 02 CDD…; `workingTimeModality`: `forfait_jours`…)
- "who is absent next week?" → `payfit_absence(begin_date="YYYY-MM-DD", end_date="YYYY-MM-DD")`, then link `contractId` to a collaborator's `contracts`
- "how much did January's payroll cost us, and on what?" → `payfit_payroll(op="accounting", date="202601")`: one row per entry, with account, label, debit, credit, employee and analytic codes. **this is the only structured numeric data in the API** — payroll cost, charges and benefits in kind are read there by account number (641x, 645x, 6417x)
- "is the month closed?" → `payfit_payroll(date="202601")` (op `status` by default) **before** using any figures
- "the gross pay of each employee, cadre / non-cadre" (a health or provident-fund tender, a payroll review) → `payfit_payroll(op="gross_by_contract", date="202609", date_to="202609")`: one row per ACTIVE contract with `college` (`cadre` | `non_cadre`, from the DSN conventional status: 03-04 cadre, 05-07 non-cadre) and `gross` = debit − credit on the 641x accounts, split in `by_account` — what payroll booked as remuneration, not the payslip's gross line. never rebuild it from `op="accounting"` (~30 lines per employee). an "assimilé cadre" (article 36) is not visible through the API. a group of companies = one call per `_account`
- "the journal for my accounting firm" → `payfit_payroll(op="accounting_export", date="202601")`; "the payment file" → `op="payment_file"` — **documents locked by default**, see the note on personal data
- "someone's payslips" → `payfit_payslip(collaborator_id=…)` for the list, then `op="download"` with the three identifiers of the row: the PDF text comes back readable, the original PDF as `raw_url` (**locked by default**, like any document)
- "how many overtime hours, and how many euros?" → `payfit_payslip(op="overtime", collaborator_id=…, date="202601")`: only the overtime / complementary / increased-rate hours lines of the payslip, read server side (the payslip does not go out). the numbers are returned in the line's order, with no role assigned: check the reading against a payslip before totalling, and never add an `allegement` line (contribution reduction) to the paid amount. for a company, loop over `payfit_collaborator()`
- "how many hours worked?" → `payfit_worked_time(date="202601")` · "meal vouchers" → `payfit_meal_voucher(date="202601")`
- "health insurance and provident cover" → `payfit_insurance()` for the company's contracts, `kind="provident"` for provident cover
- **the month is written `YYYYMM`** (`202601`), never `2026-01`: it is the only form PayFit accepts

## note — writing to payfit

every write goes through **one tool, `payfit_act`**, that your MCP client asks you to confirm — the reading tools never write (their old `create`/`cancel`/`affiliate`/`regularize` ops answer `op_moved`, naming the `payfit_act` op). it does **what the Partner API allows, nothing more**:
- `payfit_act(op="create_collaborator", first_name=…, last_name=…, personal_email=…)` → `{collaboratorId}`. the collaborator has no contract yet. ⚠️ `invite_collaborator=true` sends them PayFit's invitation email
- `payfit_act(op="create_contract", collaborator_id=…, job_title=…, start_date=…)`: puts them on payroll; nature, hours and pay are completed in PayFit (the API takes nothing more)
- `payfit_act(op="create_absence", contract_id=…, absence_type=…, begin_date=…, end_date=…)` → `{id}`. ⚠️ **created already approved, straight into payroll**: there is no request or approval step. sick leave = `fr_maladie_ordinaire`. **no work accident, no maternity or paternity leave** in the API's creation list: those are entered in PayFit. `op="cancel_absence"` (`absence_id`, optional `comment`) cancels one
- `payfit_act(op="set_affiliation", contract_id=…, insurance_contract_ids=[…])` (`kind="provident"` for prévoyance): ⚠️ **replaces** the affiliation — a contract left out is unaffiliated. `op="request_regularization"` (mutuelle only, `effective_date`) recomputes contributions already paid
- the NIR and the absence type passed to a write are masked in oto's call log
- never exercised with a real write: PayFit's side effects (a notification to the employee, the daily-allowance flow of a sick leave) are to be checked on a first real case

## note — what the payfit api does not have

these questions come up often and have **no endpoint** — oto will not fabricate them, and an invented answer would be wrong:
- **a payslip's lines** (gross, net, contribution by contribution): only the PDF and its metadata exist. the only amounts a program can read are the accounting entries — except overtime, which `op="overtime"` READS from the PDF. to get them as accounting data, isolate them on a dedicated sub-account in PayFit's accounting settings (the API cannot do it): the new account will appear as is in `op="accounting"`
- **year-to-date totals**, an aggregated "employer cost", charges as a resource: to be rebuilt from the entries, month by month
- **the DSN**: FR contracts carry fields *coded according to* the DSN standard (nature, status, IDCC, termination reason), but there is no DSN filing or retrieval
- **schedules and clock-ins**: only a monthly aggregate per contract exists (`payfit_worked_time`)
- **leave balances and counters** (paid leave accrued/taken, RTT remaining): nothing. do not deduce them from a list of absences
- **variable pay items** (overtime, bonuses, commissions, one-off amounts): no endpoint, neither to read the month's entries before closing nor to write them. they are entered or imported in PayFit itself; after closing, overtime reads from the payslip (`op="overtime"`) and every amount from the accounting entries
- **the history of amendments** and any change to an existing contract or collaborator: a contract is read as it is today, and nothing in the API updates one
- **expense reports**, benefits in kind as an object
- **tax documents**: those that exist (`payfit_document`) are **British**; on a French company the list is empty, and that is the right answer

## note — personal data: what is masked, and how to lift it

the connector no longer removes anything in a hard-coded way: it serves what the key allows, and protection is an **org policy**, editable.
- **masked by default** (server default): NIR (and NTT), IBAN/BIC, and the reason for an absence (`absence_type`) — sickness, work accident, maternity are health data. a `••••` is a masked value, not missing data
- `absence_category` always stays readable: `ordinary_leave` (paid leave, RTT, rest, unpaid, remote work, school) or `restricted` for everything else — enough to plan a workload without reading a reason
- **served unmasked**: pay in accounting entries, payslips, employer cost, charges, full contracts (termination, trial period, collective agreement, status), worked time, health insurance and provident cover, meal vouchers, e-mail, phone, address, date of birth, gender, nationality, seniority, manager
- **lifting a mask**: an org administrator does so field by field, by **naming** it: `oto_org_settings domain=field_filters op=set service=payfit rules=[] unmask=["iban"]` (accepted names: those of the server default, under their output name). the org policy is **added to** the default: without `unmask`, nothing comes out in clear, and `rules: []` alone is refused (`floor_lift_must_be_explicit`). the response lists the fields now in clear under `unmasked`. ⚠️ *clearing* the policy (`rules: null`) restores the server default as is
- **the `redaction` notice states the EFFECTIVE policy**: every response that may carry a sensitive field names those that the org's policy (or the server default) masks, and those it leaves IN CLEAR. an org that has lifted a mask receives the data in clear, and the notice says so — it never announces a mask that does not apply
- **documents are locked**: a field filter cannot see inside a file, yet the PDF payslip carries the NIR, the payment file each employee's IBAN, the accounting export names and amounts per person (and the British tax documents the insurance number). these four documents are served **only if the org has opened them**, by a separate consent: `oto_org_settings domain=field_filters op=set service=payfit rules=[] documents=true`. this consent **lifts no mask** from the JSON responses. otherwise the call is refused, saying so, and the refusal is not a bug. if the policy cannot be read, the document is refused too. `payfit_payslip(op="overtime")` is not locked: it only returns the overtime lines, never the payslip. their numbers (`numbers`, `rates`) are filtered by the policy like any data; the raw line (`line`), a verbatim excerpt of the payslip, follows the documents lock (`line_withheld` says why otherwise)
- **data** (accounting entries as JSON, payroll status, payslip list) is never locked: the policy filters it field by field
- derived from the public documentation and OpenAPI spec (read on 17/09/2026), never exercised with a real key
