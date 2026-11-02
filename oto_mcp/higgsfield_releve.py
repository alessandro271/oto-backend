"""Settling the Higgsfield jobs nobody read back.

A job outlives its call: `create_generation` answers at once and the job finishes seconds
or minutes later. Higgsfield charges it only once it COMPLETES, and only a read of the
completed job (`higgsfield` op=get_request) bills its price. A job nobody reads back
costs the platform and bills nobody. So a platform-key job is recorded at submission with
its price (`db/higgsfield_jobs.py`), and this daily job does the rest:

Each job without a final state is read on the platform key. A completed one gets a
journal line shaped like the `higgsfield` read that would have billed it — tool,
`quantity` in micro-dollars, `higgsfield_request_id` — under the org and person who
SUBMITTED it, of nature `kind='settlement'` (`journal_jour.KIND_RELEVE`): the billing lens
reads it like any other, the usage lenses (`kind='mcp'`) do not count it as the person's
activity — nobody called anything. The biller keeps one line per job: a late read by the
submitter changes nothing. A failed, nsfw or canceled job closes at no cost.

No comparison with Higgsfield's own count, unlike Apify: Higgsfield has no balance or
usage endpoint to compare with.

Run by `oto-mcp maintenance higgsfield-jobs`, prod only (the database is shared).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import credentials_store
from .calllog import truncated_args
from .db import higgsfield_jobs, usage
from .db.journal_jour import KIND_RELEVE

logger = logging.getLogger(__name__)

#: A job still unfinished after this long is said in the log. Higgsfield deletes a job's
#: data seven days after it ends; a job queued that long is stuck.
_ABANDON_APRES = timedelta(days=2)


def _instant(valeur: Any) -> datetime:
    """A date as the store serves it: a string, UTC, without its zone (`_str_dict_row`)."""
    lue = (valeur if isinstance(valeur, datetime)
           else datetime.fromisoformat(str(valeur).replace("Z", "+00:00")))
    return lue if lue.tzinfo else lue.replace(tzinfo=timezone.utc)


class PlusieursClesPlateforme(RuntimeError):
    """More than one Higgsfield platform key: which one submitted a job is not recorded,
    so the settlement cannot pick one — keep a single platform key."""


def _cle_plateforme() -> Optional[str]:
    """The platform key, or None when this instance has none. Several RAISE: settling on
    the first one would read the others' jobs as unknown and close them without a cost."""
    lignes = credentials_store.list_platform_credentials("higgsfield")
    if not lignes:
        return None
    if len(lignes) > 1:
        noms = ", ".join(str(l["label"]) for l in lignes)
        raise PlusieursClesPlateforme(
            f"{len(lignes)} Higgsfield platform keys ({noms}): the settlement serves one — "
            "remove the others")
    ligne = credentials_store.get_credential_with_meta(
        credentials_store.PLATFORM, lignes[0]["label"], "higgsfield", "")
    if not ligne:
        raise RuntimeError("the Higgsfield platform row no longer resolves")
    return credentials_store.unpack_secret("higgsfield", ligne["secret"])["key"]


def _tracer(ligne: dict) -> None:
    """The billed line of a completed job, under its submitter. The tool and the argument
    name are the ones the `higgsfield` read writes: the billing lens reads it as the same
    job. Its nature (`KIND_RELEVE`) keeps it out of the usage lenses."""
    usage.insert_tool_call({
        "kind": KIND_RELEVE,
        "tool": "higgsfield", "sub": ligne["sub"], "org_id": ligne["org_id"],
        "run_id": ligne["run_id"], "ok": True, "duration_ms": 0,
        "quantity": int(ligne["prix_microdollars"]), "key_mode": "platform",
        "args": truncated_args({"higgsfield_request_id": ligne["request_id"],
                                "releve_serveur": True}),
    })


def _relever_un(client: Any, ligne: dict, *, dry_run: bool, maintenant: datetime) -> str:
    from oto.tools.common.errors import UpstreamHTTPError

    from .tools.higgsfield import TERMINAL
    request_id = ligne["request_id"]
    try:
        status = client.get_request(request_id)
    except UpstreamHTTPError as e:
        if e.status_code != 404:
            raise
        logger.warning("higgsfield job %s: unknown to Higgsfield, nothing to settle",
                       request_id)
        if not dry_run:
            higgsfield_jobs.marquer(request_id, statut="introuvable")
        return "introuvables"
    statut = status.get("status") if isinstance(status, dict) else None
    if statut not in TERMINAL:
        if maintenant - _instant(ligne["soumis_le"]) > _ABANDON_APRES:
            logger.warning("higgsfield job %s: %s after %s, still unsettled", request_id,
                           statut, _ABANDON_APRES)
        return "en_cours"
    if not dry_run:
        if statut == "completed" and int(ligne["prix_microdollars"]) > 0:
            _tracer(ligne)
        higgsfield_jobs.marquer(request_id, statut=statut)
    return "factures" if statut == "completed" else "gratuits"


def relever(cle: str, *, dry_run: bool = False) -> dict:
    """Settle the jobs without a final state. One failed read leaves that job for
    tomorrow."""
    from oto.tools.higgsfield import HiggsfieldClient
    client = HiggsfieldClient(api_key=cle)
    maintenant = datetime.now(timezone.utc)
    compte = {"factures": 0, "gratuits": 0, "en_cours": 0, "introuvables": 0, "echecs": 0}
    for ligne in higgsfield_jobs.a_relever():
        try:
            compte[_relever_un(client, ligne, dry_run=dry_run, maintenant=maintenant)] += 1
        except Exception:
            logger.warning("higgsfield job %s: settlement failed, retried tomorrow",
                           ligne["request_id"], exc_info=True)
            compte["echecs"] += 1
    if not dry_run:
        compte["purges"] = higgsfield_jobs.purger()
    return compte


def maintenance(*, dry_run: bool = False) -> dict:
    cle = _cle_plateforme()
    if cle is None:
        return {"cle_plateforme": False}
    return {"relevement": relever(cle, dry_run=dry_run)}
