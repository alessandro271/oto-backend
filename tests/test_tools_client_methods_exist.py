"""Garde-fou version-skew (otomata-private, leçon folk_user) — un tool ne doit
PAS référencer une méthode absente de l'oto-core ÉPINGLÉ.

Contexte : backend et oto-core sont deux repos ; le backend épingle une version
d'oto-core (pin git dans pyproject, ADR 0020). Un tool mergé en avance de phase —
qui appelle `client.methode()` avant que le tag épinglé la contienne — passe la
CI (l'import du module réussit, la méthode n'est touchée qu'à l'appel) puis lève
`AttributeError` en prod à la 1ʳᵉ invocation (vécu 2026-07-01→03 : `folk_user`
→ `FolkClient` sans `get_user` sur v1.11.0, corrigé par le bump v1.12.0).

Cette sonde ferme la fenêtre : en CI de PR, oto-core est installé AU TAG ÉPINGLÉ
(runner neuf → pin du pyproject) ; on vérifie STATIQUEMENT que chaque `_client().m()`
d'un tool existe sur la vraie classe. Une méthode manquante casse la PR au lieu
d'atteindre la prod.

Portée = `def _client(...) -> <ClasseConcrète>` **ou** `-> tuple[<Classe>, …]`, et
les appels sur le client, qu'il soit chaîné (`_client().m()`), lié à une variable
(`client, _ = _client()` puis `client.m()`) ou passé à un dispatcher (`c.m()`). La
fabrique peut être `async def` : le premier connecteur dont le cœur est asynchrone
(`planity`, 2026-09-09) serait sinon sorti de la sonde SANS que rien ne le dise —
`ast.AsyncFunctionDef` n'est pas un `ast.FunctionDef`, donc l'annotation de retour
ne se lisait pas, et `test_no_module_silently_uncovered` ne voyait même pas qu'il y
avait un `_client`. Un angle mort qui s'ouvre au moment où un connecteur adopte une
forme neuve est exactement celui qu'on ne trouve jamais par relecture.

⚠️ Élargie le 2026-09-09 à la méthode **confiée** : `instagram_meta` n'appelle
jamais son client, il passe la méthode à un exécuteur qui la joue hors boucle
(`await appeler("le profil", ig.get_profile)`). Le nom n'est alors PAS le `func`
d'un `ast.Call` mais un argument, donc la sonde ne voyait aucune méthode et sortait
le module de la couverture. C'est une forme qui va se répandre : dès qu'un cœur est
synchrone et le serveur mono-loop, l'appel part à `asyncio.to_thread` ou à une
enveloppe maison (`_run`, `appeler`, `functools.partial`) — le site d'appel n'est
plus le site d'usage. La sonde compte donc aussi les méthodes **remises à un
appelant**, quel que soit cet appelant : `minari` et `snitcher` y gagnent chacun une
méthode qu'ils perdaient déjà en silence (`list_custom_fields`, `get_me`).

⚠️ La portée a été élargie le 2026-07-31 après un trou vécu : `tools/apollo.py`
déclare `def _client() -> tuple[ApolloClient, bool]` et appelle `client.m()` — les
DEUX motifs échappaient à la sonde, donc **le module entier sortait de la couverture
en silence**. Un `apollo_bulk_enrich_organizations` appelant une méthode absente de
l'oto-core épinglé serait passé en CI pour lever `AttributeError` en prod : très
exactement ce que ce fichier existe pour empêcher. Le docstring promettait que les
modules hors convention seraient « listés » — personne ne les listait ; c'est
maintenant un test (`test_no_module_silently_uncovered`)."""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

# La sonde compare les tools à l'oto-core ÉPINGLÉ : lancée contre un autre, elle ne
# mesure rien — ni ses rouges (méthodes absentes d'un client périmé) ni ses verts
# (méthodes présentes dans un client que le tronc n'épingle pas : le faux vert
# symétrique). Donc NON CONCLUANTE en bloc, pas connecteur par connecteur.
# Cf. `tests/_oto_core_pin.py`.
pytestmark = pytest.mark.exige_pin_oto_core

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "oto_mcp" / "tools"

# Modules sans `_client()` du tout (spine, dispatchers, connecteurs sans client
# oto-core) : hors sujet, pas un trou. Tout AUTRE module non couvert casse le test
# de couverture — la sonde ne doit jamais rétrécir sans que ça se voie.
_NO_CLIENT_EXPECTED = {
    "meta", "whoami", "docs_app", "datastore", "remote", "mount",
}

# Clients à SOUS-OBJETS (`client.companies.list(…)`) : leurs datastores sont des
# attributs d'instance, invérifiables sur la classe → hors de portée de cette sonde
# statique. Déclarés ici pour que ça reste un choix visible, pas un oubli.
_SUBOBJECT_CLIENTS = {"attio"}

# Dispatch DYNAMIQUE (`getattr(client, method)(**kw)`) : le nom de la méthode est une
# donnée, pas du code — invérifiable statiquement. Ces connecteurs restent donc à
# découvert sur le version-skew ; c'est un choix, pas un oubli, et l'ajouter à cette
# liste doit rester un geste conscient (sinon un nouveau connecteur perdrait sa
# couverture en silence, ce qui est précisément le trou qu'on vient de fermer).
# `ahrefs` : PARTIELLEMENT dynamique — `_call_report` fait `getattr(client,
# method_name)(**kwargs)` pour les 4 tools à axe `report=` (site_explorer/
# keywords_explorer/site_audit/rank_tracker) + les rapports Brand Radar ; les
# tools Management/Social/Public appellent le client en clair et RESTENT
# couverts (des appels littéraux existent dans le module, `_covered_modules()`
# ne consulte pas cet ensemble). Le trou dynamique est recouvert à la main par
# `test_dispatch_tables_point_to_real_client_methods` (oto-backend/tests/
# test_ahrefs.py) : chaque entrée de chaque table de dispatch est vérifiée
# contre `AhrefsClient` au lieu d'être laissée hors de portée.
_DYNAMIC_DISPATCH_CLIENTS = {"serper", "serpapi", "brightdata", "cloro", "spott", "ahrefs"}

# Fabriques de client reconnues. Un connecteur à DEUX régimes de clé en a deux
# (apollo : `_client()` admet le palier plateforme, `_client_byo()` ne résout que
# la clé du propriétaire) — et les deux rendent la MÊME classe, donc les deux
# doivent être suivies. Sans `_client_byo`, les tools byo-only d'apollo n'étaient
# couverts que par ACCIDENT : leur variable s'appelle `client`, un nom que
# `_client()` lie ailleurs dans le même fichier. Un appel byo CHAÎNÉ
# (`_client_byo().m()`) ou tenu sous un autre nom échappait donc entièrement à la
# sonde — le trou exact que ce fichier a déjà refermé une fois pour apollo (cf.
# docstring, 2026-07-31). Ajouter une 3ᵉ fabrique = une ligne ici, délibérée.
_CLIENT_FACTORIES = ("_client", "_client_byo")


def _client_class_name(tree: ast.Module) -> str | None:
    """Classe concrète rendue par `_client()` — annotation `Name` (`-> FolkClient`)
    ou premier élément d'un `tuple[...]` (`-> tuple[ApolloClient, bool]`, la forme
    des connecteurs qui rendent aussi « clé plateforme ? »). None si absente."""
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_client"):
            continue
        ret = node.returns
        if isinstance(ret, ast.Name):
            return ret.id
        # tuple[ApolloClient, bool] → ApolloClient
        if isinstance(ret, ast.Subscript):
            base = ret.value
            if isinstance(base, ast.Name) and base.id in ("tuple", "Tuple"):
                sl = ret.slice
                elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
                if elts and isinstance(elts[0], ast.Name):
                    return elts[0].id
    return None


def _names_bound_to_client(tree: ast.Module) -> set[str]:
    """Variables qui REÇOIVENT le client : `client = _client()`, le dépaquetage
    `client, is_platform = _client()`, et les mêmes formes sur toute fabrique de
    `_CLIENT_FACTORIES` (`_client_byo()`). Sans ça, tout connecteur qui passe par
    une variable (au lieu de chaîner) est invisible à la sonde."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        # `client = await _client()` : l'attente enveloppe l'appel. Sans ce
        # dépliage, une fabrique asynchrone ne lie plus rien et son connecteur
        # ne tient sa couverture que par le nom conventionnel `c`.
        if isinstance(val, ast.Await):
            val = val.value
        if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
                and val.func.id in _CLIENT_FACTORIES):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Tuple) and target.elts:
                # 1er élément = le client (le reste = is_platform…)
                first = target.elts[0]
                if isinstance(first, ast.Name):
                    names.add(first.id)
    return names


def _import_of(tree: ast.Module, clsname: str) -> str | None:
    """Module d'origine d'un `from <module> import <clsname>` (n'importe où, y
    compris imports imbriqués dans `register`)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == clsname:
                    return node.module
    return None


def _fabrique_importee(tree: ast.Module) -> str | None:
    """Module FRÈRE d'où vient `_client` quand le module ne le définit pas.

    Un connecteur découpé partage sa fabrique (`from .pennylane_socle import
    _client`) plutôt que de la recopier — recopier le résolveur de clé serait
    pire. Mais sans ce chaînage, chaque module d'un connecteur découpé sort de
    la sonde EN SILENCE : c'est le trou d'apollo (2026-07-31) par un autre
    chemin, et il s'ouvre au moment précis où un connecteur grandit assez pour
    être découpé — donc là où il y a le plus de méthodes neuves à vérifier.
    """
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
                and any(a.name in _CLIENT_FACTORIES for a in node.names)):
            return node.module
    return None


def _est_fabrique_partagee(stem: str, tree: ast.Module) -> bool:
    """Un module qui FABRIQUE le client sans jamais l'utiliser.

    Il n'a rien à vérifier par lui-même : les appels vivent chez ses
    consommateurs, que la sonde couvre via `_fabrique_importee`. On l'établit
    mécaniquement — définit `_client`, n'appelle aucune méthode, et au moins un
    module frère importe sa fabrique — plutôt que par une liste : une liste
    d'exemption vieillit sans qu'on le voie.
    """
    if _methods_called_on_client(tree):
        return False
    if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in _CLIENT_FACTORIES for n in ast.walk(tree)):
        return False
    for autre in _TOOLS_DIR.glob("*.py"):
        if autre.stem == stem:
            continue
        try:
            if _fabrique_importee(ast.parse(autre.read_text())) == stem:
                return True
        except SyntaxError:
            continue
    return False


def _classe_et_arbre_fabrique(tree: ast.Module) -> tuple[str | None, ast.Module]:
    """`(classe rendue par la fabrique, arbre où on l'a lue)`.

    Une seule résolution pour les deux lecteurs — la couverture ET le diagnostic
    du module non couvert. Séparées, elles divergent : le message d'échec disait
    « type de retour de `_client()` non reconnu » pour `instagram_meta`, dont la
    fabrique vit chez un frère et annonce très bien sa classe. Un garde-fou qui
    nomme la mauvaise cause envoie corriger la mauvaise chose."""
    cls = _client_class_name(tree)
    if cls:
        return cls, tree
    socle = _fabrique_importee(tree)
    chemin = _TOOLS_DIR / f"{socle}.py" if socle else None
    if chemin and chemin.exists():
        arbre = ast.parse(chemin.read_text(), filename=str(chemin))
        return _client_class_name(arbre), arbre
    return None, tree


def _methods_called_on_client(tree: ast.Module) -> set[str]:
    """Méthodes appelées sur le client, quelle que soit la façon de le tenir :

    - **chaîné** `_client().m()` / `_client_byo().m()` ;
    - **lié** `client, _ = _client()` puis `client.m()` (cf. `_names_bound_to_client`) ;
    - **passé** `_create_one(c, …)` puis `c.m()` — nom conventionnel du client dans
      un dispatcher partagé (ex. Folk factorise singulier/bulk) ; sans ce motif, un
      connecteur qui factorise perd toute couverture version-skew ;
    - **confié** `appeler(geste, ig.get_profile)` / `_run(client.get_me)` /
      `asyncio.to_thread(client.m, …)` — la méthode est remise à un exécuteur qui
      l'appellera ailleurs. Le nom est alors un ARGUMENT, jamais le `func` d'un
      `Call` : sans ce motif, un connecteur dont le cœur est synchrone sur un
      serveur mono-loop sort entièrement de la sonde (`instagram_meta`, 2026-09-09).

    ⚠️ Seuls les attributs **appelés** ou **confiés** comptent — jamais un attribut
    du client lu sur place. Un client à sous-objets (`client.companies.list(…)`,
    Attio) porte ses datastores en attributs d'INSTANCE : `hasattr(AttioClient,
    "companies")` est False sur la classe, donc les compter produirait un faux
    « méthode absente » — un garde-fou qui crie à tort finit ignoré. La position
    d'argument est ce qui distingue les deux : `client.companies` n'y apparaît
    jamais, il n'est que le receveur de l'attribut suivant.
    """
    bound = _names_bound_to_client(tree) | {"c"}

    def _est_le_client(node: ast.expr) -> bool:
        """Le client, chaîné (`_client()`) ou tenu par une variable (`ig`)."""
        if isinstance(node, ast.Name):
            return node.id in bound
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _CLIENT_FACTORIES)

    methods: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # appelée ici : `client.m(...)`
        if isinstance(node.func, ast.Attribute) and _est_le_client(node.func.value):
            methods.add(node.func.attr)
        # confiée à qui l'appellera : `appeler(geste, ig.m)`, `_run(client.m)`…
        # ⚠️ En position d'ARGUMENT seulement. Compter tout attribut nu du client
        # ferait entrer ses attributs d'INSTANCE (`c.session_id`, `client.host`,
        # `client.companies` d'Attio) que `hasattr(Classe, …)` dit absents : la
        # sonde crierait à tort, et un garde-fou qui crie à tort finit ignoré.
        for arg in (*node.args, *(kw.value for kw in node.keywords)):
            if isinstance(arg, ast.Attribute) and _est_le_client(arg.value):
                methods.add(arg.attr)
    return methods


def _kwargs_called_on_client(tree: ast.Module) -> dict[str, set[str]]:
    """`{méthode: {kwarg, …}}` pour les appels sur le client — même reconnaissance
    du receveur que `_methods_called_on_client`, un cran plus bas.

    Deux formes échappent, et c'est dit plutôt qu'implicite : un appel qui déballe
    (`m(**opts)`) ne porte rien de lisible statiquement, et une méthode **confiée**
    (`appeler(geste, ig.get_profile)`) n'a pas de site d'appel ici — c'est
    l'exécuteur qui choisit ses arguments. Prétendre les vérifier vaudrait moins
    que dire qu'on ne les vérifie pas.
    """
    bound = _names_bound_to_client(tree) | {"c"}

    def _est_le_client(node: ast.expr) -> bool:
        if isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Name):
            return node.id in bound
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _CLIENT_FACTORIES)

    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if not _est_le_client(node.func.value):
            continue
        out.setdefault(node.func.attr, set()).update(
            kw.arg for kw in node.keywords if kw.arg)
    return out


def _covered_modules() -> list[tuple[str, str, str, set[str]]]:
    """(module_tool, clsname, import_module, méthodes) pour chaque tool suivant la
    convention `_client() -> ClasseConcrète` avec ≥1 appel `_client().m()`."""
    out = []
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        # La fabrique peut vivre dans un module frère (connecteur découpé) : on
        # y lit alors la classe ET son import.
        cls, arbre_fabrique = _classe_et_arbre_fabrique(tree)
        if not cls:
            continue
        methods = _methods_called_on_client(tree)
        if not methods:
            continue
        mod = _import_of(tree, cls) or _import_of(arbre_fabrique, cls)
        if not mod:
            continue
        out.append((path.stem, cls, mod, methods))
    return out


_CASES = _covered_modules()


def test_convention_coverage_not_silently_shrinking():
    """Filet anti-régression de couverture : si ce nombre chute brutalement (un
    connecteur bascule hors convention), la sonde couvre moins sans le dire."""
    covered = {c[0] for c in _CASES}
    assert "folk" in covered, "folk doit rester couvert (cas d'école du garde-fou)"
    assert "apollo" in covered, (
        "apollo doit rester couvert : `_client() -> tuple[...]` + `client.m()` est "
        "le motif qui échappait à la sonde (trou vécu 2026-07-31)")
    assert {"planity", "planity_stats"} <= covered, (
        "les deux modules de planity doivent rester couverts : leur fabrique est "
        "`async def _client()` — le motif qui sortait de la sonde sans un mot "
        "(2026-09-09). C'est le connecteur qui en a le plus besoin : son cœur "
        "vient d'oto-core, donc d'un autre dépôt et d'un autre rythme de tag.")
    assert "instagram_meta" in covered, (
        "instagram_meta doit rester couvert : ses outils ne chaînent pas l'appel, "
        "ils CONFIENT la méthode à un exécuteur (`appeler(geste, ig.get_profile)`) "
        "— la méthode n'est alors jamais le `func` d'un appel, et le module sortait "
        "entier de la sonde (2026-09-09).")
    assert len(_CASES) >= 20, f"couverture anormalement basse ({len(_CASES)} modules)"


def test_no_module_silently_uncovered():
    """Un module de connecteur qui a un `_client()` mais sort de la portée de la
    sonde doit se VOIR. C'est le trou qui a laissé passer apollo : le docstring
    promettait que les modules hors convention seraient listés, rien ne le faisait,
    donc perdre la couverture d'un connecteur ne coûtait rien."""
    covered = {c[0] for c in _CASES}
    uncovered = []
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        if path.name.startswith("_") or path.stem in covered:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        has_client = (any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and n.name == "_client" for n in ast.walk(tree))
                      or _fabrique_importee(tree) is not None)
        exempt = _NO_CLIENT_EXPECTED | _SUBOBJECT_CLIENTS | _DYNAMIC_DISPATCH_CLIENTS
        if has_client and path.stem not in exempt and not _est_fabrique_partagee(
                path.stem, tree):
            cls, _ = _classe_et_arbre_fabrique(tree)
            why = ("pas de méthode détectée sur le client" if cls
                   else "type de retour de `_client()` non reconnu")
            uncovered.append(f"{path.stem} ({why})")
    assert not uncovered, (
        "modules avec un `_client()` mais SANS couverture version-skew : "
        f"{uncovered} — élargis la sonde (ou déclare-les dans _NO_CLIENT_EXPECTED "
        "si le client ne vient pas d'oto-core).")


@pytest.mark.parametrize("tool_mod, clsname, import_mod, methods",
                         _CASES, ids=[c[0] for c in _CASES])
def test_client_methods_exist_on_pinned_core(tool_mod, clsname, import_mod, methods):
    """Chaque `_client().m()` du tool existe sur la classe oto-core épinglée."""
    try:
        cls = getattr(importlib.import_module(import_mod), clsname)
    except Exception as e:  # noqa: BLE001 — extra non installé, etc.
        pytest.skip(f"{clsname} non importable ({import_mod}) : {e}")
    missing = sorted(m for m in methods if not hasattr(cls, m))
    assert not missing, (
        f"{tool_mod}.py appelle des méthodes absentes de {clsname} "
        f"(oto-core épinglé) : {missing} — bump le pin oto-core dans CETTE PR "
        f"(version-skew, cf. leçon folk_user).")


@pytest.mark.parametrize("tool_mod, clsname, import_mod, methods",
                         _CASES, ids=[c[0] for c in _CASES])
def test_client_kwargs_exist_on_pinned_core(tool_mod, clsname, import_mod, methods):
    """Chaque MOT-CLÉ passé au client existe sur la signature oto-core épinglée.

    Le voisin du dessus vérifie le NOM de la méthode ; ce trou-ci est le même
    défaut d'un cran plus bas et il ne coûte pas moins cher. Un tool mergé en
    avance de phase qui appelle `client.match_person(reveal_phone_number=True)`
    sur un oto-core qui ne connaît pas encore ce paramètre passe la CI (la
    méthode EXISTE) et lève `TypeError: unexpected keyword argument` à la
    première invocation en prod — exactement le scénario `folk_user`, à ceci
    près que la sonde d'à côté le laissait passer.

    Trouvé en ajoutant le reveal de téléphone Apollo, qui est précisément un lot
    « trois paramètres neufs chez oto-core, puis le backend les passe » : la
    seule chose qui protégeait cet ordre était la mémoire de celui qui l'écrit.

    Deux formes échappent, et c'est assumé plutôt qu'implicite : une méthode qui
    déclare `**kwargs` (elle accepte tout — `update_contact(**fields)`), et un
    appel qui déballe un dict. Les deux sont statiquement invérifiables.
    """
    try:
        cls = getattr(importlib.import_module(import_mod), clsname)
    except Exception as e:  # noqa: BLE001 — extra non installé, etc.
        pytest.skip(f"{clsname} non importable ({import_mod}) : {e}")

    tree = ast.parse((_TOOLS_DIR / f"{tool_mod}.py").read_text())
    inconnus = []
    for methode, kwargs in sorted(_kwargs_called_on_client(tree).items()):
        fn = getattr(cls, methode, None)
        if fn is None or not callable(fn):
            continue          # absence de méthode : c'est l'autre test qui parle
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            continue          # `**fields` accepte tout
        inconnus += [f"{methode}({kw}=…)" for kw in sorted(kwargs)
                     if kw not in params]
    assert not inconnus, (
        f"{tool_mod}.py passe des paramètres absents de {clsname} "
        f"(oto-core épinglé) : {inconnus} — bump le pin oto-core dans CETTE PR "
        f"(version-skew de SIGNATURE, pas de nom).")


def test_the_kwarg_probe_bites_and_knows_what_it_cannot_see():
    """La sonde ci-dessus se PROUVE sur l'anomalie qu'elle prétend attraper —
    sinon on ne mesure que sa présence (`docs/conventions.md` : « à sa création,
    prouver qu'il mord »). Vérifié à l'écriture contre le vrai couple : les
    trois kwargs neufs d'`apollo_match_person` remontent face à un oto-core qui
    ne les connaît pas encore, et disparaissent face à celui qui les porte.

    Ici on éprouve l'EXTRACTION, qui ne dépend d'aucun pin : ce qu'elle voit, et
    ce qu'elle admet ne pas voir."""
    tree = ast.parse(
        "def _client() -> ApolloClient:\n"
        "    return ApolloClient()\n"
        "def t():\n"
        "    client, _ = _client()\n"
        "    client.match_person(person_id=p, reveal_phone_number=True)\n"
        "    client.update_contact(cid, **fields)\n")
    vus = _kwargs_called_on_client(tree)
    assert vus["match_person"] == {"person_id", "reveal_phone_number"}
    # Le déballage ne fabrique pas de faux nom : la méthode est vue, ses kwargs
    # non — mieux vaut un trou nommé qu'une vérification qui invente.
    assert vus["update_contact"] == set()
