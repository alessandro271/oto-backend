"""File de travail (ADR 0046 D) — un STATUT sans état TERMINAL ne libère aucun bail.

Signal #360 : un vivier drainé par 4 workers déclarait `role="status"` + `options`,
mais pas de `lifecycle` ; l'écriture du verdict n'a donc rien libéré et 149 lignes
traitées sont restées réservées. Le substrat faisait ce qui est écrit — c'est le
SILENCE de la configuration qui coûtait. On garde ici le fait qu'il parle."""
from __future__ import annotations

from oto_mcp.datastore import core as D
from oto_mcp.datastore import schema as dsv2

_STATUS = {"key": "statut", "role": "status", "type": "enum",
           "options": ["a_enrichir", "enrichi", "echec"]}


def _with_lifecycle(**lc):
    return {"fields": [{**_STATUS, "lifecycle": lc}]}


def test_une_colonne_qui_RESSEMBLE_a_un_etat_sans_lifecycle_avertit():
    """⚠️ Le fait a changé le 08/09/2026, le danger non.

    La colonne d'état est désormais CELLE QUI PORTE le `lifecycle` : une colonne qui
    n'en porte pas n'est pas un état, et le tableau n'a donc pas de file du tout —
    `claim_next` y rend `{}`, sans ligne ni raison.

    **C'est pire que l'incident #360, pas mieux** : là-bas la file existait et ne
    libérait rien ; ici elle n'existe pas et ne le dit pas. L'avertissement porte donc
    sur ce qui est vrai maintenant — une colonne avec ses options, ou l'ancienne
    étiquette, RESSEMBLE à un état sans en être un."""
    w = dsv2.queue_release_warning({"fields": [_STATUS]})

    assert w and "statut" in w
    assert "n'a PAS de file de travail" in w
    assert "sans rien dire" in w, "le silence de `claim_next` doit être nommé"
    assert "c'est le bloc `lifecycle` qui fait l'état" in w


def test_lifecycle_without_derivable_terminal_warns():
    # tout état a une transition sortante ⇒ ensemble terminal dérivé VIDE
    schema = _with_lifecycle(states=["a", "b"], transitions={"a": ["b"], "b": ["a"]})
    assert dsv2.terminal_states(schema) == set()
    w = dsv2.queue_release_warning(schema)
    assert w and "AUCUN bail" in w and "data_release" in w, (
        "l'incident #360 lui-même : la file EXISTE et ne libère rien")


def test_explicit_terminal_is_silent():
    schema = _with_lifecycle(states=["a_enrichir", "enrichi"], terminal=["enrichi"])
    assert dsv2.queue_release_warning(schema) is None


def test_derived_terminal_is_silent():
    schema = _with_lifecycle(states=["a", "fini"], transitions={"a": ["fini"]})
    assert dsv2.queue_release_warning(schema) is None


def test_no_status_field_is_silent():
    # table libre ou schéma sans statut : la file ne la concerne pas
    assert dsv2.queue_release_warning({"fields": [{"key": "nom", "type": "text"}]}) is None
    assert dsv2.queue_release_warning(None) is None


def test_set_schema_returns_the_warning(monkeypatch):
    """L'auteur du schéma l'apprend au moment où il le pose — les DEUX faces, le
    retour de `set_schema` étant servi tel quel par le tool MCP et la route REST."""
    monkeypatch.setattr(D.db, "set_datastore_schema", lambda ns_id, schema: None)
    monkeypatch.setattr(D.db, "datastore_drop_key_index", lambda ns_id: None)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    # `set_schema` relit le schéma en place avant de le remplacer (#388).
    monkeypatch.setattr(s, "_ns_of", lambda ns_id: {})

    out = s.set_schema("vivier", {"fields": [_STATUS]})
    assert "warning" in out
    out = s.set_schema("vivier", _with_lifecycle(states=["a", "fini"], terminal=["fini"]))
    assert "warning" not in out


def test_claim_next_warns_the_worker(monkeypatch):
    """Le worker qui claim est celui que ça concerne : sans terminal, c'est à lui
    d'appeler data_release. Rien n'est signalé quand la file est vide (pas de row)."""
    row = {"row_id": "r1", "created_at": "c", "updated_at": "u",
           "data": {"statut": "a_enrichir"}, "claimed_by": "w-1", "claimed_until": "t",
           "claimed_run": None, "claim_active": True}
    schema = {"fields": [_STATUS]}
    monkeypatch.setattr(D.db, "datastore_claim_next",
                        lambda ns_id, **kw: row if kw.get("filters") is not None else None)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    # Une seule lecture de la ligne namespace sert le warning ET le relevé de journal
    # (`_after_claim`) — d'où le stub à ce grain, pas sur le schéma seul.
    monkeypatch.setattr(s, "_ns_of", lambda ns_id: {"namespace": "vivier", "schema": schema})

    warnings: list = []
    assert s.claim_next("vivier", worker="w-1", warnings=warnings)["_id"] == "r1"
    # Le worker reçoit l'avertissement du schéma tel qu'il est : ici la colonne
    # ressemble à un état sans en être un, donc le tableau n'a pas de file — et
    # `claim_next` le lui dit au lieu de le laisser conclure de son silence.
    assert len(warnings) == 1 and "n'a PAS de file de travail" in warnings[0]

    # schéma sain ⇒ silence ; et `warnings` reste optionnel (appelants historiques)
    monkeypatch.setattr(s, "_ns_of", lambda ns_id: {
        "namespace": "vivier",
        "schema": _with_lifecycle(states=["a_enrichir", "enrichi"], terminal=["enrichi"])})
    warnings = []
    s.claim_next("vivier", worker="w-1", warnings=warnings)
    assert warnings == []
    assert s.claim_next("vivier", worker="w-1")["_id"] == "r1"


def test_claim_next_silent_when_queue_is_empty(monkeypatch):
    monkeypatch.setattr(D.db, "datastore_claim_next", lambda ns_id, **kw: None)
    s = D.DatastorePg("u1")
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False: 7)
    monkeypatch.setattr(s, "_ns_of", lambda ns_id: {"namespace": "vivier",
                                                    "schema": {"fields": [_STATUS]}})

    warnings: list = []
    assert s.claim_next("vivier", worker="w-1", warnings=warnings) is None
    assert warnings == []  # rien à traiter ⇒ rien à dire
