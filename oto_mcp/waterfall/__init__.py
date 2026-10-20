"""Waterfall — native enrichment cascades: email, phone, company, person, people, tech.

SIX connectors, one tool each (`waterfall_email`, `waterfall_phone`, `waterfall_company`,
`waterfall_person`, `waterfall_people`, `waterfall_tech`), mounted by `tools/waterfall.py`,
plus a table-writing `<ns>_act` mounted by `tools/waterfall_lignes.py`
(`Connector.modules=("waterfall", "waterfall_lignes")`, each loaded once). This package is
the domain both serve:

- `etape` — what a step is (`Step`, `Collect`); `entrees` — a call's inputs, normalized;
  `valeurs` — reading a provider's answer;
- `ordre` — which steps, in which order, on which key; `appel` — one step's call and
  its verdict; `moteur` — the cascade;
- `qualification` — the optional Jev pass; `vivier` — the people pool (pages, reveals);
- one step table per cascade (`email`, `telephone`, `societe`, `personne`, `gens`,
  `tech`, their shared request builders in `fournisseurs`); `plan` — the plan a screen
  reads.

Each tool tries a list of existing connector tools, one after the other, and stops at the
FIRST that finds the value asked for. Nothing here talks to a provider: every step is an
ordinary call of the provider's own tool through `meta.executer_cible`, the seam
`oto_call` and the recipes use — so each step keeps ITS guards (connector activation for
the org/team of the call, credential cascade, access level, hosted-job tool list), ITS
row in `tool_calls` (and therefore its `quantity`/`key_mode`: the billing stays on the
provider's row, never on the waterfall's), and ITS namespace's redaction policy.

**Which steps, in which order** — three layers, the most specific wins:
1. the call's `providers=[…]` (validated: an unknown key is the caller's fault);
2. the `steps` setting of the waterfall's card — an org default set by an admin, a
   member's override, resolved by the same cascade as a key (`providers/waterfall_*.py`,
   `secret=False`: a setting, never a secret). An unknown key there is IGNORED and named
   on the receipt (`ignored_steps`): the person calling did not write it;
3. the default table (`email.EMAIL_STEPS`…): provider cost and yield, the synchronous
   steps before the asynchronous ones.
On the DEFAULT order only, **the providers the caller holds their OWN key for run first**
(user, team, org or tenant key — `access.credential_mode_for`, read once per call, before
spending anything) together with the FREE steps (French registry, the caller's own
LinkedIn session), the shared keys next, each group in the table's order. An order
someone WROTE (the call's, the setting's) is kept as written. Either way, a provider with
no key at all is skipped without being called.

**What a step's failure does**: nothing but move on — a connector not activated, a key
refused, an empty account, a rate limit, a provider error: each is recorded on the receipt
(`attempts`) with its code and message, and the next provider is tried. Only the caller's
OWN input faults raise — and a qualification asked for that Jev cannot run, refused
before anything is spent.

**Asynchronous providers** (lemlist, Dropcontact, FullEnrich, Apollo's phone reveal): the
step submits, then polls the provider's own collect tool (free) within a bounded wait.
Not done in time → the step is `pending`, with the exact collect call to make later, and
the cascade STOPS there: no later step is paid while a paid answer is still coming.

**One contact per call.** A list goes through the table mode (`<ns>_act`) or a recipe:
the tools are declared read-only (`lecture.LECTURE`) — they change nothing at any third
party, even though their steps spend credits.
"""
