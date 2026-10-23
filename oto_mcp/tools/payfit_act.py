"""PayFit — EVERY write, in one tool: `payfit_act` (wired on 21/10/2026).

Refused since 24/09/2026, the writes were wired on Alexis's decision of 21/10/2026, in
their OWN tool rather than as ops of the readers: an MCP client authorizes TOOLS, not
ops, and "always allow" on `payfit_absence` must never record an absence in payroll
(`lecture.ENVOI`, convention `<ns>_act` of 17/10/2026). The readers' old write ops
answer `op_moved` (`payfit_garde.moved`).

What the Partner API allows, nothing more — no endpoint updates a collaborator or a
contract, none takes variable pay items (overtime, bonuses):

- `create_collaborator` → `{collaboratorId}`, without a contract yet;
- `create_contract` → puts an existing collaborator on payroll (French companies);
- `create_absence` → `{id}`, **already approved**, straight into payroll;
- `cancel_absence`;
- `set_affiliation` → **replaces** one employment contract's mutuelle or prévoyance
  affiliation;
- `request_regularization` → PayFit recomputes mutuelle contributions already paid.

⚠️ **Three gates, none of them ours to bypass**: the key's PayFit scopes (a key
created with read scopes answers 403, named by `payfit_garde.upstream_message`), the
org's or team's access level (`providers/payfit.ACCESS_LEVELS`: `read` serves none of
these ops, `full` — the default — all of them), and the MCP client's confirmation
(`ENVOI`). The NIR and the absence type passed here are masked in the call log
(`journal_secrets.SECRET_TOOL_ARGS`).

Never exercised with a real write: PayFit's side effects (a notification to the
employee, the daily-allowance flow of a sick leave) are to be checked on a first case.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .lecture import ENVOI
from .payfit_garde import _bad, _client, need, refuse_ignored, refuse_unknown_op, run

_OPS = ("create_collaborator", "create_contract", "create_absence", "cancel_absence",
        "set_affiliation", "request_regularization")


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ENVOI)
    def payfit_act(
        op: Literal["create_collaborator", "create_contract", "create_absence",
                    "cancel_absence", "set_affiliation", "request_regularization"],
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        personal_email: Optional[str] = None,
        other_name: Optional[str] = None,
        social_security_number: Optional[str] = None,
        personal_address: Optional[dict] = None,
        birth_information: Optional[dict] = None,
        personal_phone_number: Optional[str] = None,
        number_of_children: Optional[int] = None,
        gender: Optional[str] = None,
        invite_collaborator: Optional[bool] = None,
        collaborator_id: Optional[str] = None,
        job_title: Optional[str] = None,
        start_date: Optional[str] = None,
        contract_id: Optional[str] = None,
        absence_type: Optional[str] = None,
        begin_date: Optional[str] = None,
        end_date: Optional[str] = None,
        start_moment: Optional[str] = None,
        end_moment: Optional[str] = None,
        absence_id: Optional[str] = None,
        comment: Optional[str] = None,
        kind: Optional[Literal["health", "provident"]] = None,
        insurance_contract_ids: Optional[list] = None,
        employee_is_exempted: Optional[bool] = None,
        effective_date: Optional[str] = None,
    ) -> dict:
        """WRITES to PayFit — hiring, absences, mutuelle and prévoyance. Each call
        changes the company's payroll: check the identifiers and dates with the
        person before calling. The reads are the other `payfit_*` tools.

        `op`:
        - **"create_collaborator"** (`first_name`, `last_name`, `personal_email`
          required) → `{collaboratorId}`. They have NO contract yet:
          `create_contract` is the next step. ⚠️ `invite_collaborator=True` sends
          them PayFit's invitation email. Scope `collaborators:write`.
        - **"create_contract"** (`collaborator_id`, `job_title`, `start_date`, French
          companies) → puts them on payroll. Nature, hours and pay are completed in
          PayFit: the API takes nothing more. Scope `collaborators:contracts:write`.
        - **"create_absence"** (`contract_id`, `absence_type`, `begin_date`,
          `end_date`) → `{id}`. ⚠️ Created **ALREADY APPROVED**, straight into
          payroll: there is no request or approval step. `absence_type` is PayFit's
          creation list: `fr_conges_payes`, `fr_rtt`, `fr_repos`, `fr_sans_solde`,
          `fr_teletravail`, `fr_maladie_ordinaire` (sick leave), the family events
          (`fr_mariage_salarie`, `fr_deces_conjoint`…). **No work accident, no
          maternity or paternity leave**: those are entered in PayFit itself. An
          unknown type comes back as PayFit's 400. Scope `time:write`.
        - **"cancel_absence"** (`absence_id` from `payfit_absence()`, optional
          `comment` kept by PayFit). Scope `time:write`.
        - **"set_affiliation"** (`contract_id`, `insurance_contract_ids` from
          `payfit_insurance(kind=…)`, `kind` health | provident): ⚠️ **REPLACES** the
          employment contract's affiliation — a contract left out of the list is
          unaffiliated; read the current one in `payfit_contract(op="get",
          fr=True)` first. `employee_is_exempted`: health only. Scope
          `health-insurance:write`.
        - **"request_regularization"** (`contract_id`, `insurance_contract_ids`,
          `effective_date`; mutuelle only): PayFit recomputes contributions already
          through payroll, and it flows into a payslip. Scope
          `health-insurance:write`.

        Nothing here updates an existing collaborator or contract, nor enters a
        variable pay item (overtime, bonus): the PayFit API has no such endpoint.

        Args:
            op: the write — required, no default.
            first_name / last_name / personal_email: create_collaborator, required.
            other_name / social_security_number (NIR, 15 characters for FR) /
                personal_address (`{streetNumber, addressFirstLine,
                addressSecondLine, city, postCode, country}`, ISO-2 country) /
                birth_information (`{birthDate, birthPlace, birthCountry}`) /
                personal_phone_number / number_of_children (0..20) / gender
                (MALE | FEMALE) / invite_collaborator: create_collaborator.
            collaborator_id / job_title / start_date (YYYY-MM-DD): create_contract,
                required.
            contract_id: create_absence, set_affiliation, request_regularization —
                required.
            absence_type / begin_date / end_date (YYYY-MM-DD, first and last day):
                create_absence, required.
            start_moment / end_moment: create_absence — `beginning-of-day` |
                `middle-of-day` | `end-of-day`; default a full first and last day.
            absence_id: cancel_absence, required. comment: cancel_absence.
            kind: set_affiliation — health (default) | provident.
            insurance_contract_ids: set_affiliation, request_regularization —
                required.
            employee_is_exempted: set_affiliation, kind=health.
            effective_date: request_regularization — YYYY-MM-DD, required.
        """
        collaborateur = dict(
            first_name=first_name, last_name=last_name, personal_email=personal_email,
            other_name=other_name, social_security_number=social_security_number,
            personal_address=personal_address, birth_information=birth_information,
            personal_phone_number=personal_phone_number,
            number_of_children=number_of_children, gender=gender,
            invite_collaborator=invite_collaborator)
        contrat = dict(collaborator_id=collaborator_id, job_title=job_title,
                       start_date=start_date)
        absence = dict(absence_type=absence_type, begin_date=begin_date,
                       end_date=end_date, start_moment=start_moment,
                       end_moment=end_moment)
        annulation = dict(absence_id=absence_id, comment=comment)
        couverture = dict(kind=kind, insurance_contract_ids=insurance_contract_ids,
                          employee_is_exempted=employee_is_exempted,
                          effective_date=effective_date)

        if op == "create_collaborator":
            need(op, first_name=first_name, last_name=last_name,
                 personal_email=personal_email)
            refuse_ignored(op, contract_id=contract_id, **contrat, **absence,
                           **annulation, **couverture)
            c = _client()
            return {"created": run(lambda: c.create_collaborator(**collaborateur))}
        if op == "create_contract":
            need(op, **contrat)
            refuse_ignored(op, contract_id=contract_id, **collaborateur, **absence,
                           **annulation, **couverture)
            c = _client()
            return {"created": run(lambda: c.create_contract(
                collaborator_id, job_title=job_title, start_date=start_date))}
        if op == "create_absence":
            need(op, contract_id=contract_id, absence_type=absence_type,
                 begin_date=begin_date, end_date=end_date)
            refuse_ignored(op, **collaborateur, **contrat, **annulation, **couverture)
            # Omitted moments are not passed: the client keeps its full-day defaults.
            moments = {k: v for k, v in (("start_moment", start_moment),
                                         ("end_moment", end_moment)) if v is not None}
            c = _client()
            return {"created": run(lambda: c.create_absence(
                contract_id=contract_id, absence_type=absence_type,
                start_date=begin_date, end_date=end_date, **moments))}
        if op == "cancel_absence":
            need(op, absence_id=absence_id)
            refuse_ignored(op, contract_id=contract_id, **collaborateur, **contrat,
                           **absence, **couverture)
            c = _client()
            run(lambda: c.cancel_absence(absence_id, comment=comment))
            return {"cancelled": absence_id}
        if op == "set_affiliation":
            need(op, contract_id=contract_id,
                 insurance_contract_ids=insurance_contract_ids)
            refuse_ignored(op, effective_date=effective_date, **collaborateur,
                           **contrat, **absence, **annulation)
            famille = kind or "health"
            if famille == "provident":
                refuse_ignored(op, employee_is_exempted=employee_is_exempted)
            c = _client()
            if famille == "health":
                run(lambda: c.set_health_insurance(
                    contract_id, health_insurance_contract_ids=insurance_contract_ids,
                    employee_is_exempted=employee_is_exempted))
            else:
                run(lambda: c.set_provident_fund(
                    contract_id, provident_fund_contract_ids=insurance_contract_ids))
            return {"affiliated": {"contract_id": contract_id, "kind": famille,
                                   "insurance_contract_ids": insurance_contract_ids}}
        if op == "request_regularization":
            if kind == "provident":
                raise _bad("op='request_regularization' exists for the mutuelle only: "
                           "PayFit has no prévoyance regularization.")
            need(op, contract_id=contract_id,
                 insurance_contract_ids=insurance_contract_ids,
                 effective_date=effective_date)
            refuse_ignored(op, employee_is_exempted=employee_is_exempted,
                           **collaborateur, **contrat, **absence, **annulation)
            c = _client()
            return {"requested": run(lambda: c.request_health_insurance_regularization(
                contract_id, health_insurance_contract_ids=insurance_contract_ids,
                effective_date=effective_date))}
        raise refuse_unknown_op(op, *_OPS)
