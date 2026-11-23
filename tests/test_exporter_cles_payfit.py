"""Le script qui exporte les clés PayFit d'une équipe vers un tuyau
(`scripts/exporter_cles_payfit.py`).

Ce qui coûterait le plus cher à rater, dans l'ordre :
1. **une valeur affichée** — jamais sur stderr, ni dans un refus, ni dans une erreur ;
2. **une sortie partielle** — tout ou rien : au moindre refus, stdout reste vide ;
3. **une sortie sur un terminal** — refusée, hors `--compter`.

Les lectures (équipe, lignes, clé) sont remplacées : ce banc éprouve la logique du
script, pas le coffre, que ses propres bancs couvrent.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

# La racine du dépôt n'est pas dans `sys.path` en CI (pas de `__init__.py`).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import exporter_cles_payfit as E  # noqa: E402

ORG = 353
EQUIPE = 47
CLE_A = "pf-FICTIVE-ne-doit-jamais-sortir-AAAA"
CLE_B = "pf-FICTIVE-ne-doit-jamais-sortir-BBBB"


class Sortie(io.StringIO):
    def __init__(self, tty: bool = False):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _poser(monkeypatch, lignes, cles, org_de_l_equipe=ORG):
    monkeypatch.setattr(E, "lire_equipe",
                        lambda g: {"id": g, "org_id": org_de_l_equipe} if g == EQUIPE else None)
    monkeypatch.setattr(E, "lire_lignes", lambda g: [E.Ligne(a, m) for a, m in lignes])

    def lire_cle(g, account):
        v = cles[account]
        if isinstance(v, Exception):
            # Une erreur du coffre qui recopierait la valeur dans sa chaîne.
            raise E.Refus(f"compte {account!r} : clé illisible ({type(v).__name__})") from None
        return v
    monkeypatch.setattr(E, "lire_cle", lire_cle)


def _lancer(compter=False, tty=False):
    out, err = Sortie(tty=tty), io.StringIO()
    code = E.executer(ORG, EQUIPE, compter, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _sans_valeur(*textes):
    for t in textes:
        assert CLE_A not in t and CLE_B not in t
        assert "FICTIVE" not in t


def test_sortie_une_ligne_json_sans_autre_champ(monkeypatch):
    _poser(monkeypatch,
           [("a", {"label": "Clinique Saint-Éloi"}), ("b", {})],
           {"a": CLE_A, "b": CLE_B})
    code, out, err = _lancer()
    assert code == 0
    assert "\n" not in out
    assert json.loads(out) == [{"societe": "clinique-saint-eloi", "cle": CLE_A},
                               {"societe": "b", "cle": CLE_B}]
    assert "compte 'a' → clinique-saint-eloi" in err
    assert err.rstrip().endswith("JSON écrit : 2 société(s)")
    _sans_valeur(err)


def test_refus_sur_un_terminal(monkeypatch):
    _poser(monkeypatch, [("a", {})], {"a": CLE_A})
    code, out, err = _lancer(tty=True)
    assert code == 2 and out == ""
    assert "terminal" in err
    _sans_valeur(err)


def test_compter_accepte_un_terminal_et_n_ecrit_aucune_cle(monkeypatch):
    _poser(monkeypatch, [("a", {"label": "Société A"}), ("b", {})], {"a": CLE_A, "b": CLE_B})
    code, out, err = _lancer(compter=True, tty=True)
    assert code == 0 and out == ""
    assert "2 ligne(s) lue(s), 2 retenue(s), 0 exclue(s) — --compter" in err
    assert "compte 'a' → societe-a" in err
    _sans_valeur(err)


@pytest.mark.parametrize("lignes,cles,motif", [
    # une clé trop courte, APRÈS une bonne : rien ne sort quand même
    ([("a", {}), ("b", {})], {"a": CLE_A, "b": "court"}, "5 caractères"),
    # deux lignes, même nom court : pas de suffixe inventé
    ([("a", {"label": "Clinique A"}), ("b", {"label": "clinique-a"})],
     {"a": CLE_A, "b": CLE_B}, "en double"),
    # un libellé qui ne donne aucun nom court
    ([("a", {}), ("b", {"label": "—"})], {"a": CLE_A, "b": CLE_B}, "nom court vide"),
    # une ligne illisible
    ([("a", {}), ("b", {})], {"a": CLE_A, "b": ValueError(CLE_B)}, "illisible"),
])
def test_tout_ou_rien(monkeypatch, lignes, cles, motif):
    _poser(monkeypatch, lignes, cles)
    code, out, err = _lancer()
    assert code == 1 and out == ""
    assert motif in err and "rien n'est sorti" in err
    _sans_valeur(err)


def test_equipe_hors_de_l_org(monkeypatch):
    _poser(monkeypatch, [("a", {})], {"a": CLE_A}, org_de_l_equipe=ORG + 1)
    code, out, err = _lancer()
    assert code == 1 and out == ""
    assert "n'appartient pas à l'org" in err


def test_equipe_sans_ligne(monkeypatch):
    _poser(monkeypatch, [], {})
    code, out, err = _lancer()
    assert code == 1 and out == "" and "aucune clé payfit" in err


def test_ligne_suspendue_exclue_et_signalee(monkeypatch):
    _poser(monkeypatch, [("a", {}), ("b", {"suspended": True})], {"a": CLE_A, "b": CLE_B})
    code, out, err = _lancer()
    assert code == 0
    assert json.loads(out) == [{"societe": "a", "cle": CLE_A}]
    assert "exclue (suspendue) : compte 'b'" in err
    _sans_valeur(err)


def test_ligne_suspendue_au_libelle_invalide_ne_bloque_rien(monkeypatch):
    _poser(monkeypatch, [("a", {}), ("b", {"suspended": True, "label": "—"})],
           {"a": CLE_A, "b": CLE_B})
    code, out, err = _lancer()
    assert code == 0
    assert json.loads(out) == [{"societe": "a", "cle": CLE_A}]


def test_toutes_suspendues_rien_ne_sort(monkeypatch):
    _poser(monkeypatch, [("a", {"suspended": True})], {"a": CLE_A})
    code, out, err = _lancer()
    assert code == 1 and out == ""


def test_la_lecture_du_coffre_reduit_l_erreur_a_son_type(monkeypatch):
    """`lire_cle` réel : une exception du coffre qui recopie la valeur n'en laisse rien."""
    from oto_mcp import credentials_store as cs

    def fuit(*a, **k):
        raise RuntimeError(f"déchiffrement raté : {CLE_A}")
    monkeypatch.setattr(cs, "get_credential_with_meta", fuit)
    with pytest.raises(E.Refus) as exc:
        E.lire_cle(EQUIPE, "a")
    assert "RuntimeError" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    _sans_valeur(str(exc.value))


def test_main_n_imprime_jamais_de_pile(monkeypatch, capsys):
    def casse(*a, **k):
        raise RuntimeError(f"panne : {CLE_A}")
    monkeypatch.setattr(E, "lire_equipe", casse)
    canal = Sortie()
    code = E.main(["--org", str(ORG), "--equipe", str(EQUIPE)], canal=canal)
    assert code == 1 and canal.getvalue() == ""
    cap = capsys.readouterr()
    assert "RuntimeError" in cap.err and "Traceback" not in cap.err
    _sans_valeur(cap.err)


@pytest.mark.parametrize("brut,nom", [
    ("Clinique Saint-Éloi", "clinique-saint-eloi"),
    ("  SAS  Les  Érables  ", "sas-les-erables"),
    ("--a__b--", "a-b"),
])
def test_nom_court(brut, nom):
    assert E.nom_court(brut) == nom


_PARASITE = '''
import logging, os, sys
sys.path.insert(0, {racine!r})
from scripts import exporter_cles_payfit as E

def lignes(g):
    # Ce qu'un import d'oto ou une bibliothèque pourrait émettre pendant la collecte.
    print("parasite print")
    logging.getLogger("x").warning("parasite journal")
    os.write(1, b"parasite fd1")
    return [E.Ligne("a", {{}})]

E.lire_equipe = lambda g: {{"id": g, "org_id": 353}}
E.lire_lignes = lignes
E.lire_cle = lambda g, a: {cle!r}
sys.exit(E.main(["--org", "353", "--equipe", "47"]))
'''


def test_un_parasite_sur_stdout_n_entre_pas_dans_le_tuyau():
    """Bout en bout, dans un processus à part (le canal touche aux descripteurs) :
    stdout est un tuyau, et il ne porte QUE le JSON."""
    import subprocess
    racine = str(pathlib.Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-c", _PARASITE.format(racine=racine, cle=CLE_A)],
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode()
    assert r.stdout == json.dumps([{"societe": "a", "cle": CLE_A}],
                                  separators=(",", ":")).encode()
    err = r.stderr.decode()
    assert "parasite print" in err and "parasite fd1" in err
    assert "JSON écrit : 1 société(s)" in err
    _sans_valeur(err)
