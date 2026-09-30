"""Ce que `migrate_sub` a le droit de faire au coffre, et la porte cross-tenant.

Deux invariants nés du passage d'un partenaire en tenant déclaré (oto-private#83) :

1. **Une clé personnelle ne se repointe pas : elle se RECHIFFRE.** L'AAD dérive de
   l'entité (`credentials_store._aad(entity_type, entity_id, connector, account)`),
   donc repointer `entity_id` sans rechiffrer fabrique une ligne que plus rien ne
   peut ouvrir. Jusqu'à #439, la fusion l'ABANDONNAIT donc derrière elle — ce qui la
   rendait invisible à son propriétaire (13 clés membre sur 43 après la bascule du
   13/08). Elle suit désormais la personne, rechiffrée en place
   (`rekey_personal_credentials`), son instance avec elle.

2. **Le merge par email reste borné à un tenant** (ADR 0052 §6). Seul un acte
   d'opérateur nommé ouvre le passage cross-tenant — jamais un login.
"""
import base64
import inspect
import re

import pytest

from oto_mcp import credentials_store as cs
from oto_mcp import crypto
from oto_mcp.db import users


def test_le_coffre_personnel_nest_jamais_repointe_sans_rechiffrement():
    """TRIPWIRE — si quelqu'un « simplifie » le repointage en un `UPDATE entity_id`
    nu, la prod gagne des credentials présents-et-morts (`InvalidTag` à chaque
    appel). Le seul écrivain d'`entity_id` sur ce chemin est le rechiffrement, et il
    réécrit `secret_enc` DANS LE MÊME ordre."""
    for fn in (users.migrate_sub, users.repointer_patrimoine):
        src = inspect.getsource(fn)
        fautifs = [ligne.strip() for ligne in src.splitlines()
                   if "connector_credentials" in ligne and "entity_id" in ligne]
        assert not fautifs, (
            f"{fn.__name__} touche `connector_credentials.entity_id` en direct :\n  "
            + "\n  ".join(fautifs)
            + "\nL'AAD dérive de l'entité — passer par `rekey_personal_credentials`.")
    assert "rekey_personal_credentials" in inspect.getsource(users.repointer_patrimoine)
    src = inspect.getsource(cs.rekey_personal_credentials)
    assert re.search(r"UPDATE connector_credentials SET entity_id = %s, "
                     r"secret_enc = %s", src), (
        "le rechiffrement ne réécrit plus `secret_enc` avec `entity_id` : la ligne "
        "déplacée serait indéchiffrable")


def test_lauteur_du_coffre_est_bien_repointe():
    """`set_by` n'entre PAS dans l'AAD, c'est de l'attribution : il se repointe par
    l'inventaire, comme toute colonne d'auteur."""
    assert ("connector_credentials", "set_by") in users._SUB_COLUMNS, (
        "migrate_sub ne repointe plus l'auteur des credentials : l'attribution "
        "d'une clé d'org survivrait à son poseur en pointant un sub supprimé.")


# ── le rechiffrement, exercé sur le VRAI chiffrement ─────────────────────────

_KEY = base64.b64encode(bytes(range(32))).decode()


class _Res:
    def __init__(self, rows=None, one=None, rowcount=0):
        self._rows, self._one, self.rowcount = rows or [], one, rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _Coffre:
    """Une table `connector_credentials` + `connector_instances` en mémoire, qui ne
    comprend QUE les requêtes du rechiffrement — un SQL imprévu lève."""

    def __init__(self, lignes, instances):
        self.lignes = {(l["entity_type"], l["entity_id"], l["connector"], l["account"]): l
                       for l in lignes}
        self.instances = instances   # {id: (owner_type, owner_id, connector, account)}

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT entity_type, entity_id, connector, account, secret_enc"):
            user, old, member, old2 = params
            rows = [dict(l) for k, l in sorted(self.lignes.items())
                    if (k[0] == user and k[1] == old)
                    or (k[0] == member and k[1].split(":", 1)[0].isdigit()
                        and k[1].split(":", 1)[1] == old2)]
            return _Res(rows)
        if sql.startswith("SELECT 1 FROM connector_credentials"):
            et, eid, con, acc = params[:4]
            pris = (et, eid, con, acc) in self.lignes or (et, eid, con, acc) in \
                self.instances.values()
            return _Res(one={"?column?": 1} if pris else None)
        if sql.startswith("UPDATE connector_credentials SET entity_id"):
            nouveau, enc, et, ancien, con, acc = params
            ligne = self.lignes.pop((et, ancien, con, acc))
            self.lignes[(et, nouveau, con, acc)] = {**ligne, "entity_id": nouveau,
                                                    "secret_enc": enc}
            return _Res(rowcount=1)
        if sql.startswith("UPDATE connector_instances SET owner_id"):
            nouveau, ot, ancien, con, acc = params
            for i, q in self.instances.items():
                if q == (ot, ancien, con, acc):
                    self.instances[i] = (ot, nouveau, con, acc)
                    return _Res(one={"id": i})
            return _Res()
        raise AssertionError(f"SQL imprévu : {sql}")


def _ligne(et, eid, con, secret, acc=""):
    return {"entity_type": et, "entity_id": eid, "connector": con, "account": acc,
            "secret_enc": crypto.encrypt(secret, cs._aad(et, eid, con, acc))}


@pytest.fixture
def cle(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", _KEY)


def test_une_cle_membre_suit_la_personne_et_se_dechiffre_sous_son_nouveau_nom(cle):
    """Le symptôme de #439 : la clé restait sous `12:ancien`, la résolution la
    cherchait sous `12:nouveau`. Après la fusion elle est sous le nouveau nom, elle
    s'ouvre avec l'AAD du nouveau nom, et son instance l'a suivie avec son id."""
    coffre = _Coffre([_ligne("member", "12:ancien", "apollo", "k-apollo"),
                      _ligne("member", "12:quelquun", "apollo", "pas-a-lui")],
                     {7: ("member", "12:ancien", "apollo", "")})
    bilan = cs.rekey_personal_credentials(coffre, "ancien", "nouveau")
    assert bilan == {"rekeyed": 1, "collisions": 0, "illisibles": 0}
    ligne = coffre.lignes[("member", "12:nouveau", "apollo", "")]
    assert crypto.decrypt(ligne["secret_enc"],
                          cs._aad("member", "12:nouveau", "apollo")) == "k-apollo"
    assert ("member", "12:ancien", "apollo", "") not in coffre.lignes
    assert coffre.instances[7] == ("member", "12:nouveau", "apollo", "")
    assert ("member", "12:quelquun", "apollo", "") in coffre.lignes


def test_un_sub_qui_en_suffixe_un_autre_ne_se_confond_pas(cle):
    """`entity_id` d'une clé membre = `org:sub`, et un sub qualifié contient des `:`.
    Un filtre par SUFFIXE (`LIKE '%:ancien'`) prendrait la clé de `tenant:x:ancien`."""
    coffre = _Coffre([_ligne("member", "12:tenant:x:ancien", "apollo", "autre")], {})
    assert cs.rekey_personal_credentials(coffre, "ancien", "nouveau")["rekeyed"] == 0
    assert ("member", "12:tenant:x:ancien", "apollo", "") in coffre.lignes


def test_une_collision_garde_la_cle_du_compte_canonique(cle):
    """Le compte canonique a reposé la même clé : c'est celle qu'il sert — on ne
    l'écrase pas, et l'ancienne n'est pas supprimée (elle reste, comptée)."""
    coffre = _Coffre([_ligne("member", "12:ancien", "apollo", "vieille"),
                      _ligne("member", "12:nouveau", "apollo", "reposee")], {})
    bilan = cs.rekey_personal_credentials(coffre, "ancien", "nouveau")
    assert bilan == {"rekeyed": 0, "collisions": 1, "illisibles": 0}
    assert crypto.decrypt(coffre.lignes[("member", "12:nouveau", "apollo", "")]["secret_enc"],
                          cs._aad("member", "12:nouveau", "apollo")) == "reposee"
    assert ("member", "12:ancien", "apollo", "") in coffre.lignes


def test_une_ligne_illisible_reste_en_place_et_se_compte(cle, monkeypatch):
    """Clé maîtresse périmée : rechiffrer est impossible, déplacer rendrait pire.
    La ligne reste, le bilan le dit — jamais un succès muet."""
    coffre = _Coffre([_ligne("member", "12:ancien", "apollo", "x")], {})
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", base64.b64encode(b"\x11" * 32).decode())
    bilan = cs.rekey_personal_credentials(coffre, "ancien", "nouveau")
    assert bilan == {"rekeyed": 0, "collisions": 0, "illisibles": 1}
    assert ("member", "12:ancien", "apollo", "") in coffre.lignes


def test_le_cross_tenant_exige_un_acte_doperateur_nomme():
    """La garde `same_tenant` ne doit jamais disparaître : elle doit s'OUVRIR sur
    une source explicite. Un login (`reconcile_tenant_migration`) ne la renseigne
    pas, donc reste fermé — c'est ce qui empêche qu'une inscription sous l'email
    d'autrui, chez un tenant tiers, absorbe son compte."""
    sig = inspect.signature(users.migrate_sub)
    assert "operator_source" in sig.parameters, (
        "la porte cross-tenant délibérée a disparu de migrate_sub")
    assert sig.parameters["operator_source"].kind is inspect.Parameter.KEYWORD_ONLY, (
        "`operator_source` doit être keyword-only : un troisième argument positionnel "
        "s'attraperait par mégarde à l'appel.")
    assert sig.parameters["operator_source"].default == "", (
        "le défaut doit être vide — fermé par défaut, ouvert seulement si on le nomme")

    src = inspect.getsource(users.migrate_sub)
    assert "same_tenant(old_sub, new_sub) and not operator_source" in src, (
        "la garde de tenant ne s'ouvre plus sur la source d'opérateur, ou ne garde "
        "plus du tout : les deux sont des régressions distinctes.")


def test_le_login_nouvre_jamais_la_porte():
    """`reconcile_tenant_migration` est le chemin CHAUD (à chaque login du nouveau
    tenant). S'il passait `operator_source`, la fédération d'identités que §6
    interdit redeviendrait automatique — sans qu'aucun test de forme ne bronche."""
    src = inspect.getsource(users.reconcile_tenant_migration)
    assert "operator_source" not in src, (
        "reconcile_tenant_migration nomme `operator_source` : le merge automatique "
        "par email franchirait les tenants (ADR 0052 §6).")
