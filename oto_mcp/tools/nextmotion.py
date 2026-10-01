"""Nextmotion — logiciel de gestion de cliniques de médecine esthétique : tout le côté
ADMINISTRATIF, en lecture ET en écriture (cliniques, praticiens, agenda, catalogue,
ventes, leads, appels et messages, statistiques, stock, réglages), plus l'IDENTITÉ du
patient (`nextmotion_patient`).

Wrappe `oto.tools.nextmotion.NextmotionClient` (Bearer, API « External » v4). keyed
`api_key`, BYO (membre ou org) : une clé agit au nom de l'utilisateur qui l'a générée,
sur les cliniques dont il est employé — il n'y a pas de clé plateforme.

## Données de santé : ce que ce connecteur ne sert PAS

Nextmotion porte des dossiers patients. Tout ce qui est contenu médical — antécédents,
photos et médias, ordonnances et leur signature, consentements signés, soins réalisés,
consultations, visites (notes cliniques), devis et factures créés sous une
consultation — est **hors périmètre**, comme le chat : le client oto-core n'a aucune
méthode vers ces endpoints, et ces modules n'en ajoutent pas. Les ouvrir est une
décision de gouvernance (RGPD art. 9, hébergement HDS), pas une extension de surface.
Ne se suppriment pas non plus : un patient, une facture, un paiement.

**L'identité du patient est servie, par un seul outil** (décision du propriétaire,
2026-10-01) : `nextmotion_patient` lit (liste avec recherche, fiche), crée et modifie
nom, prénom, email, téléphone, date de naissance, âge, genre, adresse, consentements de
contact, numéro de patient, archivé — jamais les commentaires du praticien, la photo ni
les coordonnées GPS. `nextmotion_analyse` lit la même liste pour des agrégats à seuil.

⚠️ **Des ressources du périmètre EMBARQUENT de la donnée personnelle** : rendez-vous,
parcours, devis, factures et paiements portent un objet `patient` complet ; une demande
de rendez-vous en ligne et un lead portent le nom, l'email et le téléphone de la
personne ; beaucoup portent du texte libre. **Tout ce qui sort passe donc par une LISTE
BLANCHE** (`nextmotion_socle`) : seuls les champs nommés passent, un champ que l'API
ajouterait demain reste dehors. **Hors de `nextmotion_patient`, le patient n'est servi
que par son `id`** (qui s'y résout) ; un lead sert son identité de contact (nom, email,
téléphone), jamais ses notes ; la personne d'une demande en ligne pas du tout. **Il
n'existe aucune échappatoire vers le brut** (`fields=["*"]` rend la vue par défaut), et
aucun filtre qui cherche sur le nom d'une personne n'est exposé hors de la liste des
patients.

## Écritures : un aperçu tant qu'on ne dit pas le contraire

Toute écriture (create, update, delete, et les verbes propres : reschedule, validate,
pay, convert…) a **`dry_run=True` par défaut** : l'outil valide les arguments, relit
l'objet visé (projeté) et rend ce qui partirait, sans appeler aucune méthode d'écriture.
**Jamais de notification implicite** : les drapeaux d'envoi que la spec met à `true` par
défaut (modification d'un rendez-vous) partent à `false` sauf demande explicite, et
l'aperçu dit qui serait prévenu. Le corps passe en `data`, validé contre la liste
blanche d'ENTRÉE de l'op (`nextmotion_entrees`, tirée du `requestBody` de la spec) :
**un champ inconnu est refusé nommément**, jamais ignoré. La réponse d'une écriture
repasse par la liste blanche de la ressource. Mécanique commune → `nextmotion_garde`
(`Write`, `_serve_write`).

## Surface (ADR 0047), verbe en `op`, défaut toujours en lecture

Ce module :
- `nextmotion_clinic` — découverte : les cliniques de la clé (seul, sans op).
- `nextmotion_practitioner` — list | get | create | update | delete.
- `nextmotion_appointment` — list | get | update | reschedule | delete.
- `nextmotion_availability` — créneaux libres (params disjoints de l'agenda, d'où
  un tool à part) ; fournit l'`id` et le `time_slot` qu'exigent `reschedule` et une
  demande de rendez-vous en ligne.
- `nextmotion_product` — stock (lots), list | get | create | update | delete, NON
  rattaché aux factures.

Modules frères (même clé, même client, montés par `Connector.modules`) :
`nextmotion_catalogue` (catalogue, forfaits, répartitions comptables, configuration
post-soin), `nextmotion_agenda` (salles, appareils, plages, absences, demandes en
ligne, parcours), `nextmotion_ventes` (devis, factures et avoirs, paiements,
statistiques, totaux d'un patient), `nextmotion_crm` (leads, appels et messages,
réglages, modèles de questionnaires, webhooks), `nextmotion_patient` (l'identité),
`nextmotion_analyse` (patientèle et occupation des appareils, en agrégats).

**Aucun argument n'est retenu au silence** (`is not None`) → `nextmotion_garde`.

Dérivé de la spec OpenAPI publique (lue le 2026-09-17, écritures le 2026-10-01).
**Aucun appel réel** : pas de clé disponible — la forme exacte des réponses, les effets
de bord d'une écriture (notification au patient ?) ne sont pas vérifiés.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify
from .nextmotion_entrees import (_IN_APPOINTMENT, _IN_DOCTOR_CREATE, _IN_DOCTOR_UPDATE,
                                 _IN_PRODUCT_CREATE, _IN_PRODUCT_UPDATE,
                                 _NO_NOTIFY_APPOINTMENT)
from .nextmotion_garde import (_NAME, Kind, Write, _bad, _client, _crud, _need, _paging,
                               _refuse_ignored, _run, _serve_write, _verify)
from .nextmotion_socle import (_CLINIC, _COLLAB, _WITHHELD, _appointment, _one, _page,
                               _product, _shape)

_clinic = _shape(_CLINIC)
_collab = _shape(_COLLAB)
_slot = _shape(("id", "type", "time_slot", "utc_offset"))

_PRACTITIONERS = {"practitioner": Kind(
    "practitioners", _collab, lire=lambda c, i: c.get_doctor(i),
    writes=_crud(lambda c, cid, b: c.create_doctor(cid, body=b),
                 lambda c, i, b: c.update_doctor(i, body=b),
                 lambda c, i, b: c.delete_doctor(i), _IN_DOCTOR_CREATE, ("email", "kind"),
                 accepted_update=_IN_DOCTOR_UPDATE, required_update=("speciality",)))}
_APPOINTMENTS = {"appointment": Kind(
    "appointments", _appointment, lire=lambda c, i: c.get_appointment(i),
    withheld=_WITHHELD,
    writes={"update": Write(lambda c, i, b: c.update_appointment(i, body=b), "item",
                            _IN_APPOINTMENT, ("calendar_event",),
                            defaults=_NO_NOTIFY_APPOINTMENT,
                            notify=tuple(_NO_NOTIFY_APPOINTMENT))})}
_PRODUCTS = {"product": Kind(
    "products", _product, lire=lambda c, i: c.get_product(i),
    writes=_crud(lambda c, cid, b: c.create_product(cid, body=b),
                 lambda c, i, b: c.update_product(i, body=b),
                 lambda c, i, b: c.delete_product(i), _IN_PRODUCT_CREATE,
                 ("global_product",), accepted_update=_IN_PRODUCT_UPDATE,
                 required_update=()))}


def register(mcp: FastMCP) -> None:
    connector_verify.register(_NAME, _verify)

    @mcp.tool()
    def nextmotion_clinic(limit: Optional[int] = None, offset: Optional[int] = None,
                          fields: Optional[list] = None) -> dict:
        """The Nextmotion clinics the API key's user belongs to — start here: every
        other nextmotion tool needs a `clinic_id` from this list.

        Args:
            limit: 1..100 (default 50).
            offset: pagination start (default 0).
            fields: keep only these keys per clinic (`id` always kept).
        """
        c = _client()
        return _page(_run(lambda: c.list_clinics(**_paging(limit, offset))), "clinics",
                     _clinic, fields=fields, withheld=None)

    @mcp.tool()
    def nextmotion_practitioner(
        op: Literal["list", "get", "create", "update", "delete"] = "list",
        clinic_id: Optional[str] = None,
        doctor_id: Optional[str] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Practitioners (doctors, staff) of a Nextmotion clinic — read and write.

        `op`: "list" (default, `clinic_id`) | "get" (`doctor_id`) | "create" (`clinic_id`
        + `data`: `email`, `kind` required; names when the email is unknown) | "update"
        (`doctor_id` + `data`, `speciality` required) | "delete" (`doctor_id`). `data` takes
        the fields the Nextmotion spec accepts for the op; any other field is refused.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act.

        Args:
            op: list (default) | get | create | update | delete.
            clinic_id: op="list"/"create".
            doctor_id: op="get"/"update"/"delete".
            data: op="create"/"update" — the fields to send.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        if op in ("create", "update", "delete"):
            return _serve_write(_PRACTITIONERS, "practitioner", op, client=_client,
                                clinic_id=clinic_id, item_id=doctor_id, data=data,
                                dry_run=dry_run, item_name="doctor_id",
                                unused={"limit": limit, "offset": offset, "fields": fields})
        _refuse_ignored(op, data=data, dry_run=dry_run)
        c = _client()
        if op == "list":
            _need(op, clinic_id=clinic_id)
            _refuse_ignored(op, doctor_id=doctor_id)
            return _page(_run(lambda: c.list_doctors(clinic_id, **_paging(limit, offset))),
                         "practitioners", _collab, fields=fields, withheld=None)
        if op == "get":
            _need(op, doctor_id=doctor_id)
            _refuse_ignored(op, clinic_id=clinic_id, limit=limit, offset=offset,
                            fields=fields)
            return _one(_run(lambda: c.get_doctor(doctor_id)), "practitioner", _collab,
                        withheld=None)
        raise _bad("op doit être 'list', 'get', 'create', 'update' ou 'delete'.")

    @mcp.tool()
    def nextmotion_appointment(
        op: Literal["list", "get", "update", "reschedule", "delete"] = "list",
        clinic_id: Optional[str] = None,
        appointment_id: Optional[str] = None,
        date: Optional[str] = None,
        patient_id: Optional[str] = None,
        visit_type_opening_hour_id: Optional[str] = None,
        time_slot: Optional[str] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Calendar appointments of a Nextmotion clinic — read the agenda, update, move
        or delete an appointment.

        Health data is withheld: schedule, status, visit type, room and practitioners
        stay. The patient is served by ID ONLY — resolve it with
        `nextmotion_patient(op="get")` when needed. No title, notes or anything clinical.

        `op`:
        - "list" (default, `clinic_id`, optional `date` YYYY-MM-DD or `patient_id`) |
          "get" (`appointment_id`).
        - "update" (`appointment_id` + `data`, `calendar_event` with `start_time` /
          `end_time` required; the link to a clinical visit is refused). ⚠️ Nextmotion
          would email/SMS the patient by default: this tool sends
          `send_appointment_modified_email` / `_sms` = false unless you pass true; the
          preview lists who is notified (`notifie_le_patient`). `data` takes the fields the
          Nextmotion spec accepts for the op; any other field is refused.
        - "reschedule" (`appointment_id`, `visit_type_opening_hour_id` + `time_slot` from
          ONE `nextmotion_availability` entry). ⚠️ Touches a real patient.
        - "delete" (`appointment_id`). ⚠️ Touches a real patient; whether Nextmotion
          notifies them is not documented.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act.

        Args:
            op: list (default) | get | update | reschedule | delete.
            clinic_id: op="list".
            appointment_id: every op but list.
            date: op="list" — YYYY-MM-DD.
            patient_id: op="list" — only this patient's appointments.
            visit_type_opening_hour_id: op="reschedule" — slot `id`.
            time_slot: op="reschedule" — slot `time_slot` (date-time).
            data: op="update" — the fields to send.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        if op == "update":
            return _serve_write(
                _APPOINTMENTS, "appointment", op, client=_client, clinic_id=clinic_id,
                item_id=appointment_id, data=data, dry_run=dry_run,
                item_name="appointment_id",
                unused={"date": date, "patient_id": patient_id, "limit": limit,
                        "offset": offset, "fields": fields,
                        "visit_type_opening_hour_id": visit_type_opening_hour_id,
                        "time_slot": time_slot})
        _refuse_ignored(op, data=data)
        c = _client()
        if op == "list":
            _need(op, clinic_id=clinic_id)
            _refuse_ignored(op, appointment_id=appointment_id,
                            visit_type_opening_hour_id=visit_type_opening_hour_id,
                            time_slot=time_slot, dry_run=dry_run)
            return _page(_run(lambda: c.list_appointments(
                clinic_id, date=date, patient_id=patient_id, **_paging(limit, offset))),
                "appointments", _appointment, fields=fields)
        if op not in ("get", "reschedule", "delete"):
            raise _bad("op doit être 'list', 'get', 'update', 'reschedule' ou 'delete'.")
        _refuse_ignored(op, clinic_id=clinic_id, date=date, patient_id=patient_id,
                        limit=limit, offset=offset, fields=fields)
        _need(op, appointment_id=appointment_id)
        if op == "get":
            _refuse_ignored(op, visit_type_opening_hour_id=visit_type_opening_hour_id,
                            time_slot=time_slot, dry_run=dry_run)
            return _one(_run(lambda: c.get_appointment(appointment_id)),
                        "appointment", _appointment)
        if op == "reschedule":
            _need(op, visit_type_opening_hour_id=visit_type_opening_hour_id,
                  time_slot=time_slot)
        else:
            _refuse_ignored(op, visit_type_opening_hour_id=visit_type_opening_hour_id,
                            time_slot=time_slot)
        if dry_run is None or dry_run:
            current = _one(_run(lambda: c.get_appointment(appointment_id)),
                           "appointment", _appointment)
            preview = {"dry_run": True, "would": op, **current,
                       "note": f"Rien n'est écrit. Repasse avec dry_run=False pour {op}."}
            if op == "reschedule":
                preview["to"] = {"visit_type_opening_hour_id": visit_type_opening_hour_id,
                                 "time_slot": time_slot}
            return preview
        if op == "reschedule":
            return _one(_run(lambda: c.reschedule_appointment(
                appointment_id, visit_type_opening_hour_id=visit_type_opening_hour_id,
                time_slot=time_slot)), "appointment", _appointment)
        _run(lambda: c.delete_appointment(appointment_id))
        return {"deleted": True, "appointment_id": appointment_id}

    @mcp.tool()
    def nextmotion_availability(
        clinic_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sub_visit_type_id: Optional[str] = None,
        sub_visit_type_name: Optional[str] = None,
        doctor_id: Optional[str] = None,
        doctor_name: Optional[str] = None,
    ) -> dict:
        """Free appointment slots of a Nextmotion clinic. Each slot's `id` and
        `time_slot` are what `nextmotion_appointment(op="reschedule")` needs.

        Args:
            clinic_id: the clinic.
            start_date / end_date: YYYY-MM-DD; omitted, Nextmotion searches the
                current month.
            sub_visit_type_id / sub_visit_type_name: only slots for this sub visit
                type (name = exact match).
            doctor_id / doctor_name: only slots of this practitioner (name = exact).
        """
        c = _client()
        env = _run(lambda: c.search_time_slots(
            clinic_id, start_date=start_date, end_date=end_date,
            sub_visit_type_id=sub_visit_type_id, sub_visit_type_name=sub_visit_type_name,
            doctor_id=doctor_id, doctor_name=doctor_name))
        return {"slots": [_slot(r) for r in (env or {}).get("data") or []]}

    @mcp.tool()
    def nextmotion_product(
        op: Literal["list", "get", "create", "update", "delete"] = "list",
        clinic_id: Optional[str] = None,
        product_id: Optional[str] = None,
        search: Optional[str] = None,
        stock_state: Optional[Literal["low", "out", "ok"]] = None,
        expiring_within_days: Optional[int] = None,
        order: Optional[Literal["name", "-name", "brand_name", "-brand_name", "stock_level",
                                "-stock_level", "warning_level", "-warning_level"]] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Product stock of a Nextmotion clinic — one row per lot: lot number,
        expiration date, digital and physical stock levels, warning level, unit price,
        catalogue product (`global_product`: name, brand).
        ⚠️ The stock is not linked to invoices, quotes or treatments: the Nextmotion API
        does not expose which consumables or lots an invoice used. Never pair a lot with
        an invoice or a patient.

        `op`: "list" (default, `clinic_id`, filters) | "get" (`product_id`) | "create"
        (`clinic_id` + `data`, `global_product` required) | "update" (`product_id` +
        `data`) | "delete" (`product_id`; answers the deleted lot). `data` takes the fields
        the Nextmotion spec accepts for the op; any other field is refused.
        ⚠️ `dry_run` DEFAULTS TO TRUE on writes.

        Args:
            op: list (default) | get | create | update | delete.
            clinic_id: op="list"/"create".
            product_id: op="get"/"update"/"delete" — the lot.
            search: op="list" — free-text search.
            stock_state: op="list" — low | out | ok.
            expiring_within_days: op="list" — lots expiring within N days (>= 1).
            order: op="list" — sort (default brand_name; `-` = descending).
            data: op="create"/"update" — the fields to send.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        if op in ("create", "update", "delete"):
            return _serve_write(
                _PRODUCTS, "product", op, client=_client, clinic_id=clinic_id,
                item_id=product_id, data=data, dry_run=dry_run, item_name="product_id",
                unused={"search": search, "stock_state": stock_state,
                        "expiring_within_days": expiring_within_days, "order": order,
                        "limit": limit, "offset": offset, "fields": fields})
        _refuse_ignored(op, data=data, dry_run=dry_run)
        c = _client()
        if op == "list":
            _need(op, clinic_id=clinic_id)
            _refuse_ignored(op, product_id=product_id)
            if expiring_within_days is not None and expiring_within_days < 1:
                raise _bad(f"expiring_within_days doit être >= 1 — reçu {expiring_within_days}.")
            return _page(_run(lambda: c.list_products(
                clinic_id, search=search, stock_state=stock_state,
                expiring_within_days=expiring_within_days, order=order,
                **_paging(limit, offset))), "products", _product, fields=fields,
                withheld=None)
        if op == "get":
            _need(op, product_id=product_id)
            _refuse_ignored(op, clinic_id=clinic_id, search=search, stock_state=stock_state,
                            expiring_within_days=expiring_within_days, order=order,
                            limit=limit, offset=offset, fields=fields)
            return _one(_run(lambda: c.get_product(product_id)), "product", _product,
                        withheld=None)
        raise _bad("op doit être 'list', 'get', 'create', 'update' ou 'delete'.")
