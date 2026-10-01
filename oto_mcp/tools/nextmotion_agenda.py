"""Nextmotion — l'organisation de l'agenda : salles, appareils, plages d'ouverture,
absences, demandes de rendez-vous en ligne, et parcours patient du jour.

Module frère de `nextmotion.py` (cf. `Connector.modules`). Les rendez-vous eux-mêmes
et les créneaux libres restent dans `nextmotion.py` (`nextmotion_appointment` porte
leurs écritures). Deux outils :

- `nextmotion_calendar` — les ressources et réglages de l'agenda, `kind` × list | get |
  create | update | delete : tous partagent clinique + identifiant, chaque kind a au
  plus ses filtres (ADR 0047). Les écritures ont `dry_run=True` par défaut et leur
  `data` passe la liste blanche d'entrée (`nextmotion_entrees`) ; une demande en ligne
  se crée (pour une personne, nommée dans `data`) mais ne se modifie ni ne s'efface.
- `nextmotion_journey` — le parcours d'un patient sur un rendez-vous (étapes
  requises / faites) : une liste seule, à douze filtres qui ne recouvrent rien des
  autres, d'où un outil à part.

⚠️ Ce qui est retiré, en plus de la règle commune (`nextmotion_socle`) :
- d'une **demande en ligne** : la personne qui la fait (nom, prénom, email,
  téléphone, date de naissance, âge, sexe), son lien et son message de prépaiement ;
- d'un **évènement** (plage, absence, parcours) : titre, sous-titre, notes, statut
  de séance, textes des SMS / WhatsApp de rappel ;
- d'un **parcours** : sa consultation (un objet médical), et le filtre `search` de
  l'API, qui cherche sur le NOM du patient — comme le tri par nom.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .nextmotion_entrees import (_IN_ABSENCE, _IN_APPOINTMENT_REQUEST, _IN_DEVICE,
                                 _IN_OPENING_HOUR, _IN_ROOM)
from .nextmotion_garde import Kind, Write, _client, _crud, _need, _paging, _run, _serve
from .nextmotion_socle import (_ABSENCE, _APPOINTMENT_REQUEST, _DEVICE, _JOURNEY,
                               _OPENING_HOUR, _PERSONNE, _ROOM, _TEXTES, _page, _shape)

_EVENT = ("calendar_event",)
_KINDS = {
    "room": Kind(
        "rooms", _shape(_ROOM), lambda c, cid, **p: c.list_appointment_rooms(cid, **p),
        lambda c, i: c.get_appointment_room(i),
        writes=_crud(lambda c, cid, b: c.create_appointment_room(cid, body=b),
                     lambda c, i, b: c.update_appointment_room(i, body=b),
                     lambda c, i, b: c.delete_appointment_room(i), _IN_ROOM, ("name",))),
    "device": Kind(
        "devices", _shape(_DEVICE),
        lambda c, cid, **p: c.list_appointment_devices(cid, **p),
        lambda c, i: c.get_appointment_device(i),
        writes=_crud(lambda c, cid, b: c.create_appointment_device(cid, body=b),
                     lambda c, i, b: c.update_appointment_device(i, body=b),
                     lambda c, i, b: c.delete_appointment_device(i), _IN_DEVICE,
                     ("name",))),
    "opening_hour": Kind(
        "opening_hours", _shape(_OPENING_HOUR),
        lambda c, cid, show_all, **p: c.list_calendar_opening_hours(
            cid, show_all=show_all, **p),
        lambda c, i: c.get_calendar_opening_hour(i), ("show_all",), _TEXTES,
        writes=_crud(lambda c, cid, b: c.create_calendar_opening_hour(cid, body=b),
                     lambda c, i, b: c.update_calendar_opening_hour(i, body=b),
                     lambda c, i, b: c.delete_calendar_opening_hour(i), _IN_OPENING_HOUR,
                     _EVENT, required_update=())),
    "absence": Kind(
        "absences", _shape(_ABSENCE),
        lambda c, cid, start_date, end_date, show_all, **p: c.list_calendar_absences(
            cid, start_date=start_date, end_date=end_date, show_all=show_all, **p),
        lambda c, i: c.get_calendar_absence(i), ("start_date", "end_date", "show_all"),
        _TEXTES,
        writes=_crud(lambda c, cid, b: c.create_calendar_absence(cid, body=b),
                     lambda c, i, b: c.update_calendar_absence(i, body=b),
                     lambda c, i, b: c.delete_calendar_absence(i), _IN_ABSENCE, _EVENT,
                     required_update=())),
    "appointment_request": Kind(
        "appointment_requests", _shape(_APPOINTMENT_REQUEST),
        lambda c, cid, request_status, **p: c.list_appointment_requests(
            cid, status=request_status, **p),
        lambda c, i: c.get_appointment_request(i), ("request_status",), _PERSONNE,
        writes={"create": Write(
            lambda c, _, b: c.create_appointment_request(body=b), "none",
            _IN_APPOINTMENT_REQUEST,
            ("visit_type_opening_hour", "time_slot", "email", "first_name", "last_name",
             "birth_date", "phone_number"))}),
}

_journey = _shape(_JOURNEY)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def nextmotion_calendar(
        kind: Literal["room", "device", "opening_hour", "absence", "appointment_request"],
        op: Literal["list", "get", "create", "update", "delete"] = "list",
        clinic_id: Optional[str] = None,
        item_id: Optional[str] = None,
        show_all: Optional[bool] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        request_status: Optional[Literal["new", "pending_pre_payment", "accepted",
                                         "rejected"]] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """How a Nextmotion clinic's calendar is organised — rooms, devices, opening
        hours, absences — and online appointment requests; read and write.
        Appointments: `nextmotion_appointment`; free slots: `nextmotion_availability`.

        `kind`: "room" | "device" | "opening_hour" (filter `show_all`) | "absence"
        (filters `start_date`, `end_date`, `show_all`) | "appointment_request" (filter
        `request_status`; the requesting person is NOT served back). Event titles,
        notes and reminder texts are withheld.

        `op`: "list" (default, `clinic_id`) | "get" (`item_id`) | "create" (`clinic_id`
        + `data`) | "update" (`item_id` + `data`) | "delete" (`item_id`).
        appointment_request: create only, no `clinic_id`; `data` = a slot from
        `nextmotion_availability` (`visit_type_opening_hour`, `time_slot`) and the
        person (`first_name`, `last_name`, `email`, `phone_number`, `birth_date`).
        Required: room/device `name`; opening_hour/absence `calendar_event`
        (`start_time`, `end_time`, …) on create. `data` takes the fields the Nextmotion spec
        accepts for the op; any other field is refused.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act.

        Args:
            kind: which calendar object.
            op: see above (default "list").
            clinic_id: op="list"/"create".
            item_id: op="get"/"update"/"delete".
            show_all: opening_hour/absence list — the whole clinic, not only the key user.
            start_date / end_date: absence list — YYYY-MM-DD.
            request_status: appointment_request list — new | pending_pre_payment |
                accepted | rejected.
            data: create/update — the fields to send.
            dry_run: writes — default True.
            limit / offset: list — pagination (limit 1..100, default 50).
            fields: list — keep only these keys per row (`id` kept; `["*"]` = default)."""
        filters = {"show_all": show_all, "start_date": start_date, "end_date": end_date,
                   "request_status": request_status}
        return _serve(_KINDS, kind, op, client=_client, clinic_id=clinic_id,
                      item_id=item_id, filters=filters, limit=limit, offset=offset,
                      fields=fields, data=data, dry_run=dry_run)

    @mcp.tool()
    def nextmotion_journey(
        clinic_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        include_ongoing: Optional[bool] = None,
        status: Optional[Literal["not_started", "in_progress", "finished"]] = None,
        doctor_ids: Optional[list[str]] = None,
        visit_type_ids: Optional[list[str]] = None,
        sub_visit_type_ids: Optional[list[str]] = None,
        patient_id: Optional[str] = None,
        order: Optional[Literal["start_time", "-start_time"]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Patient journeys of a Nextmotion clinic — for each appointment, the steps
        required and completed, visit type, room and schedule. Read only.

        The patient is served by their ID ONLY — no name, email or phone; resolve it
        with `nextmotion_patient(op="get")` when the identity is needed. The
        consultation, event titles and notes are withheld; searching or sorting by
        patient name is not offered.

        Args:
            clinic_id: the clinic.
            start_date / end_date: ISO 8601 date (YYYY-MM-DD) or date-time.
            include_ongoing: include journeys overlapping a bound (Nextmotion
                default: true).
            status: not_started | in_progress | finished.
            doctor_ids / visit_type_ids / sub_visit_type_ids: only these (UUID lists).
            patient_id: only this patient's journeys.
            order: start_time | -start_time.
            limit / offset: pagination (limit 1..100, default 50).
            fields: keep only these keys per row (`id` always kept); omitted or
                `["*"]` = the default view.
        """
        _need("list", clinic_id=clinic_id)
        c = _client()
        return _page(_run(lambda: c.list_calendar_journeys(
            clinic_id, start_date=start_date, end_date=end_date,
            include_ongoing=include_ongoing, status=status, doctor_ids=doctor_ids,
            visit_type_ids=visit_type_ids, sub_visit_type_ids=sub_visit_type_ids,
            patient_id=patient_id, order=order, **_paging(limit, offset))),
            "journeys", _journey, fields=fields)
