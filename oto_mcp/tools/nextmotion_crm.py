"""Nextmotion — les leads (prospects), le journal des appels et des messages, et les
réglages d'une clinique.

Module frère de `nextmotion.py` (cf. `Connector.modules`). Trois outils :

- `nextmotion_lead` — list | get | create | update | delete | convert. Un lead EST une
  personne : son identité de contact (nom, prénom, email, téléphone) est servie
  (décision du 2026-10-01), ses notes et sa référence externe (un identifiant chez un
  tiers) s'écrivent mais ne se relisent jamais. Le pipeline : source, statut, canal,
  soin souhaité et zone (étiquettes de la clinique), relances, rendez-vous prévu,
  praticien assigné. Le filtre `search` de l'API (nom, email, téléphone) n'est pas
  exposé. `convert` rend le patient créé par son seul id.
- `nextmotion_setting` — les réglages, `kind` × list | get | create | update | delete
  (+ `duplicate` d'un gabarit de document, `placeholders` des champs de fusion) :
  abonnement Nextmotion (`feature`), moyens de paiement personnalisés, étiquettes,
  gabarits de communication et de documents, modèles de questionnaires (`survey_form` :
  notes BoltNote et consentements de soin — le MODÈLE, jamais une réponse de patient),
  webhooks. Les gabarits et modèles sortent en MÉTADONNÉES (leur corps est un objet
  libre non décrit par la spec) mais s'écrivent entiers ; les `headers` d'un webhook
  s'écrivent et ne sortent jamais (secret du destinataire).
- `nextmotion_communication` — create seul : consigner un appel, ENVOYER un message
  (email, SMS, WhatsApp) tiré d'un gabarit, sur un devis, une facture ou un document
  administratif. Ni le numéro, ni les notes, ni la transcription d'un appel, ni le
  destinataire d'un message ne ressortent.

Toute écriture a `dry_run=True` par défaut et son `data` passe la liste blanche d'entrée
(`nextmotion_entrees`).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .nextmotion_entrees import (_COMMUNICATION_TYPES, _IN_CALL, _IN_COMMUNICATION_RECORD,
                                 _IN_COMMUNICATION_TEMPLATE, _IN_DOCUMENT_TEMPLATE_CREATE,
                                 _IN_DOCUMENT_TEMPLATE_UPDATE, _IN_LEAD,
                                 _IN_PAYMENT_MEDIUM, _IN_SURVEY_FORM_CREATE,
                                 _IN_SURVEY_FORM_UPDATE, _IN_WEBHOOK_CREATE,
                                 _IN_WEBHOOK_UPDATE)
from .nextmotion_garde import (Kind, Write, _bad, _client, _crud, _need, _refuse_ignored,
                               _run, _serve, _serve_write)
from .nextmotion_socle import (_CALL, _COMMUNICATION_RECORD, _COMMUNICATION_TEMPLATE,
                               _DOCUMENT_TEMPLATE, _FEATURE, _LABEL, _LEAD, _LEAD_RETIRE,
                               _NAMED, _PATIENT, _PLACEHOLDERS, _SURVEY_FORM, _WEBHOOK,
                               _WITHHELD, _shape)

_LEADS = {"lead": Kind(
    "leads", _shape(_LEAD), lambda c, cid, **p: c.list_leads(cid, **p),
    lambda c, i: c.get_lead(i), (), _LEAD_RETIRE,
    {**_crud(lambda c, cid, b: c.create_lead(cid, body=b),
             lambda c, i, b: c.update_lead(i, body=b), lambda c, i, b: c.delete_lead(i),
             _IN_LEAD, ("first_name", "last_name")),
     "convert": Write(lambda c, i, b: c.convert_lead_to_patient(i), key="patient",
                      shape=_shape(_PATIENT), withheld=_WITHHELD)})}

_SETTINGS = {
    "feature": Kind(
        "features", _shape(_FEATURE), lambda c, cid, **p: c.list_clinic_features(cid, **p)),
    "payment_medium": Kind(
        "payment_mediums", _shape(_NAMED),
        lambda c, cid, **p: c.list_payment_mediums(cid, **p),
        lambda c, i: c.get_payment_medium(i),
        writes=_crud(lambda c, cid, b: c.create_payment_medium(cid, body=b),
                     lambda c, i, b: c.update_payment_medium(i, body=b),
                     lambda c, i, b: c.delete_payment_medium(i), _IN_PAYMENT_MEDIUM,
                     ("name",))),
    "object_label": Kind(
        "object_labels", _shape(_LABEL),
        lambda c, cid, label_types, **p: c.list_object_labels(cid, types=label_types, **p),
        None, ("label_types",)),
    "communication_template": Kind(
        "communication_templates", _shape(_COMMUNICATION_TEMPLATE),
        lambda c, cid, template_kind, **p: c.list_communication_templates(
            cid, kind=template_kind, **p),
        lambda c, i: c.get_communication_template(i), ("template_kind",),
        writes={"update": Write(
            lambda c, i, b: c.update_communication_template(i, body=b), "item",
            _IN_COMMUNICATION_TEMPLATE, ("template",))}),
    "document_template": Kind(
        "document_templates", _shape(_DOCUMENT_TEMPLATE),
        lambda c, cid, document_type, master_template_id, **p: c.list_document_templates(
            cid, type=document_type, master_id=master_template_id, **p),
        lambda c, i: c.get_document_template(i), ("document_type", "master_template_id"),
        writes={**_crud(lambda c, cid, b: c.create_document_template(cid, body=b),
                        lambda c, i, b: c.update_document_template(i, body=b),
                        lambda c, i, b: c.delete_document_template(i),
                        _IN_DOCUMENT_TEMPLATE_CREATE, ("name", "template", "type"),
                        accepted_update=_IN_DOCUMENT_TEMPLATE_UPDATE,
                        required_update=("name", "template")),
                "duplicate": Write(lambda c, i, b: c.duplicate_document_template(i))}),
    "survey_form": Kind(
        "survey_forms", _shape(_SURVEY_FORM),
        lambda c, cid, search, survey_type, **p: c.list_survey_forms(
            cid, search=search, type=survey_type, **p),
        lambda c, i: c.get_survey_form(i), ("search", "survey_type"),
        writes=_crud(lambda c, cid, b: c.create_survey_form(cid, body=b),
                     lambda c, i, b: c.update_survey_form(i, body=b),
                     lambda c, i, b: c.delete_survey_form(i), _IN_SURVEY_FORM_CREATE,
                     ("type", "name", "fields_tmpl"),
                     accepted_update=_IN_SURVEY_FORM_UPDATE, required_update=())),
    "webhook": Kind(
        "webhooks", _shape(_WEBHOOK), lambda c, cid, **p: c.list_webhooks(cid, **p),
        lambda c, i: c.get_webhook(i),
        writes=_crud(lambda c, cid, b: c.create_webhook(cid, body=b),
                     lambda c, i, b: c.update_webhook(i, body=b),
                     lambda c, i, b: c.delete_webhook(i), _IN_WEBHOOK_CREATE,
                     ("action_type", "url"), accepted_update=_IN_WEBHOOK_UPDATE,
                     required_update=("url",))),
}

#: `op="placeholders"` : le paramètre de type de chaque kind, et sa lecture.
_PLACEHOLDER_READS = {
    "communication_template": (
        "communication_type",
        lambda c, t: c.list_communication_template_placeholders(type=t)),
    "document_template": (
        "document_type", lambda c, t: c.list_document_template_placeholders(type=t)),
    "survey_form": ("survey_type", lambda c, t: c.list_survey_form_placeholders(type=t)),
}
_placeholders = _shape(_PLACEHOLDERS)

_COMMUNICATIONS = {
    "call": Kind(
        "calls", _shape(_CALL),
        withheld=("appel servi sans le numéro, les notes, la transcription, le résumé ni "
                  "l'enregistrement ; patient servi par son seul id."),
        writes={"create": Write(lambda c, cid, b: c.create_call(cid, body=b), "clinic",
                                _IN_CALL)}),
    "message": Kind(
        "messages", _shape(_COMMUNICATION_RECORD),
        withheld="message servi sans son destinataire ni son objet.",
        writes={"create": Write(
            lambda c, cid, b: c.create_communication_record(cid, body=b), "clinic",
            _IN_COMMUNICATION_RECORD,
            ("communication_template_kind", "communication_template_type", "object"))}),
}

_LABEL_TYPES = Literal["patient", "call", "quote_tag", "quote_channel", "lead_status",
                       "lead_source", "lead_channel", "lead_desired_treatment", "lead_zone"]


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def nextmotion_lead(
        op: Literal["list", "get", "create", "update", "delete", "convert"] = "list",
        clinic_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Leads (prospects) of a Nextmotion clinic — contact identity (first and last
        name, email, phone) and the sales pipeline: source, status, channel, desired
        treatment and zone (clinic labels), follow-ups, scheduled appointment,
        assigned practitioner, done flag. Notes and external reference are written,
        never served back. Searching leads by name is not offered.

        `op`: "list" (default, `clinic_id`) | "get" (`lead_id`) | "create" (`clinic_id` +
        `data`) | "update" (`lead_id` + `data`) — `first_name`, `last_name` required;
        labels are ids from `nextmotion_setting(kind="object_label")` | "delete" |
        "convert" (`lead_id`: becomes a patient, answered by id —
        `nextmotion_patient(op="get")` reads it). `data` takes the fields the Nextmotion
        spec accepts for the op; any other field is refused.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act.

        Args:
            op: list (default) | get | create | update | delete | convert.
            clinic_id: op="list"/"create".
            lead_id: every op but list/create.
            data: op="create"/"update" — the lead's fields.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        return _serve(_LEADS, "lead", op, client=_client, clinic_id=clinic_id,
                      item_id=lead_id, filters={}, limit=limit, offset=offset,
                      fields=fields, data=data, dry_run=dry_run, item_name="lead_id")

    @mcp.tool()
    def nextmotion_setting(
        kind: Literal["feature", "payment_medium", "object_label", "communication_template",
                      "document_template", "survey_form", "webhook"],
        op: Literal["list", "get", "create", "update", "delete", "duplicate",
                    "placeholders"] = "list",
        clinic_id: Optional[str] = None,
        item_id: Optional[str] = None,
        label_types: Optional[list[_LABEL_TYPES]] = None,
        template_kind: Optional[Literal["email", "sms", "whatsapp"]] = None,
        document_type: Optional[Literal[0, 1, 2, 3, 4, 6, 7, 8]] = None,
        master_template_id: Optional[str] = None,
        search: Optional[str] = None,
        survey_type: Optional[Literal["bolt_note", "treatment_consent"]] = None,
        communication_type: Optional[str] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Settings and referentials of a Nextmotion clinic — read and write.

        `kind`:
        - "feature" — Nextmotion subscription options; list only.
        - "payment_medium" — custom payment means.
        - "object_label" — lead, quote, call and patient labels; filter `label_types`;
          list only.
        - "communication_template" — email / SMS / WhatsApp templates (metadata);
          filter `template_kind`; update only (`template` required).
        - "document_template" — document templates (metadata); filters `document_type`
          (0 PRESCRIPTION, 1 QUOTE, 2 INVOICE, 3 DEPOSIT_INVOICE, 4 CREDIT_NOTE,
          6 IMAGE_RIGHTS_CONSENT, 7 ADMINISTRATIVE, 8 VISIT), `master_template_id`;
          + "duplicate".
        - "survey_form" — survey-form TEMPLATES (BoltNote notes, treatment consents),
          never a patient's answers; filters `search`, `survey_type`.
        - "webhook" — action type and URL; `headers` are accepted, NEVER returned.
        Bodies of templates and forms are written whole, read as metadata.

        `op`: "list" (default, `clinic_id`) | "get" (`item_id`) | "create" (`clinic_id` +
        `data`) | "update" (`item_id` + `data`) | "delete" / "duplicate" (`item_id`) |
        "placeholders" — merge fields: communication_template (`communication_type`),
        document_template (`document_type`), survey_form (`survey_type`, required).
        `data` takes the fields the Nextmotion spec accepts for the op; any other field is
        refused. Each filter belongs to its kind only.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act. Webhook
        headers are masked in the preview.

        Args:
            kind: which setting.
            op: see above (default "list").
            clinic_id: op="list"/"create".
            item_id: op="get"/"update"/"delete"/"duplicate".
            label_types / template_kind / document_type / master_template_id / search /
                survey_type: list filters (see above).
            communication_type / document_type / survey_type: op="placeholders".
            data: op="create"/"update" — the fields to send.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        filters = {"label_types": label_types, "template_kind": template_kind,
                   "document_type": document_type, "master_template_id": master_template_id,
                   "search": search, "survey_type": survey_type,
                   "communication_type": communication_type}
        if op != "placeholders":
            return _serve(_SETTINGS, kind, op, client=_client, clinic_id=clinic_id,
                          item_id=item_id, filters=filters, limit=limit, offset=offset,
                          fields=fields, data=data, dry_run=dry_run)
        if kind not in _PLACEHOLDER_READS:
            raise _bad("op='placeholders' ne vaut que pour kind='communication_template', "
                       "'document_template' ou 'survey_form'.")
        type_name, read = _PLACEHOLDER_READS[kind]
        _refuse_ignored(op, clinic_id=clinic_id, item_id=item_id, data=data,
                        dry_run=dry_run, limit=limit, offset=offset, fields=fields,
                        **{n: v for n, v in filters.items() if n != type_name})
        if kind == "survey_form":
            _need(op, survey_type=survey_type)
        c = _client()
        env = _run(lambda: read(c, filters[type_name]))
        data_ = env.get("data") if isinstance(env, dict) else None
        rows = data_ if isinstance(data_, list) else [data_] if data_ is not None else []
        return {"kind": kind, "placeholders": [_placeholders(r) for r in rows]}

    @mcp.tool()
    def nextmotion_communication(
        kind: Literal["call", "message"],
        clinic_id: str,
        data: dict,
        op: Literal["create"] = "create",
        dry_run: Optional[bool] = None,
    ) -> dict:
        """Log a call or SEND a message from a Nextmotion clinic — write only.

        `kind`:
        - "call" — logs a call (`patient` id, `phone_number`, `time`, `direction`,
          `duration`, `status` label, `notes`, `summary`, `transcript`…). Served back
          without the number, notes, transcript, summary or recording.
        - "message" — ⚠️ SENDS an email, SMS or WhatsApp to the patient from a clinic
          template: `communication_template_kind` (email | sms | whatsapp),
          `communication_template_type` (quote | quote_checkout | quote_info_documents |
          invoice | administrative_document — never a medical document), `object` (that
          document's id) required.
        `data` takes the fields the Nextmotion spec accepts for the op; any other field is refused.

        ⚠️ `dry_run` DEFAULTS TO TRUE: `data` is checked, what would be sent is returned,
        nothing is logged or sent. `dry_run=False` to act.

        Args:
            kind: call | message.
            clinic_id: the clinic.
            data: the fields to send.
            op: create (the only op).
            dry_run: default True."""
        if kind not in _COMMUNICATIONS:
            raise _bad(f"kind inconnu : {kind!r}.")
        if kind == "message" and isinstance(data, dict):
            kind_type = data.get("communication_template_type")
            if kind_type is not None and kind_type not in _COMMUNICATION_TYPES:
                raise _bad(f"communication_template_type={kind_type!r} n'est pas servi : "
                           f"{', '.join(_COMMUNICATION_TYPES)} seulement (aucun document "
                           "médical ni texte libre de patient).")
        return _serve_write(_COMMUNICATIONS, kind, op, client=_client, clinic_id=clinic_id,
                            item_id=None, data=data, dry_run=dry_run, unused={})
