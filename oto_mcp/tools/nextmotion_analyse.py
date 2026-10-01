"""Nextmotion — deux AGRÉGATS de la clinique : sa patientèle (code postal, ville, pays,
genre, tranche d'âge) et l'occupation de ses appareils. Aucune ligne patient ne sort.

Module frère de `nextmotion.py` (cf. `Connector.modules`).

## La patientèle : la liste des patients, lue pour COMPTER

Décision du 2026-10-01 (demande du marketing d'une clinique cliente) : la liste des
patients, hors périmètre jusque-là (cf. `nextmotion.py`), est lue par CE module et par
lui seul, pour en tirer des comptages. Ce qui tient la garde :

- **rien d'individuel ne sort** : ni ligne, ni id, ni nom ; l'outil lit chaque page,
  incrémente des compteurs et jette la page. Seuls les champs de dimension sont lus
  (`zip_code`, `city`, `country`, `gender`, `birth_date`) ; l'adresse, le nom, les
  coordonnées et les commentaires ne sont jamais touchés ;
- **une case de moins de `SEUIL` patients est masquée** (secret statistique) : elle ne
  sort ni par sa clé ni par son effectif, seul le total masqué est rendu. Croiser
  plusieurs dimensions fait vite tomber les cases sous le seuil ; c'est voulu ;
- **l'âge sort en tranche**, jamais en date de naissance ni en âge exact.

Risque résiduel assumé : deux appels sur des dimensions différentes peuvent, par
différence, approcher une petite case. Le seuil le rend coûteux, pas impossible.

Le profil socio-démographique d'un territoire (population, revenus, ménages) n'est
PAS recalculé ici : c'est l'open data, `urba_socio` / `urba_iris` par code INSEE.

## Les appareils : l'usage RÉSERVÉ à l'agenda

L'API n'a aucune statistique par appareil. L'outil lit l'agenda jour par jour (seul
filtre de date de `calendar_appointments`) et compte, par appareil, les rendez-vous
tenus, leurs minutes et les non tenus (annulé au dernier moment, absent, suspendu,
supprimé). C'est l'usage **réservé**, pas l'usage réel de la machine (tirs, durée
effective), que Nextmotion ne connaît pas.

Dérivé de la spec OpenAPI publique (lue le 2026-10-01) ; **jamais exercé avec une vraie
clé** : la forme réelle de `gender` (typé `string` sur le patient, entier 0/1/2
ailleurs) n'est pas vérifiée — une valeur inconnue LÈVE plutôt que d'être devinée.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Callable, Literal, Optional

from fastmcp import FastMCP

from .nextmotion_garde import _bad, _client, _need, _run

SEUIL = 10
PAGE = 100
MAX_PAGES_PATIENTS = 500  # 50 000 patients
MAX_PAGES_APPAREILS = 10
MAX_PAGES_JOUR = 20
MAX_JOURS = 93

_DIMENSIONS = ("zip_code", "department", "city", "country", "gender", "age_band")
_GENRES = {"0": "femme", "1": "homme", "2": "autre"}
_TRANCHES = ((18, "0-17"), (25, "18-24"), (35, "25-34"), (45, "35-44"), (55, "45-54"),
             (65, "55-64"))
_FRANCE = {"", "FR", "FRA", "FRANCE"}
_INCONNU = "inconnu"
_NON_TENUS = {"canceled_last_minute", "absent", "suspended", "deleted"}
_JOUR = re.compile(r"\d{4}-\d{2}-\d{2}")


# --- patientèle ----------------------------------------------------------------------

def _texte(valeur: Any) -> str:
    texte = str(valeur).strip().upper() if valeur is not None else ""
    return texte or _INCONNU


def _departement(patient: dict) -> str:
    """Le département d'un code postal français (Corse 2A/2B, outre-mer sur 3
    chiffres) ; « étranger » si le pays n'est pas la France. Un pays vide compte pour
    la France : la clinique y est, et Nextmotion ne remplit pas toujours le champ."""
    pays = str(patient.get("country") or "").strip().upper()
    if pays not in _FRANCE:
        return "étranger"
    cp = str(patient.get("zip_code") or "").strip()
    if not re.fullmatch(r"\d{5}", cp):
        return _INCONNU
    if cp.startswith("20"):
        return "2A" if cp < "20200" else "2B"
    return cp[:3] if cp.startswith(("97", "98")) else cp[:2]


def _genre(patient: dict) -> str:
    brut = patient.get("gender")
    if brut is None or str(brut).strip() == "":
        return _INCONNU
    genre = _GENRES.get(str(brut).strip())
    if genre is None:
        raise ValueError(f"Nextmotion : genre de patient inattendu ({str(brut)[:20]!r}) — "
                         "la spec annonce 0, 1 ou 2 ; agrégat interrompu plutôt que deviné.")
    return genre


def _tranche(patient: dict, aujourd_hui: date) -> str:
    brut = patient.get("birth_date")
    if brut is None or str(brut).strip() == "":
        return _INCONNU
    try:
        naissance = date.fromisoformat(str(brut).strip()[:10])
    except ValueError:
        # La valeur n'est pas citée : c'est une date de naissance.
        raise ValueError("Nextmotion : une date de naissance de patient n'est pas au "
                         "format AAAA-MM-JJ — agrégat interrompu.") from None
    age = aujourd_hui.year - naissance.year - (
        (aujourd_hui.month, aujourd_hui.day) < (naissance.month, naissance.day))
    if age < 0:
        return _INCONNU
    return next((nom for borne, nom in _TRANCHES if age < borne), "65+")


def _cle(patient: dict, dimensions: list, aujourd_hui: date) -> tuple:
    lire = {
        "zip_code": lambda p: _texte(p.get("zip_code")),
        "department": _departement,
        "city": lambda p: _texte(p.get("city")),
        "country": lambda p: _texte(p.get("country")),
        "gender": _genre,
        "age_band": lambda p: _tranche(p, aujourd_hui),
    }
    return tuple(lire[d](patient) for d in dimensions)


def _parcourir(lire_page: Callable[[int], Any], plafond: int, quoi: str):
    """Chaque ligne de chaque page amont, jusqu'à la dernière ; LÈVE si le plafond de
    pages coupe (un agrégat partiel présenté comme complet mentirait)."""
    offset = 0
    for _ in range(plafond):
        env = lire_page(offset)
        env = env if isinstance(env, dict) else {}
        lignes = env.get("data") or []
        yield from (ligne for ligne in lignes if isinstance(ligne, dict))
        if env.get("next") is None:
            return
        if not lignes:
            raise ValueError(f"Nextmotion : page vide alors que `next` annonce une suite "
                             f"({quoi}, offset {offset}).")
        offset += len(lignes)
    raise ValueError(f"Nextmotion : plus de {plafond * PAGE} {quoi} — plafond de lecture "
                     "atteint, agrégat non rendu.")


def patientele(lire_page: Callable[[int], Any], dimensions: list,
               aujourd_hui: date) -> dict:
    """Les effectifs par combinaison de `dimensions`, cases sous `SEUIL` masquées."""
    effectifs: Counter = Counter()
    for patient in _parcourir(lire_page, MAX_PAGES_PATIENTS, "patients"):
        effectifs[_cle(patient, dimensions, aujourd_hui)] += 1
    visibles = sorted(((k, n) for k, n in effectifs.items() if n >= SEUIL),
                      key=lambda kn: (-kn[1], kn[0]))
    masquees = [n for n in effectifs.values() if n < SEUIL]
    return {
        "dimensions": dimensions,
        "seuil": SEUIL,
        "patients": sum(effectifs.values()),
        "cellules": [{**dict(zip(dimensions, k)), "patients": n} for k, n in visibles],
        "masquees": {"cellules": len(masquees), "patients": sum(masquees)},
    }


# --- appareils -----------------------------------------------------------------------

def _jour(valeur: str, nom: str) -> date:
    if not isinstance(valeur, str) or not _JOUR.fullmatch(valeur):
        raise ValueError(f"`{nom}` doit être une date YYYY-MM-DD — reçu {valeur!r}.")
    return date.fromisoformat(valeur)


def _periode(jour: date, period_type: Optional[str]) -> Optional[str]:
    if period_type is None:
        return None
    if period_type == "day":
        return jour.isoformat()
    if period_type == "week":
        annee, semaine, _ = jour.isocalendar()
        return f"{annee}-W{semaine:02d}"
    return jour.strftime("%Y-%m")


def _minutes(rdv: dict) -> int:
    evt = rdv.get("calendar_event") if isinstance(rdv.get("calendar_event"), dict) else {}
    if isinstance(evt.get("duration_minutes"), int):
        return evt["duration_minutes"]
    try:
        debut = datetime.fromisoformat(evt["start_time"])
        fin = datetime.fromisoformat(evt["end_time"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Nextmotion : le rendez-vous {rdv.get('id')!r} n'a ni durée ni "
                         "bornes lisibles.") from None
    return int((fin - debut).total_seconds() // 60)


def _appareils_du_rdv(rdv: dict) -> dict:
    """{id: nom} des appareils d'un rendez-vous : `device`, plus ceux de son évènement."""
    evt = rdv.get("calendar_event") if isinstance(rdv.get("calendar_event"), dict) else {}
    tous = [rdv.get("device")] + list(evt.get("appointment_devices") or [])
    return {a["id"]: a.get("name") for a in tous if isinstance(a, dict) and a.get("id")}


def occupation(lire_jour: Callable[[date, int], Any], lire_appareils: Callable[[int], Any],
               debut: date, fin: date, period_type: Optional[str]) -> dict:
    """Par appareil (y compris ceux sans rendez-vous) : rendez-vous tenus, minutes
    réservées, non tenus — au total et, avec `period_type`, par période."""
    noms = {a["id"]: a.get("name") for a in _parcourir(lire_appareils, MAX_PAGES_APPAREILS,
                                                       "appareils") if a.get("id")}
    compteurs: dict = {}
    lus = sans_appareil = 0
    jour = debut
    while jour <= fin:
        for rdv in _parcourir(lambda o, j=jour: lire_jour(j, o), MAX_PAGES_JOUR,
                              f"rendez-vous le {jour.isoformat()}"):
            lus += 1
            statuts = set(rdv.get("statuses") or []) | {rdv.get("status")}
            tenu = not statuts & _NON_TENUS
            appareils = _appareils_du_rdv(rdv)
            if not appareils:
                sans_appareil += 1
            minutes = _minutes(rdv) if tenu and appareils else 0
            for ident, nom in appareils.items():
                noms.setdefault(ident, nom)
                for cle in {None, _periode(jour, period_type)}:
                    c = compteurs.setdefault((ident, cle), Counter())
                    c["rendez_vous" if tenu else "non_tenus"] += 1
                    c["minutes"] += minutes
        jour += timedelta(days=1)

    def _chiffres(ident: str, cle: Optional[str]) -> dict:
        c = compteurs.get((ident, cle), Counter())
        return {"rendez_vous": c["rendez_vous"], "minutes": c["minutes"],
                "non_tenus": c["non_tenus"]}

    appareils_out = []
    for ident in sorted(noms, key=lambda i: (-_chiffres(i, None)["minutes"], str(noms[i]))):
        ligne = {"device_id": ident, "name": noms[ident], **_chiffres(ident, None)}
        if period_type is not None:
            ligne["par_periode"] = [{"periode": cle, **_chiffres(ident, cle)}
                                    for cle in sorted(k for (i, k) in compteurs
                                                      if i == ident and k is not None)]
        appareils_out.append(ligne)
    return {"jours_lus": (fin - debut).days + 1, "rendez_vous_lus": lus,
            "sans_appareil": sans_appareil, "appareils": appareils_out}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def nextmotion_patient_demographics(
        clinic_id: str,
        by: Optional[list] = None,
        include_archived: Optional[bool] = None,
    ) -> dict:
        """Head counts of a Nextmotion clinic's clientele, by zip code, department,
        city, country, gender and/or age band — AGGREGATES ONLY: no patient row, id or
        name ever comes out, and any cell under 10 patients is masked (only the masked
        total is given). Crossing many dimensions masks more cells: start coarse.

        To profile a territory (population, income, households), do NOT ask Nextmotion:
        use the open-data tools — `urba_socio(code_insee)` per commune, `urba_iris` per
        neighbourhood (a zip code may span several INSEE communes; `foncier_geocode`
        gives the `citycode`).

        Reads the whole patient list (100 per upstream call), so a large clinic takes
        a while. Gender: femme | homme | autre | inconnu. Age bands: 0-17, 18-24,
        25-34, 35-44, 45-54, 55-64, 65+, inconnu (from the birth date, today).
        Department: from a French zip code (2A/2B, 3 digits overseas), « étranger »
        when the country is not France.

        Args:
            clinic_id: the clinic.
            by: dimensions to cross, among zip_code | department | city | country |
                gender | age_band (default ["zip_code"]).
            include_archived: also count archived patients (default False).
        """
        dimensions = ["zip_code"] if by is None else by
        if not isinstance(dimensions, list) or not dimensions:
            raise _bad("`by` doit être une liste non vide de dimensions.")
        inconnues = [d for d in dimensions if d not in _DIMENSIONS]
        if inconnues or len(set(dimensions)) != len(dimensions):
            raise _bad(f"`by` : dimensions parmi {', '.join(_DIMENSIONS)}, sans doublon "
                       f"— reçu {dimensions!r}.")
        _need("demographics", clinic_id=clinic_id)
        archives = bool(include_archived)
        c = _client()
        out = _run(lambda: patientele(
            lambda o: c.list_patients(clinic_id, is_archived=None if archives else False,
                                      limit=PAGE, offset=o),
            dimensions, date.today()))
        return {"clinic_id": clinic_id, "include_archived": archives, **out}

    @mcp.tool()
    def nextmotion_device_usage(
        clinic_id: str,
        start_date: str,
        end_date: str,
        period_type: Optional[Literal["day", "week", "month"]] = None,
    ) -> dict:
        """Booked use of a Nextmotion clinic's devices (machines) over a period, per
        device — every device of the clinic, unused ones at zero: appointments held,
        booked minutes, and appointments not held (cancelled last minute, absent,
        suspended, deleted; their minutes are not counted).

        ⚠️ This is the use BOOKED in the calendar, not the machine's real use (shots,
        effective time): Nextmotion does not know it. No occupancy rate is given —
        the API has no device capacity; divide `minutes` by your own capacity.

        Nextmotion filters appointments by ONE day only, so the tool reads the
        calendar day by day: at most 93 days per call (call again for a longer span).
        `sans_appareil` counts appointments with no device.

        Args:
            clinic_id: the clinic.
            start_date / end_date: YYYY-MM-DD, both inclusive, at most 93 days.
            period_type: day | week | month — adds a `par_periode` breakdown per
                device (omitted = totals only).
        """
        _need("device_usage", clinic_id=clinic_id)
        try:
            debut, fin = _jour(start_date, "start_date"), _jour(end_date, "end_date")
        except ValueError as e:
            raise _bad(str(e)) from None
        if debut > fin:
            raise _bad("`start_date` est postérieur à `end_date`.")
        if (fin - debut).days + 1 > MAX_JOURS:
            raise _bad(f"Période de plus de {MAX_JOURS} jours : découpe-la en plusieurs "
                       "appels.")
        c = _client()
        out = _run(lambda: occupation(
            lambda jour, o: c.list_appointments(clinic_id, date=jour.isoformat(),
                                                limit=PAGE, offset=o),
            lambda o: c.list_appointment_devices(clinic_id, limit=PAGE, offset=o),
            debut, fin, period_type))
        return {"clinic_id": clinic_id,
                "periode": {"start_date": start_date, "end_date": end_date,
                            "period_type": period_type}, **out}
