"""PayFit — the GROSS PAY of each active contract, with its cadre / non-cadre
college, computed from the payroll accounting entries.

The PayFit API serves no payslip line: the only structured amounts are the
accounting entries of a month (`accounting-v2`), about thirty lines per employee.
A health or provident-fund tender asks for one line per employee — gross pay and
college — so the aggregation is done HERE, server side, and only the compact
result goes out.

**What "gross" is here.** The sum, per contract, of debit minus credit on the
**641x accounts** (wages and salaries: 6411 base salary, 6412 paid leave, 6413
bonuses, 6414 allowances, 6417 benefits in kind…), over the months asked. It is
what the payroll BOOKED as remuneration, not the "salaire brut" line of the
payslip: an allowance exempt from contributions booked in 641x is counted, a
remuneration booked elsewhere is not. `by_account` gives the split, so the reader
can leave out an account. Employer contributions (645x) are never counted.

**What "college" is here.** The DSN conventional status of the French contract
(`statutConventionnelDsn`, block S21.G00.40.002): 01 executive officer and 02
other cadres → `cadre`; 03 intermediate profession, 04 employee, 05 worker →
`non_cadre`; anything else → `null`, with the raw code kept. ⚠️ The API does not
serve the supplementary-pension category (S21.G00.40.003), so an "assimilé
cadre" (article 36) is not distinguishable from a non-cadre here.

Pure: no client, no network — `payfit_paie` fetches, this module adds up.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

MAX_MONTHS = 12
WAGE_PREFIX = "641"
CADRE = "cadre"
NON_CADRE = "non_cadre"
_CADRE_CODES = frozenset({"01", "02"})
_NON_CADRE_CODES = frozenset({"03", "04", "05"})
_MONTH = re.compile(r"\d{4}(0[1-9]|1[0-2])")


def months(date: str, date_to: Optional[str]) -> list:
    """The months from `date` to `date_to` included (`YYYYMM`), at most twelve."""
    for name, value in (("date", date), ("date_to", date_to)):
        if value is not None and not _MONTH.fullmatch(str(value)):
            raise ValueError(f"PayFit: `{name}` is written `YYYYMM` (January = 01), "
                             f"got \"{value}\".")
    y, m = int(date[:4]), int(date[4:])
    end = date_to or date
    if end < date:
        raise ValueError(f"PayFit: `date_to` ({end}) is before `date` ({date}).")
    out = []
    while True:
        cur = f"{y:04d}{m:02d}"
        out.append(cur)
        if cur == end:
            return out
        if len(out) == MAX_MONTHS:
            raise ValueError(f"PayFit: at most {MAX_MONTHS} months per call "
                             f"({date} → {end} is more).")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def college(contract: dict) -> Optional[str]:
    if contract.get("estCadreDirigeant") is True:
        return CADRE
    code = contract.get("statutConventionnelDsn")
    if code in _CADRE_CODES:
        return CADRE
    if code in _NON_CADRE_CODES:
        return NON_CADRE
    return None


def _amount(value: Any, month: str, account: str) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"PayFit: accounting entry of {month} on account {account} "
                         f"carries a non-numeric amount {value!r}.") from None


def gross_by_contract(entries_by_month: dict, contracts: Iterable[dict]) -> dict:
    """`entries_by_month`: `{YYYYMM: [accounting-v2 entries]}`; `contracts`: the
    FR contracts of the company. One row per ACTIVE contract; the gross of a
    contract that is not active (left, pending) is only counted in `others`."""
    totals: dict = {}
    paid_months: dict = {}
    for month, entries in entries_by_month.items():
        for e in entries or ():
            account = str(e.get("accountId") or "")
            if not account.startswith(WAGE_PREFIX):
                continue
            net = (_amount(e.get("debit"), month, account)
                   - _amount(e.get("credit"), month, account))
            cid = e.get("contractId") or None
            per = totals.setdefault(cid, {})
            per[account[:4]] = per.get(account[:4], 0.0) + net
            paid_months.setdefault(cid, set()).add(month)

    rows = []
    active = set()
    for c in contracts:
        if c.get("status") != "ACTIVE":
            continue
        cid = c.get("contractId")
        active.add(cid)
        per = totals.get(cid)
        n = len(paid_months.get(cid, ()))
        rows.append({
            "contractId": cid,
            "firstName": c.get("firstName"),
            "lastName": c.get("lastName"),
            "jobName": c.get("jobName"),
            "startDate": c.get("startDate"),
            "natureContratDsn": c.get("natureContratDsn"),
            "statutConventionnelDsn": c.get("statutConventionnelDsn"),
            "college": college(c),
            # No 641x entry over the period: a MISSING amount, never a zero.
            "gross": round(sum(per.values()), 2) if per else None,
            "months_paid": n,
            "by_account": {k: round(v, 2) for k, v in sorted(per.items())} if per else {},
        })
    rows.sort(key=lambda r: ((r["lastName"] or "").lower(), (r["firstName"] or "").lower()))

    others = [cid for cid in totals if cid not in active]
    return {
        "count": len(rows),
        "contracts": rows,
        "others": {
            "contracts": sum(1 for cid in others if cid is not None),
            "gross": round(sum(sum(totals[cid].values()) for cid in others), 2),
        },
    }
