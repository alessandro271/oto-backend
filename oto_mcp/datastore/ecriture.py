"""ÉCRIRE une ligne : l'ajouter, la fusionner, la remplacer, la patcher, l'effacer.

Extrait de `core.py` (déplacement pur, 07/09/2026) — un mixin que `DatastorePg`
compose, sur le modèle de `SchemaOpsMixin`. Le LOT vit à côté (`lots.py`) : les deux
chemins d'écriture ont déjà divergé une fois sur une famille de règles (#322), ils
partagent donc les fonctions — `_check_row`, `arbitrer_les_vides`,
`refuser_champs_reserves` — pas seulement l'intention.

⚠️ Ce module LIT des attributs de colonne et de tableau (`key`, `schema`, `data`…) :
il est listé dans `vocabulaire._read_keys`.
"""
from __future__ import annotations

from typing import Optional

from psycopg.errors import UniqueViolation

from .. import db
from . import acces_agent as aga
from . import schema as dsv2
from .columns import (
    _META_COLS,
    _merge_column,
    _refuse_mixed_layers,
    arbitrer_les_vides,
    refuser_geste_sans_effet,
)
from .controles import _relever_origine_module
from .errors import NamespaceNotFound, RowNotFound
from .forcage import Forcage
from .outils import _new_id, _now_iso, _refus_de_creation
from .points import _refuse_dotted_names, ranger_les_couches
from .donnees_d_origine import poser_les_deux_versions
from .reserves import poser_origine_systeme, refuser_champs_reserves


class EcritureMixin:
    """Les écritures unitaires du store. Composé par `DatastorePg`."""

    # --- row ops -------------------------------------------------------------

    def append_row(self, namespace: str, data: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False) -> dict:
        """Écrit UNE row. Si le namespace déclare une clé métier (`schema.key`),
        applique la MÊME dédup upsert que le batch `write_rows` : une row de même
        valeur de clé est MERGÉE (pas de doublon, l'index `ds_bkey_<ns>` la refuse) ;
        sinon append. Renvoie la row (nouvelle ou mise à jour).

        ⚠️ Sur un tableau qui déclare `key_required` (#516), l'append n'existe plus :
        une écriture qui ne désigne aucune ligne existante est REFUSÉE
        (`BusinessKeyRequired`) au lieu d'en créer une.

        `trace` (dict mutable, optionnel) = relevé pour le journal, cf. `_trace`.
        `readonly_override` (#658) = forcer les colonnes verrouillées de CET appel,
        sous palier — cf. `_forcage_readonly`."""
        if isinstance(data, dict) and "_id" in data:
            # PROMOTION (#354, amende le refus #390) : `_id` dans `row` EST
            # l'adresse de la ligne — réécrire la ligne telle que
            # `data_claim_next`/`data_rows` l'a servie devient le geste juste,
            # symétrique du claim. Garde-fou indissociable : un `_id` qui ne
            # matche AUCUNE ligne rend une erreur nommée, jamais une création —
            # sinon la promotion re-fabrique le fantôme par une porte de côté.
            cible = str(data["_id"])
            reste = {k: v for k, v in data.items() if k != "_id"}
            try:
                return self.update_row(namespace, cible, reste, trace=trace,
                                       readonly_override=readonly_override,
                                       donnees_d_origine=donnees_d_origine)
            except RowNotFound:
                raise ValueError(
                    f"`_id` ({cible!r}) ne correspond à aucune ligne de "
                    f"`{namespace}` — rien n'est créé. L'identifiant est peut-être "
                    "tronqué ou la ligne purgée : relis-la (data_rows, "
                    "data_claim_next) et réécris avec son `_id` exact.")
        ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        # CAS 1 avant le refus : une fiche relue et réémise entière porte
        # `site_web` ET `site_web.comment`, et c'est notre propre lecture. On range
        # l'annotation à sa place AVANT de juger quoi que ce soit — sinon les gardes
        # qui suivent (champs réservés, schéma) jugeraient une adresse au lieu d'une
        # colonne, et le geste dominant d'un agent se ferait refuser.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: self._colonnes_de_la_ligne_visee(
                ns_id, schema, user_data))
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        # #586 : la couche d'origine d'un champ système ne s'écrit pas, création
        # comprise — jugée sur le payload seul (le readonly, lui, se juge contre la
        # ligne en place, donc dans la fusion). Refusé AVANT le lookup de clé.
        # #658 : tranché AVANT la fusion — c'est elle qui ouvre le verrou de ligne.
        forcage = self._forcage_readonly(ns_id, schema, readonly_override)
        refuser_champs_reserves(schema, user_data, agent=aga.appel_d_agent())
        _relever_origine_module(self, ns_id, user_data, schema=schema,
                                declare=origine_override)
        self._trace(trace, ns_id, ns)
        # La clé métier sort du MÊME schéma que ci-dessus (`declared_key` re-résolvait
        # le namespace et relisait la ligne pour le même résultat).
        key = self._declared_key_of(schema)
        kv = user_data.get(key) if key else None
        if key and kv is not None and str(kv) != "":
            existing_id = db.datastore_find_row_id_by_key(ns_id, key, kv)
            if existing_id is not None:
                return self._row_to_dict(
                    self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                         forcage=forcage,
                                         origine_override=origine_override,
                                         donnees_d_origine=donnees_d_origine),
                    schema)
        # #516 : sur un tableau FERMÉ, on ne crée pas — on vise. Le geste est arrivé
        # jusqu'ici sans désigner de ligne : ni par son `_id` (promu plus haut, et
        # refusé s'il ne matche rien), ni par une valeur de clé que le tableau porte.
        # Refuser AVANT `_check_row` : la validation de schéma parlerait des champs
        # d'une ligne qui ne doit pas naître.
        if dsv2.key_required_of(schema):
            raise _refus_de_creation(ns.get("namespace") or namespace, key, kv)
        # #390 (3ᵉ demande) : une ligne CRÉÉE sans la clé métier déclarée est non
        # rapprochable — aucune écriture ultérieure ne la retrouvera par sa clé, et
        # le batch qui dédouble passera à côté. C'est la forme résiduelle de
        # l'incident : une 501ᵉ ligne sans SIREN née avec tout l'enrichissement,
        # sans une erreur. Les deux autres portes (adresse égarée dans `row`, `id`
        # nu) sont désormais fermées ; celle-ci n'a pas d'adresse du tout, donc rien
        # à refuser — on NOMME, comme `hors_schema`. Mesuré avant de la poser :
        # 197 tableaux à clé déclarée, 50 024 lignes, 3 sans clé. Elle ne parlera
        # quasiment jamais, et c'est ce qui la rendra lisible.
        if key and (kv is None or str(kv) == ""):
            self.off_notices.add(
                f"ligne créée SANS `{key}`, la clé métier de ce tableau : elle ne "
                f"sera rapprochée par personne — ni une réécriture, ni un lot qui "
                f"dédouble sur cette clé. Si elle visait une ligne existante, c'est "
                f"data_write(id=…) ; sinon renseigne `{key}`.")
        # ⚠️ QUATRIÈME chemin, et celui que j'avais oublié — trouvé par le banc, pas
        # par relecture. Les trois autres (lot, fusion, patch par `id`) étaient
        # branchés ; la création unitaire, non. C'est exactement le défaut que ce
        # fichier dénonce ailleurs sur la même famille de règles : une garde posée sur
        # les chemins auxquels on pense, absente de celui qu'on croyait couvert parce
        # qu'il ressemble aux autres.
        if donnees_d_origine:
            poser_les_deux_versions(user_data)
        self._check_row(schema, user_data)
        try:
            row = db.datastore_insert_row(ns_id, _new_id(), user_data)
        except UniqueViolation:
            # Course perdue sous l'index UNIQUE de clé métier (#109 ch.3) : un write
            # concurrent a inséré la même clé entre le lookup et l'insert — le doublon
            # que la contrainte empêche. On converge en merge (même chemin que le batch).
            existing_id = (db.datastore_find_row_id_by_key(ns_id, key, kv)
                           if key and kv is not None else None)
            if existing_id is None:
                raise  # violation inexpliquée → erreur franche, pas de repli muet
            return self._row_to_dict(
                self._merge_into_row(ns_id, existing_id, user_data, schema=schema,
                                     forcage=forcage,
                                     origine_override=origine_override),
                schema)
        return self._row_to_dict(row, schema)

    def _merge_into_row(self, ns_id: int, row_id: str, user_data: dict,
                        *, schema: Optional[dict] = None,
                        forcage: Optional[Forcage] = None,
                        origine_override: bool = False,
                        donnees_d_origine: bool = False) -> dict:
        """MERGE `user_data` dans la row existante (dernier écrit gagne par champ),
        en appliquant le schéma v2 (ADR 0046) au résultat mergé : validation avec
        `prev_status` (transition de lifecycle) puis release du claim si l'état
        devient terminal. Renvoie la row brute persistée. Corps commun à l'append
        unitaire et au batch.

        Le read-merge-write est ATOMIQUE (verrou de ligne, #197) : le get + le
        merge + l'update tournent dans une seule transaction `FOR UPDATE`, sinon
        deux writes concurrents de la même clé (même row_id) s'écrasaient
        mutuellement (last-writer-wins) et perdaient des champs silencieusement."""
        if schema is None:
            schema = self._schema_of(ns_id)
        # La ligne visée est connue ICI : ses colonnes comptent pour « colonne réelle »,
        # ce qui rend `{"site_web.comment": …}` seul écrivable sur un tableau souple.
        # Lue paresseusement — le chemin nominal ne la demande jamais.
        user_data = ranger_les_couches(
            schema, user_data,
            colonnes_en_place=lambda: set(
                (db.datastore_get_row(ns_id, row_id) or {}).get("data") or {}))
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        sk = (dsv2.status_field(schema) or {}).get("key")

        def _apply(current: dict) -> dict:
            merged = dict(current or {})
            prev_status = merged.get(sk) if sk else None
            # Arbitrage AVANT la fusion : après, l'ancienne valeur n'existe plus
            # nulle part. Il rend d'un coup ce que l'écriture pose VRAIMENT (les
            # vides non-`null` qui auraient déplacé une valeur en sont retirés,
            # #608) et les deux relevés. Posés sur le store seulement une fois la
            # validation passée — un refus n'a rien effacé, l'annoncer ferait
            # chercher un dégât imaginaire.
            # `donnees_d_origine` : l'appel apporte la donnée telle qu'elle a été
            # REMISE. On fige sa version d'origine AVANT l'arbitrage des vides, pour
            # que ce qui est écarté le soit sur la forme définitive. Sous le verrou de
            # ligne, donc `current` est la ligne vraie — indispensable ici : c'est LUI
            # qui dit si une origine est déjà posée, et une origine posée ne se
            # réécrit jamais. Muter en place est sans risque, le geste est idempotent.
            if donnees_d_origine:
                poser_les_deux_versions(user_data, avant=current)
            pose, vidages, ecartes = arbitrer_les_vides(current, user_data, row_id)
            # #724 : préserver et le DIRE ne suffit pas quand l'écarté était TOUT ce
            # que l'écriture portait — l'appel n'a alors aucun effet et répond 200.
            # ⚠️ Par CE chemin le refus ne peut pas parler : on n'arrive ici (append
            # promu, lot) qu'avec une valeur de clé métier non vide, donc posée — ce
            # qui garantit qu'un LOT ne casse jamais dessus. Il y est quand même :
            # les deux chemins d'écriture ont déjà divergé une fois sur cette famille
            # de règles (#322), ils partagent la fonction, pas seulement l'intention.
            refuser_geste_sans_effet(pose, ecartes)
            # Colonne par colonne, pour que l'origine survive à une écriture
            # ordinaire. Un `update` en bloc l'emporterait avec le reste — et
            # silencieusement, puisque remplacer une valeur est le geste normal.
            # ⚠️ Le champ DÉCLARÉ passe avec la valeur : sans lui, une liste qui
            # nomme l'identité de ses éléments (`of.key`) se remplacerait quand même
            # en bloc, et la déclaration serait une clé de plus que rien ne lit.
            for _k, _v in pose.items():
                merged[_k] = _merge_column(merged.get(_k), _v,
                                           dsv2.champ_declare(schema, _k))
            # #586/#606 : ce que l'appelant n'écrit pas — jugé sur le geste ENTIER
            # (payload, ligne en place, résultat), sous le verrou, avant que quoi
            # que ce soit ne parte. Puis la plateforme pose l'origine qu'elle doit.
            refuser_champs_reserves(schema, pose, avant=current or {},
                                    forcage=forcage, agent=aga.appel_d_agent())
            _relever_origine_module(self, ns_id, pose, current or {}, schema=schema,
                                    declare=origine_override)
            poser_origine_systeme(schema, current, merged, set(pose))
            # ⚠️ `written` reste l'ensemble des clés que l'appelant a NOMMÉES, pas
            # celles qu'on a retenues : une borne de longueur ou un motif ne doit pas
            # se réarmer sur une colonne préservée, dont la valeur n'a pas bougé.
            self._check_row(schema, merged, prev_status=prev_status,
                            written=set(pose))
            self.off_erased.extend(vidages)
            self.off_ignored.extend(ecartes)
            return merged

        result = db.datastore_merge_row_locked(ns_id, row_id, _apply, _now_iso(),
                                               lease_guard=self._lease_guard(row_id))
        if result is None:
            raise RowNotFound(row_id)  # supprimée entre le lookup et le verrou (course)
        row, merged = result
        # #658 : après le verrou — un forçage n'est journalisé que s'il a ABOUTI.
        self._relever_forcage(forcage, row_id)
        self._terminal_write_notice(schema, ns_id, row_id, merged)
        return row

    def upsert_row(self, namespace: str, row_id: str, data: dict, *,
                   origine_override: bool = False) -> tuple[dict, bool]:
        """Écrit une row à une clé `row_id` EXPLICITE (≠ append_row qui génère un
        id), en remplaçant si elle existe. Crée le namespace au besoin. Sert le
        stockage dédupliqué par clé stable (ex. urn LinkedIn). Renvoie
        `(row, inserted)` — `inserted` False = la row existait déjà."""
        self._reject_misplaced_id(data, row_id)
        try:
            ns_id = self._resolve(namespace, write=True)
        except NamespaceNotFound:
            _ot, _oid = self._default_owner()
            db.create_datastore_namespace(_ot, _oid, namespace)
            self._active_scope_cache = None  # invalide le cache (le ns créé appartient à la PERSONNE (ADR 0068), pas à l'org active)
            ns_id = self._resolve(namespace, write=True)
        user_data = {k: v for k, v in data.items() if k not in _META_COLS}
        schema = self._schema_of(ns_id)
        # ⚠️ Pas de `colonnes_en_place` ici, et c'est délibéré : l'upsert REMPLACE la
        # ligne. Ranger une annotation sur une colonne qui n'est que dans l'ancienne
        # ligne poserait une couche sur une valeur qui tombe dans le même geste.
        user_data = ranger_les_couches(schema, user_data)
        _refuse_dotted_names(user_data)
        _refuse_mixed_layers(schema, user_data)
        valide = dsv2.validation_active(schema) or dsv2.lifecycle_of(schema)
        reserves = bool(dsv2.readonly_fields(schema)
                        or dsv2.system_origin_fields(schema))
        prev = db.datastore_get_row(ns_id, row_id) if (valide or reserves) else None
        prev_data = dict((prev or {}).get("data") or {}) if prev else None
        if reserves:
            # #586/#606 sur un REMPLACEMENT : une colonne readonly absente du corps
            # serait perdue par le remplacement — c'est une modification, jugée
            # comme telle (le payload est complété des colonnes qui tomberaient).
            complet = {**{k: None for k in (prev_data or {}) if k not in user_data},
                       **user_data}
            refuser_champs_reserves(schema, complet, avant=prev_data,
                                    agent=aga.appel_d_agent())
            _relever_origine_module(self, ns_id, complet, prev_data, schema=schema,
                                    declare=origine_override)
            if prev_data is not None:
                poser_origine_systeme(schema, prev_data, user_data, set(complet))
        if valide:
            sk = (dsv2.status_field(schema) or {}).get("key")
            prev_status = (prev_data or {}).get(sk) if sk else None
            self._check_row(schema, user_data, prev_status=prev_status)
        self._assert_writable(ns_id, row_id)
        row, inserted = db.datastore_upsert_row(ns_id, row_id, user_data)
        if not inserted:
            self._terminal_write_notice(schema, ns_id, row_id, user_data)
        return self._row_to_dict(row, schema), inserted

    def declared_key(self, namespace: str) -> Optional[str]:
        """Clé métier déclarée au schéma (`schema.key`) — sert la dédup au batch
        write. None si aucune (table libre / schéma sans clé).

        Lit le schéma SERVI, et c'est sans conséquence (oto#83) : le masquage ne touche
        jamais la clé métier — `acces_agent._cles_par_acces` l'écarte quoi qu'en dise la
        déclaration, précisément pour qu'aucune décision interne ne dépende du point de
        vue de l'appelant."""
        return self._declared_key_of(self.get_schema(namespace))

    def write_rows(self, namespace: str, rows: list, *, key: Optional[str] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False) -> dict:
        """Écrit un LOT de rows en un appel. Si une clé métier est en vigueur (param
        `key` explicite, sinon `schema.key` déclarée), chaque row qui la porte fait un
        UPSERT (merge) sur la row existante de même valeur de clé — pas de doublon ;
        sinon append d'une nouvelle row. Renvoie un récap {inserted, updated, count,
        key, ids}. Résout le namespace UNE fois (write) pour tout le lot."""
        ns_id = self._resolve(namespace, write=True)
        return self._write_rows_to_ns(ns_id, rows, key=key or self.declared_key(namespace),
                                      readonly_override=readonly_override,
                                      origine_override=origine_override,
                                      donnees_d_origine=donnees_d_origine)

    def update_row(self, namespace: str, row_id: str, patch: dict, *,
                   trace: Optional[dict] = None,
                   readonly_override: bool = False,
                   origine_override: bool = False,
                   donnees_d_origine: bool = False) -> dict:
        """Patch partiel d'une row. `trace` (dict mutable, optionnel) = relevé pour
        le journal — dont l'état AVANT, celui-là même sur lequel la transition de
        cycle de vie est validée juste en dessous (cf. `_trace`).

        `readonly_override` (#658) = forcer les colonnes verrouillées de CET appel,
        sous palier — cf. `_forcage_readonly`."""
        self._reject_misplaced_id(patch, row_id)
        ns_id = self._resolve(namespace, write=True)
        existing = db.datastore_get_row(ns_id, row_id)
        if not existing:
            raise RowNotFound(row_id)
        data = dict(existing.get("data") or {})
        ns = self._ns_of(ns_id)
        schema = ns.get("schema")
        # La ligne est déjà lue : ses colonnes sont « réelles » sans un aller-retour de
        # plus. C'est la porte du round-trip #390 — relire une fiche et la repousser —
        # donc celle où l'aller-retour DOIT se refermer.
        patch = ranger_les_couches(schema, patch, colonnes_en_place=lambda: set(data))
        _refuse_dotted_names(patch)
        _refuse_mixed_layers(schema, patch)
        status_key = (dsv2.status_field(schema) or {}).get("key")
        prev_status = data.get(status_key) if status_key else None
        self._trace(trace, ns_id, ns, prev_status=prev_status)
        # MÊME arbitrage que la fusion : le patch par `id` est le geste qui a vidé
        # `moteur` en production le 13/08 — et il l'a fait en le NOMMANT (#407/#408/
        # #409). Fait avant la boucle, sur l'état lu en base. Les deux chemins
        # d'écriture ont déjà divergé une fois sur cette famille de règles (#322) :
        # ils partagent donc la fonction, pas seulement l'intention.
        # ⚠️ TROISIÈME branchement de ce chemin sur la même famille de règles, et le
        # commentaire vingt lignes plus bas dit pourquoi : `update_row` a déjà été
        # oublié DEUX fois — une fois pour la survie de l'origine, une fois pour son
        # relevé — parce qu'il ne passe pas par `_merge_into_row`. Le geste le plus
        # courant d'un agent est aussi celui qu'on oublie, précisément parce qu'il a
        # son propre corps. `avant` = l'état lu : c'est lui qui dit si une origine est
        # déjà posée, et une origine posée ne se réécrit jamais.
        if donnees_d_origine:
            poser_les_deux_versions(patch, avant=data)
        pose, vidages, ecartes = arbitrer_les_vides(data, patch, row_id)
        # #724 : le patch par `id` est le chemin des dix retraits perdus du 01/09 —
        # un vide SEUL y était accepté sans effet, et le relevé qui nommait déjà la
        # porte n'a pas été lu. Refusé AVANT tout relevé : rien n'a été touché, il
        # n'y a donc rien à annoncer — le message, lui, écrit la porte en toutes
        # lettres, au moment où l'appelant peut encore corriger.
        refuser_geste_sans_effet(pose, ecartes)
        avant = dict(data)
        written = set()
        for k, v in pose.items():
            if k in _META_COLS:
                continue
            # MÊME fusion que le batch : l'origine survit ici aussi. Elle avait été
            # câblée dans `_merge_into_row` seulement — donc un patch par `id`, le
            # geste le plus courant d'un agent, l'effaçait quand même.
            data[k] = _merge_column(data.get(k), v, dsv2.champ_declare(schema, k))
            written.add(k)
        # #586/#606 : MÊME garde que la fusion — le patch par `id` est le geste le
        # plus courant d'un agent, et celui qui a écrasé les quatorze valeurs.
        forcage = self._forcage_readonly(ns_id, schema, readonly_override)
        refuser_champs_reserves(schema, pose, avant=avant,
                                forcage=forcage, agent=aga.appel_d_agent())
        # ⚠️ CE chemin-ci a déjà été oublié une fois, six lignes plus haut : l'origine
        # n'avait été câblée que dans `_merge_into_row`, et le patch par `id` — le
        # geste le plus courant d'un agent — l'effaçait quand même. Le barreau 1 a
        # refait la MÊME omission sur le relevé : quatre chemins branchés, celui-ci
        # non. Un instrument qui ne voit pas le geste le plus courant sous-compte
        # exactement la population qu'il existe pour trouver.
        _relever_origine_module(self, ns_id, pose, avant, schema=schema,
                                declare=origine_override)
        poser_origine_systeme(schema, avant, data, written)
        # Validation sur le RÉSULTAT mergé (un patch partiel ne doit pas échouer
        # sur un requis déjà présent) + transition de cycle de vie (ADR 0046 B/C).
        # Seule la borne de longueur se limite aux clés du patch (#383).
        self._check_row(schema, data, prev_status=prev_status, written=written)
        self.off_erased.extend(vidages)
        self.off_ignored.extend(ecartes)
        try:
            self._assert_writable(ns_id, row_id)
            row = db.datastore_update_row(ns_id, row_id, data, _now_iso())
        except UniqueViolation:
            # Un AUTRE enregistrement porte déjà cette valeur de clé métier (index
            # UNIQUE ds_bkey_<ns_id>). Contrairement au batch write (qui converge en
            # merge sur la row de même clé), un update ciblé sur `row_id` ne peut pas
            # basculer silencieusement sur une autre row → erreur actionnable
            # (ValueError → INVALID_PARAMS), jamais un 500 opaque.
            dk = (schema or {}).get("key")
            dkv = data.get(dk) if dk else None
            if dk and dkv is not None:
                raise ValueError(
                    f"un autre enregistrement porte déjà {dk}={dkv} "
                    "(clé métier unique) — impossible de dupliquer") from None
            raise  # violation inexpliquée → erreur franche, pas de repli muet
        # #658 : après l'UPDATE — un forçage n'est journalisé que s'il a ABOUTI.
        self._relever_forcage(forcage, row_id)
        self._terminal_write_notice(schema, ns_id, row_id, data)
        return self._row_to_dict(row, schema)

    def delete_row(self, namespace: str, row_id: str, *,
                   trace: Optional[dict] = None) -> None:
        ns_id = self._resolve(namespace, write=True)
        if trace is not None:
            # Relevé demandé : on lit l'état de la row DANS le chemin de suppression
            # (au plus près du delete), jamais par un `get_row` séparé côté route —
            # qui re-résoudrait le namespace et courrait avec un write concurrent.
            ns = self._ns_of(ns_id)
            sk = (dsv2.status_field(ns.get("schema")) or {}).get("key")
            prev = ((db.datastore_get_row(ns_id, row_id) or {}).get("data") or {}) if sk else {}
            self._trace(trace, ns_id, ns, prev_status=prev.get(sk) if sk else None)
        self._assert_writable(ns_id, row_id)
        if not db.datastore_delete_row(ns_id, row_id):
            raise RowNotFound(row_id)
