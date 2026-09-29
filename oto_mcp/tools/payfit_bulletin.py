"""PayFit — les lignes HEURES SUP d'un bulletin, lues dans son texte.

L'API PayFit ne sert aucune ligne de bulletin : les heures supplémentaires sont
versées au même compte que le salaire de base dans les écritures comptables (641x),
et le temps de travail n'est qu'un total mensuel. Le seul endroit où elles se lisent
est le PDF du bulletin. Ce module en extrait les lignes qui les nomment — et RIEN
d'autre : l'appelant ne reçoit jamais le texte du bulletin (NIR, IBAN, adresse).

⚠️ **Le format du bulletin n'est pas un contrat.** pypdf rend chaque ligne du tableau
à plat, colonnes collées dans l'ordre de la page ; les libellés varient selon la
convention (heures complémentaires d'un temps partiel, majorations 10/25/50 %,
récupérations). On rend donc la ligne telle qu'elle est lue avec ses nombres dans
l'ordre, sans prétendre savoir lequel est la base, le taux ou le montant : c'est à
l'appelant de le confronter à un bulletin qu'il a sous les yeux.
"""
from __future__ import annotations

import re
import unicodedata

# Libellés d'heures payées en plus du contrat. Comparés sans accents ni casse.
_HEURES = re.compile(
    r"\bheures?\s+(?:supp?(?:lementaires?)?|compl(?:ementaires?)?|majorees?)\b"
    r"|\bh\.?\s?sup\b|\bhs\s?\d{2}\b|\bmajoration\s+\d{2}\s?%")
# Lignes qui NOMMENT les heures sup sans en être le paiement : réduction de
# cotisations, exonération, défiscalisation. Rendues à part, jamais mêlées.
_ALLEGEMENT = re.compile(r"\b(reduction|exoneration|deduction|defiscalis)")
# Nombre français : milliers séparés par espace (y compris insécables), virgule décimale.
_NOMBRE = re.compile(r"-?\d{1,3}(?:[   ]\d{3})+(?:,\d+)?|-?\d+(?:[,.]\d+)?")

PAIEMENT = "paiement"
ALLEGEMENT = "allegement"


def _plat(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _nombre(tok: str) -> float:
    return float(re.sub(r"[   ]", "", tok).replace(",", "."))


def overtime_lines(text: str) -> list[dict]:
    """Les lignes du texte d'un bulletin qui nomment des heures sup / complémentaires
    / majorées : `{kind, label, numbers, rates, line}`. `kind` = `paiement` ou
    `allegement` (réduction ou exonération de cotisations sur ces heures). `rates` =
    les pourcentages de la ligne, retirés de `numbers`."""
    out = []
    for brute in (text or "").splitlines():
        ligne = " ".join(brute.split())
        plat = _plat(ligne)
        if not ligne or not _HEURES.search(plat):
            continue
        rates = [_nombre(m) for m in re.findall(r"(\d+(?:[,.]\d+)?)\s?%", ligne)]
        sans_taux = re.sub(r"\d+(?:[,.]\d+)?\s?%", " ", ligne)
        m = _NOMBRE.search(sans_taux)
        label = (sans_taux[:m.start()] if m else sans_taux).strip(" :-")
        out.append({
            "kind": ALLEGEMENT if _ALLEGEMENT.search(plat) else PAIEMENT,
            "label": " ".join(label.split()),
            "numbers": [_nombre(t) for t in _NOMBRE.findall(sans_taux)],
            "rates": rates,
            "line": ligne,
        })
    return out
