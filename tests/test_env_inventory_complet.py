"""Complétude de l'inventaire des variables d'environnement (oto_mcp/env_inventory.py).

oto-backend#968 (ADR 0070). Généralise le patron de
`test_url_publique_sans_repli.py::test_aucune_adresse_publique_ne_se_fabrique_sur_un_defaut`
(walker AST sur `oto_mcp/**/*.py`) à TOUTE variable d'environnement, pas une seule : ce
test échoue si le code lit une variable que l'inventaire ne connaît pas.

**Asymétrie volontaire**, même patron que `test_org_store_surface_frozen.py` (FROZEN) :
il mord dans UN sens. Une entrée de l'inventaire que le code a cessé de lire ne fait
rougir personne — un retrait de lecture n'oblige pas à toucher l'inventaire dans le
même commit. Ce qui doit rougir, c'est l'inverse : une lecture neuve, jamais déclarée.

**Ce que le walker sait résoudre mécaniquement**, dans l'ordre où il essaie :

1. un littéral direct — `os.environ.get("X")`, `os.environ["X"]`, `require_env("X")` ;
2. une indirection par CONSTANTE MODULE-LEVEL du même fichier — `_VAR = "X"` puis
   `os.environ.get(_VAR)` (`db/_conn.py`, `auth/flow.py`, `egress.py`, `version.py`…) ;
3. une indirection par BOUCLE `for x in ("A", "B", "C"): os.environ.get(x)`, le tuple
   écrit sur place ou en constante module-level — la cascade `config.dashboard_url()`
   (`for var in DASHBOARD_VARS`) ;
4. une indirection par PARAMÈTRE d'une fonction-relais appelée avec des littéraux —
   `boucles_de_fond._interrupteur(var)`, appelée `_interrupteur("OTO_...")` à chaque
   site ; c'est aussi, sans cas particulier, comment il retombe sur ses pieds pour la
   lecture générique à l'intérieur de `config.require_env` lui-même (ses propres sites
   d'appel, littéraux, suffisent à la résoudre).

**Ce qu'il reconnaît par PATTERN plutôt que par résolution** : une construction
dynamique (`f"{x}_ID"`, jamais un littéral atteignable statiquement) est comparée à la
FORME déclarée des `FAMILLES_DYNAMIQUES` — préfixe/suffixe littéraux autour d'un trou.
Reconnue, elle n'a besoin d'aucun nom concret : la variable réelle n'existe qu'à
l'exécution (par provider, par annuaire tenant).

**L'échappatoire** — une indirection trop irrégulière pour tenir dans ces quatre
formes — n'a pas été nécessaire ici : les vingt sites non littéraux du fichier se
répartissent tous dans les quatre catégories ci-dessus (vérifié à l'écriture de ce
test). Si un futur site ne s'y range pas, le message d'échec le dit explicitement
(fichier:ligne + bout d'arbre) plutôt que de rougir à l'aveugle.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib

from oto_mcp import env_inventory as inv

RACINE = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"
_TROU = ""   # marque le segment dynamique d'un f-string dans le squelette comparé
                    # aux FAMILLES_DYNAMIQUES — un caractère de zone d'usage privé Unicode,
                    # qu'aucun code source légitime n'écrit dans un nom de variable.


def _est_os_environ(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _constantes_module(arbre: ast.Module) -> dict[str, str]:
    """`NOM = "littéral"` au niveau MODULE (pas dans une fonction/classe) — la seule
    profondeur où le rapport de cartographie en a rencontré."""
    out: dict[str, str] = {}
    for n in arbre.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)):
            out[n.targets[0].id] = n.value.value
    return out


def _litteraux(node: ast.expr) -> "tuple[str, ...] | None":
    """Les chaînes d'un tuple/liste/ensemble écrit en littéraux, ou None."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)) or not node.elts:
        return None
    if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return None
    return tuple(e.value for e in node.elts)


def _tuples_module(arbre: ast.Module) -> dict[str, tuple[str, ...]]:
    """`NOMS = ("A", "B")` au niveau MODULE — l'itérable d'une boucle de lecture."""
    out: dict[str, tuple[str, ...]] = {}
    for n in arbre.body:
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            valeurs = _litteraux(n.value)
            if valeurs is not None:
                out[n.targets[0].id] = valeurs
    return out


def _appels_par_nom(arbre: ast.Module) -> dict[str, list[ast.Call]]:
    """Tous les appels `f(...)` du fichier, groupés par nom de fonction — sert à
    résoudre une indirection par PARAMÈTRE : les sites d'appel de la fonction-relais
    portent le littéral que son corps, lui, ne connaît que sous un nom de paramètre."""
    out: dict[str, list[ast.Call]] = {}
    for n in ast.walk(arbre):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.setdefault(n.func.id, []).append(n)
    return out


class _Visiteur(ast.NodeVisitor):
    """Descend l'arbre d'UN fichier en portant deux contextes — la fonction englobante
    (pour la résolution par paramètre) et les boucles `for` englobantes dont l'itérable
    est un tuple de littéraux (pour la résolution par boucle)."""

    def __init__(self, fichier: str, constantes: dict[str, str],
                 appels_par_nom: dict[str, list[ast.Call]],
                 tuples: "dict[str, tuple[str, ...]] | None" = None):
        self.fichier = fichier
        self.constantes = constantes
        self.tuples = tuples or {}
        self.appels_par_nom = appels_par_nom
        self._pile_fonctions: list[ast.FunctionDef] = []
        self._pile_boucles: list[tuple[str, tuple[str, ...]]] = []
        self.resolues: list[tuple[str, int]] = []          # (nom_var_env, ligne)
        self.echecs: list[tuple[int, str]] = []             # (ligne, raison)
        # (nom_var_env, ligne, défaut littéral) — `os.environ.get(X, "d")` et
        # `os.environ.get(X) or "d"` : ce que le process prend quand X manque.
        self.defauts: list[tuple[str, int, str]] = []

    # -- contexte -----------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._pile_fonctions.append(node)
        self.generic_visit(node)
        self._pile_fonctions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_For(self, node: ast.For) -> None:
        resolu = self._resoudre_boucle(node)
        if resolu:
            self._pile_boucles.append(resolu)
        self.generic_visit(node)
        if resolu:
            self._pile_boucles.pop()

    def _resoudre_boucle(self, node: ast.For) -> "tuple[str, tuple[str, ...]] | None":
        if not isinstance(node.target, ast.Name):
            return None
        if isinstance(node.iter, ast.Name):
            valeurs = self.tuples.get(node.iter.id)
        else:
            valeurs = _litteraux(node.iter)
        return (node.target.id, valeurs) if valeurs else None

    # -- les lectures ---------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        if (isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "setdefault")
                and _est_os_environ(node.func.value) and node.args):
            self._traiter(node.args[0], node.lineno, f"os.environ.{node.func.attr}")
            if len(node.args) > 1:
                self._noter_defaut(node, node.args[1])
        elif (isinstance(node.func, ast.Attribute) and node.func.attr == "getenv"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                and node.args):
            self._traiter(node.args[0], node.lineno, "os.getenv")
            if len(node.args) > 1:
                self._noter_defaut(node, node.args[1])
        elif isinstance(node.func, ast.Name) and node.func.id == "require_env" and node.args:
            self._traiter(node.args[0], node.lineno, "require_env")
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # `lecture or "défaut"` : le littéral qui suit une lecture est son défaut.
        if isinstance(node.op, ast.Or):
            for gauche, droite in zip(node.values, node.values[1:]):
                if _est_lecture_env(gauche):
                    self._noter_defaut(gauche, droite)
        self.generic_visit(node)

    def _noter_defaut(self, lecture: ast.expr, defaut: ast.expr) -> None:
        if not (isinstance(defaut, ast.Constant) and isinstance(defaut.value, str)):
            return
        noms = self._resoudre(_cle_lue(lecture)) or ("<nom non résolu>",)
        for nom in noms:
            self.defauts.append((nom, lecture.lineno, defaut.value))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _est_os_environ(node.value) and isinstance(node.slice, ast.expr):
            self._traiter(node.slice, node.lineno, "os.environ[]")
        self.generic_visit(node)

    # -- résolution -------------------------------------------------------------------
    def _traiter(self, arg: ast.expr, ligne: int, site: str) -> None:
        littéraux = self._resoudre(arg)
        if littéraux is not None:
            for nom in littéraux:
                self.resolues.append((nom, ligne))
            return
        if isinstance(arg, ast.JoinedStr) and self._motif_dynamique_reconnu(arg):
            return
        self.echecs.append((
            ligne,
            f"{site} — argument non résolu par le walker "
            f"({ast.dump(arg, annotate_fields=False)[:140]})"))

    def _resoudre(self, arg: ast.expr) -> "tuple[str, ...] | None":
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return (arg.value,)
        if not isinstance(arg, ast.Name):
            return None
        if arg.id in self.constantes:
            return (self.constantes[arg.id],)
        for var, littéraux in reversed(self._pile_boucles):
            if var == arg.id:
                return littéraux
        if self._pile_fonctions:
            fn = self._pile_fonctions[-1]
            params = [a.arg for a in fn.args.args]
            if arg.id in params:
                idx = params.index(arg.id)
                appels = self.appels_par_nom.get(fn.name, [])
                if not appels:
                    return None
                trouvés = [self._arg_litteral(appel, idx, arg.id) for appel in appels]
                if all(t is not None for t in trouvés):
                    return tuple(trouvés)  # type: ignore[arg-type]
        return None

    @staticmethod
    def _arg_litteral(appel: ast.Call, idx: int, nom_param: str) -> "str | None":
        if idx < len(appel.args):
            a = appel.args[idx]
            return a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
        for kw in appel.keywords:
            if kw.arg == nom_param and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                return kw.value.value
        return None

    @staticmethod
    def _motif_dynamique_reconnu(node: ast.JoinedStr) -> bool:
        squelette = "".join(
            v.value if isinstance(v, ast.Constant) else _TROU
            for v in node.values)
        return any(f.motif.match(squelette) for f in inv.FAMILLES_DYNAMIQUES)


def _est_lecture_env(node: ast.expr) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.args):
        return False
    if node.func.attr == "get" and _est_os_environ(node.func.value):
        return True
    return (node.func.attr == "getenv" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os")


def _cle_lue(lecture: ast.expr) -> ast.expr:
    assert isinstance(lecture, ast.Call)
    return lecture.args[0]


def _analyser(chemin: pathlib.Path) -> _Visiteur:
    arbre = ast.parse(chemin.read_text(encoding="utf-8"), filename=str(chemin))
    v = _Visiteur(str(chemin), _constantes_module(arbre), _appels_par_nom(arbre),
                  _tuples_module(arbre))
    v.visit(arbre)
    return v


def test_toute_lecture_d_environnement_est_dans_l_inventaire():
    connues = set(inv.par_nom())
    manquantes: list[str] = []
    non_resolues: list[str] = []

    for fichier in sorted(RACINE.rglob("*.py")):
        v = _analyser(fichier)
        rel = fichier.relative_to(RACINE.parent)
        for nom, ligne in v.resolues:
            if nom not in connues:
                manquantes.append(f"{rel}:{ligne} → {nom!r} absente de env_inventory.NOMS_FIXES")
        for ligne, raison in v.echecs:
            non_resolues.append(f"{rel}:{ligne} → {raison}")

    assert not non_resolues, (
        "le walker ne sait pas résoudre ces lectures (ni littéral, ni indirection "
        "reconnue, ni famille dynamique) :\n    " + "\n    ".join(non_resolues) +
        "\n  → soit la ramener à une forme reconnue (constante module-level, boucle "
        "sur un tuple de littéraux, fonction-relais appelée avec des littéraux), soit "
        "déclarer une FamilleDynamique dans env_inventory.py si le nom est réellement "
        "construit au runtime.")

    assert not manquantes, (
        "le code lit une variable d'environnement que l'inventaire ne connaît pas :\n"
        "    " + "\n    ".join(manquantes) +
        "\n  → ajoute une entrée dans oto_mcp/env_inventory.py (NOMS_FIXES), classée "
        "REQUISE / IDENTITE / REGLAGE. Ce test ne rougit que dans CE sens : une "
        "entrée de l'inventaire que le code a cessé de lire ne le fait pas rougir.")


# ── Ce que la DÉPENDANCE lit au nom du serveur (relevé du 29/09/2026) ─────────
#
# Une variable lue par oto-core échappait à toute garde : le walker ne parcourait que
# `oto_mcp/` (cas d'origine, `OTO_CONFIG_DISABLE_SOPS`, retirée avec la lecture de
# secrets de la lib en v1.148.0). On parcourt donc oto-core AU PIN (le paquet
# installé), et chaque lecture LITTÉRALE qu'on y trouve est rangée — lue au nom du
# serveur (`NOMS_FIXES`) ou hors serveur, avec sa raison
# (`LUES_PAR_OTO_CORE_HORS_SERVEUR`). Depuis v1.148.0 la lib ne lit plus aucun secret :
# un secret de connecteur arrive toujours en argument, jamais par l'environnement.


def _racines_oto_core() -> list[pathlib.Path]:
    spec = importlib.util.find_spec("oto")
    assert spec is not None and spec.submodule_search_locations, \
        "oto-core n'est pas installé : la garde ne peut pas le parcourir"
    return [pathlib.Path(p) for p in spec.submodule_search_locations]


def _lectures_oto_core() -> dict[str, list[str]]:
    lues: dict[str, list[str]] = {}
    for racine in _racines_oto_core():
        for fichier in sorted(racine.rglob("*.py")):
            for nom, ligne in _analyser(fichier).resolues:
                lues.setdefault(nom, []).append(
                    f"oto-core:oto/{fichier.relative_to(racine)}:{ligne}")
    return lues


def test_toute_lecture_d_oto_core_est_rangee():
    connues = set(inv.par_nom()) | set(inv.LUES_PAR_OTO_CORE_HORS_SERVEUR)
    orphelines = [f"{sites[0]} → {nom!r}" for nom, sites in sorted(_lectures_oto_core().items())
                  if nom not in connues]
    assert not orphelines, (
        "oto-core lit une variable d'environnement que l'inventaire ne range pas :\n"
        "    " + "\n    ".join(orphelines) +
        "\n  → lue au nom du serveur : une entrée dans NOMS_FIXES ; sinon une entrée "
        "dans env_inventory.LUES_PAR_OTO_CORE_HORS_SERVEUR, avec la raison pour "
        "laquelle le serveur n'en dépend pas.")


def test_le_parcours_d_oto_core_mord():
    """Preuve, pas affirmation : le parcours voit bien une lecture réelle de la lib
    (les clients oto-core qui appellent l'API oto lisent `OTO_API_URL`)."""
    assert "OTO_API_URL" in _lectures_oto_core()
    arbre = ast.parse('import os\nA = os.getenv("X_G")\n')
    v = _Visiteur("<banc>", {}, {}, {})
    v.visit(arbre)
    assert [n for n, _ in v.resolues] == ["X_G"]


def test_familles_dynamiques_ont_un_motif_qui_mord() -> None:
    """Preuve, pas affirmation : le motif de chaque famille reconnaît vraiment un
    exemple de sa forme, et ne reconnaît PAS un nom fixe ordinaire pris au hasard."""
    for f in inv.FAMILLES_DYNAMIQUES:
        exemple = f.prefixe + _TROU + f.suffixe
        assert f.motif.match(exemple), f"le motif de {f.nom!r} ne reconnaît pas sa propre forme"
    assert not any(f.motif.match("DATABASE_URL") for f in inv.FAMILLES_DYNAMIQUES)
    # ⚠️ Le motif n'est comparé QU'au squelette d'une construction f-string non
    # résolue (`ast.JoinedStr`) — jamais à un nom déjà littéral. Un nom FIXE qui finit
    # en `_SECRET` (`OTO_MCP_OAUTH_STATE_SECRET`) n'est donc PAS ambigu avec la famille
    # `credential_management_annuaire_secret` : il est lu via un littéral direct,
    # résolu et vérifié contre `NOMS_FIXES` avant que le chemin dynamique n'entre en
    # jeu (`_Visiteur._traiter` n'essaie une famille qu'après échec de `_resoudre`).


# ── Aucun défaut ne pointe chez nous (décision du 28/09/2026, #968) ─────────────
#
# Ce qui distingue « chez nous » d'un défaut neutre : un de nos domaines dans la valeur.
# Liste de FORMES, pas d'adresses — elle attrape aussi une adresse qu'on n'a pas encore.
_NOS_DOMAINES = ("oto.cx", "oto.ninja", "oto.zone", "otomata")


def test_aucun_defaut_du_code_ne_pointe_chez_nous():
    """Le code ne prend jamais une de nos adresses quand une variable manque : ni
    `os.environ.get(X, "<nous>")`, ni `os.environ.get(X) or "<nous>"`. Une valeur
    émise sous le nom de l'instance se DÉCLARE (classe IDENTITE) — sans elle, refus."""
    fautes = []
    for fichier in sorted(RACINE.rglob("*.py")):
        rel = fichier.relative_to(RACINE.parent)
        for nom, ligne, defaut in _analyser(fichier).defauts:
            if any(d in defaut.lower() for d in _NOS_DOMAINES):
                fautes.append(f"{rel}:{ligne} → {nom} retombe sur {defaut!r}")
    assert not fautes, (
        "une lecture d'environnement retombe sur une de NOS adresses :\n    "
        + "\n    ".join(fautes)
        + "\n  → retire le défaut (`config.require_env`) et classe la variable IDENTITE.")


def test_l_inventaire_ne_documente_aucun_defaut_chez_nous():
    for v in inv.NOMS_FIXES:
        assert not any(d in (v.defaut or "").lower() for d in _NOS_DOMAINES), v.nom
        if v.classe in (inv.Classe.REQUISE, inv.Classe.IDENTITE):
            assert v.defaut is None, f"{v.nom} est {v.classe.value} : elle n'a pas de défaut"


def test_le_detecteur_de_defauts_mord():
    """Preuve, pas affirmation : les deux formes de repli sont bien relevées."""
    arbre = ast.parse(
        'import os\n'
        'A = os.environ.get("X_A", "https://oto.cx")\n'
        'B = os.environ.get("X_B") or "https://app.oto.ninja"\n')
    v = _Visiteur("<banc>", _constantes_module(arbre), _appels_par_nom(arbre),
                  _tuples_module(arbre))
    v.visit(arbre)
    assert {(n, d) for n, _, d in v.defauts} == {
        ("X_A", "https://oto.cx"), ("X_B", "https://app.oto.ninja")}
