"""The receipt of an Apify run — the same few fields on every `apify_*` answer that
starts or reads a run, so that a run can be audited and its output fetched again.

`{run_id, dataset_id, status, usage_total_usd, cost_usd, billing}`:

- `usage_total_usd` is Apify's own figure as served — "so far" while the run is going;
- `cost_usd` is the run's FINAL real cost, the figure the call traced for billing
  (`apify._releve`), or None while it is not final: never the booked part shown as the
  bill (`apify._cout`);
- `billing` says who pays, and when (`FACTURATIONS`). Credits are settled at the edge,
  AFTER the tool returns: the receipt gives dollars, never a credit count it would have
  to guess.

Where the answer is already a dict (`apify_run`, `apify_run_status`, `apify_abort_run`,
the error answers of a run), the receipt is ADDED under `receipt`. Where it is the LIST
of items (`apify_run_sync` and the dedicated actor tools, on success), the list is a
served contract (`waterfall/fournisseurs.py::_apify_items`, recipes): it stays as is,
and the receipt is one line at the head of the text channel (`session_org.noter_avis`).

Pure and tolerant of any shape: it runs after the run is billed, where nothing may raise.
"""
from __future__ import annotations

import math
from typing import Any, Optional

#: `billing` of a receipt.
#: - `credits`: the platform's Apify key; `cost_usd` was traced on THIS call, and the
#:   org's credits are debited from it;
#: - `apify_account`: an org's or a person's own Apify key; Apify bills that account
#:   directly, the platform debits nothing;
#: - `pending`: the run is not finished, or its cost is not final yet; a later
#:   `apify_run_status` (or the nightly settlement) bills it;
#: - `launching_org`: a platform-key run read from another org; the org that launched
#:   it is billed, never the reader.
FACTURATIONS = ("credits", "apify_account", "pending", "launching_org")

# The key rungs the platform's operator pays, and re-bills in credits.
_CLES_REFACTUREES = frozenset({"platform", "tenant"})


def _texte(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def _usd(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v) or v < 0:
        return None
    return float(v)


def recu(run: Any, run_id: Optional[str], *, key_mode: Optional[str],
         cout: Optional[float] = None, ici: bool = False) -> dict:
    """The receipt of `run` (the run object, `{data: …}` unwrapped).

    `cout` is the run's final cost when known, `ici` whether this call traced it for
    billing; `key_mode` is the rung of the key that served the call."""
    run = run if isinstance(run, dict) else {}
    cout = _usd(cout)
    if key_mode not in _CLES_REFACTUREES:
        facturation = "apify_account"
    elif cout is None:
        facturation = "pending"
    elif ici:
        facturation = "credits"
    else:
        facturation = "launching_org"
    return {
        "run_id": _texte(run.get("id")) or _texte(run_id),
        "dataset_id": _texte(run.get("defaultDatasetId")),
        "status": _texte(run.get("status")),
        "usage_total_usd": _usd(run.get("usageTotalUsd")),
        "cost_usd": cout,
        "billing": facturation,
    }


_DIT = {
    "credits": "billed in credits on this call",
    "apify_account": "paid by the Apify account of the key, nothing billed here",
    "pending": "not final yet: billed once finished — `apify_run_status`",
    "launching_org": "billed to the organization that launched it",
}


def ligne(r: dict) -> str:
    """The receipt as the one line that heads a LIST answer."""
    cout = r.get("cost_usd")
    usage = r.get("usage_total_usd")
    return (f"Apify receipt — run_id={r.get('run_id')} dataset_id={r.get('dataset_id')} "
            f"status={r.get('status')} cost_usd="
            f"{'null' if cout is None else f'{cout:.6f}'} usage_total_usd="
            f"{'null' if usage is None else f'{usage:.6f}'} billing={r.get('billing')} "
            f"({_DIT.get(r.get('billing'), '')}). Read the output again with "
            f"`apify_dataset_items(dataset_id=\"{r.get('dataset_id')}\")`.")
