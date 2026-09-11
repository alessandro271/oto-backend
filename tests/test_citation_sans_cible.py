"""Une citation qui ne trouve rien se DIT (#611) — et l'asymétrie d'avant est fermée (#888).

Signalé le 28/08 : une page citait six autres pages, en tableau ET en ligne de
liens ; aucune des six ne la voyait dans ses liens entrants, alors que le sens
inverse s'indexait correctement. La cause, reproduite le 03/09 sur ce banc factice,
était une ASYMÉTRIE de portée : une page du projet ANCRÉ de l'org (l'ancienne « base
de connaissance ») résolvait ses `[[…]]` contre ce seul projet, tandis qu'une page de
projet résolvait contre `[projet, ancre]`.

Le 03/09, le banc choisissait de RENDRE VISIBLE l'asymétrie plutôt que de la corriger,
de peur qu'une portée « toute l'org » fasse résoudre « Start Here » n'importe où. Le
11/09/2026 (signaux #888, #890), la décision est prise : la résolution se fait parmi
les projets que possède l'org, et un titre porté par plusieurs d'entre eux est AMBIGU
— dit, jamais deviné. La crainte du 03/09 est tenue par l'ambiguïté : « Start Here »
présent dans deux projets ne résout nulle part, et l'écriture le dit.

Reste vrai, et ce banc le garde : un lien-souche n'est stocké nulle part, donc il doit
être DIT au moment de son écriture.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from oto_mcp.db import backlinks as B          # noqa: E402
from test_backlinks import _Conn               # noqa: E402

_ORG = {"owner_type": "org", "owner_id": "196", "context_org_id": None}
# La carte de tête vit dans un projet d'org (900) ; ses cibles dans un autre (100).
_PROJETS_ORG = [900, 100]
_DOCS = [
    {"id": 627, "project_id": 900, "title": "Company OS: Start Here"},
    {"id": 1191, "project_id": 100, "title": "Process Intelligence: Start Here"},
    {"id": 1196, "project_id": 100, "title": "Product: Start Here"},
]
_CORPS = "voir [[Product: Start Here]] et [[Process Intelligence: Start Here]]"


def test_l_asymetrie_entre_projets_d_org_est_FERMEE():
    """Le cas du signal : la carte de tête cite les pages d'un autre projet de l'org.
    Elles se lient maintenant, dans les deux sens."""
    depuis_la_carte = _Conn(project=_ORG, org_projects=_PROJETS_ORG, docs=_DOCS)
    B.refresh_links(depuis_la_carte, 627, 900, _CORPS)
    assert sorted(depuis_la_carte.inserted) == [(627, 1191), (627, 1196)]

    depuis_le_projet = _Conn(project=_ORG, org_projects=_PROJETS_ORG, docs=_DOCS)
    B.refresh_links(depuis_le_projet, 1196, 100, "voir [[Company OS: Start Here]]")
    assert depuis_le_projet.inserted == [(1196, 627)]


def test_l_ecriture_NOMME_les_citations_qui_ne_prennent_pas():
    """Le remède de #611 tient : une cible hors de portée — ici dans un projet qui
    n'est pas un projet de l'org (une équipe, un projet personnel, une autre org) —
    est DITE au moment de l'écriture, le seul où son auteur peut agir."""
    hors_org = [d if d["id"] == 627 else dict(d, project_id=555) for d in _DOCS]
    trace: dict = {}
    conn = _Conn(project=_ORG, org_projects=_PROJETS_ORG, docs=hors_org)
    B.refresh_links(conn, 627, 900, _CORPS, trace)
    assert trace["citations_sans_cible"] == ["Product: Start Here",
                                             "Process Intelligence: Start Here"]
    hint = trace["citations_sans_cible_hint"].casefold()
    assert "aucun lien entrant" in hint, "il faut dire la CONSÉQUENCE, pas le fait"
    assert "hors de" in hint and "portée" in hint, (
        "et la cause, sinon on cherche le mauvais défaut")
    assert "projets que possède l'organisation" in hint, "la portée se dit en projets"
    assert "base de connaissance" not in hint, (
        "la « base de connaissance » n'existe plus (10/09/2026)")
    assert "historique" not in hint, "l'ancre n'est plus une marche (11/09/2026)"


def test_le_releve_reste_VIDE_quand_tout_resout():
    trace: dict = {}
    conn = _Conn(project=_ORG, org_projects=_PROJETS_ORG, docs=_DOCS)
    B.refresh_links(conn, 1196, 100, "voir [[Company OS: Start Here]]", trace)
    assert trace == {}, "pas de clé parasite dans une écriture normale"


def test_la_LIMITE_est_dite_dans_la_description_servie():
    """Sans elle, `backlinks` continue de passer pour un contrôle d'orphelin — et
    c'est précisément l'usage qui a produit le signal."""
    from oto_mcp.capabilities.docs import core as C
    prose = " ".join(c.description or "" for c in C.CAPABILITIES
                     if c.key.startswith("me.doc"))
    assert "not symmetric" in prose
    assert "orphan check" in prose
    assert "citations_sans_cible" in prose
    assert "citations_ambigues" in prose


def test_la_description_servie_ne_promet_plus_une_portee_UNIQUE():
    """L'autre moitié, signal #696 : la description jurait « that scope is the
    WHOLE reach ». Vrai de la RÉSOLUTION, faux du GRAPHE — `op=backlinks` rend
    aussi les liens stockés avant qu'une page ne change de projet, que plus
    aucune écriture ne referait. Un agent qui voyait ces liens entrants venus
    d'un autre projet en a conclu que l'avertissement d'écriture mentait, et a
    réécrit tous ses renvois inter-projets en clair, perdant la navigation.

    Le fait est mesuré sur vrai PostgreSQL par `test_backlinks.py`
    (`test_un_lien_STOCKE_survit_au_deplacement_de_sa_cible_et_reste_rendu`) :
    ici on exige seulement que la surface servie le DISE."""
    from oto_mcp.capabilities.docs import core as C
    prose = " ".join(c.description or "" for c in C.CAPABILITIES
                     if c.key.startswith("me.doc"))
    assert "WHOLE reach" not in prose, (
        "la portée de résolution n'est PAS la portée du graphe — c'est cette "
        "phrase-là qui a fait croire à une contradiction")
    assert "every STORED link whatever its project" in prose
    assert "MOVED between projects" in prose
    # …et le cran doit être dit du côté qui le SUBIT : celui qui déplace une page.
    assert "A move is NOT free for links" in prose
