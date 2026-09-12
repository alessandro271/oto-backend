"""Capacités de gestion d'email d'une org, PAR CONNECTEUR (ADR 0009).

L'org_admin déclare, **par connecteur email** (`scaleway` = hébergé Otomata ; `resend`
= BYOK), les adresses expéditrices que `email_send` peut utiliser + une fenêtre calme.
Le **transport dérive du connecteur** (plus de champ transport sur l'expéditeur). La
clé Resend, elle, se pose dans le coffre (`oto_set_org_secret(provider="resend")`).
Lecture = membre ; écriture = org_admin.

Modèle calqué sur `orgs/field_filters.py` : get global (toutes les configs keyées par
connecteur) + set par connecteur. Une déclaration → MCP `oto_*` + REST
`/api/orgs/{id}/email-settings[/{connector}]`.
"""
from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ... import org_store, providers
from ...scheduler import DEFAULT_QUIET_HOURS
from .._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from .._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from ..registry import CAPABILITIES

_ID = {"id": "org_id"}
_ID_CONNECTOR = {"id": "org_id", "connector": "connector"}


class EmailSettingsView(BaseModel):
    """Config d'envoi d'email de l'org, keyée PAR CONNECTEUR.

    ⚠️ **Un connecteur sans `quiet_hours` n'est pas « sans fenêtre calme »** : le
    défaut plateforme (`quiet_hours_default`, 20h→8h Europe/Paris) s'applique quand
    même. Une org qui n'a jamais rien réglé voit donc ses mails composés la nuit
    DIFFÉRÉS au matin — silence de config ≠ envoi immédiat.

    ⚠️ `settings: {}` = aucun expéditeur déclaré ⟹ `email_send` sans `from_email` n'a
    rien à utiliser et échoue. Ce n'est pas « on prendra un défaut ».

    `connectors` et `transports` portent la même information (les clés de l'un sont
    la liste de l'autre) — redondance de compat front, pas deux catalogues.
    `resend_key_set` dit qu'une clé Resend est POSÉE au coffre, jamais qu'elle est
    valide (aucun appel n'est fait ici).

    `settings.<connector>.footer` = le désabonnement PROPRE de l'org sur ce connecteur
    (`{unsubscribe_url?, unsubscribe_email?}`). Présent, les envois de ce connecteur
    portent le pied de l'org à la place de celui de la plateforme ; absent, le nôtre
    reste. Il ne vaut que pour CE connecteur."""
    org_id: int
    # {"<connector>": {"senders": [{email, name?, reply_to?}], "quiet_hours"?: {...}}}
    settings: dict
    connectors: list[str]
    transports: dict
    # Fenêtre appliquée à défaut, PAR le serveur — pas une suggestion d'UI.
    quiet_hours_default: dict
    resend_key_set: bool


class EmailSettingsSet(BaseModel):
    """Écho **partiel** de l'écriture : c'est un MERGE par connecteur, et seules les
    clés effectivement touchées par cet appel reviennent.

    ⚠️ `senders`/`count` **absents** = tu n'as pas envoyé `senders` ; ça ne dit RIEN
    du nombre d'expéditeurs de l'org. Idem pour `quiet_hours`.

    ⚠️ `quiet_hours: null` (via `clear_quiet_hours=true`) ne veut pas dire « plus de
    fenêtre calme » mais « retour au défaut PLATEFORME » (20h→8h) — le seul moyen
    d'envoyer la nuit reste `send_at`/`force_now` côté `email_send`.

    `senders` renvoyé est la liste NETTOYÉE (adresses strippées, `name`/`reply_to`
    vides retirés) et elle **remplace** celle du connecteur, elle ne s'y ajoute pas."""
    ok: bool
    org_id: int
    connector: str
    senders: Optional[list[dict]] = None
    count: Optional[int] = None
    quiet_hours: Optional[dict] = None
    # Le pied de l'org tel que STOCKÉ (nettoyé) ; `null` via `clear_footer=true` = le
    # pied de la plateforme revient sur ce connecteur. Absent = non touché.
    footer: Optional[dict] = None


class GetEmailSettingsInput(BaseModel):
    org_id: int


class SetEmailSettingsInput(BaseModel):
    org_id: int
    connector: str                           # "scaleway" | "resend"
    senders: Optional[list[dict]] = None     # [{email, name?, reply_to?}] — SANS transport
    quiet_hours: Optional[dict] = None       # {tz, start, end} — fenêtre d'envoi interdite
    clear_quiet_hours: bool = False          # True = efface la fenêtre du connecteur
    footer: Optional[dict] = None            # {unsubscribe_url?, unsubscribe_email?} — remplace NOTRE pied
    clear_footer: bool = False               # True = retire le pied de l'org, le nôtre revient


# Le geste à nommer quand on demande le retrait sans fournir le moyen : pas un
# « interdit », mais ce qu'il faut déclarer pour l'obtenir.
_PIED_SANS_DESABONNEMENT = (
    "Retirer le pied de page de la plateforme exige de déclarer TON propre désabonnement "
    "sur ce connecteur : passe `footer` avec `unsubscribe_url` (un lien https://) et/ou "
    "`unsubscribe_email` (l'adresse qui reçoit les demandes de désinscription). Tant "
    "qu'il n'est pas déclaré, le pied de page de la plateforme reste sur tes envois.")


def _validate_senders(senders: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for s in senders:
        email = (s.get("email") or "").strip()
        if not email or "@" not in email:
            raise AuthzDenied(400, "bad_sender_email",
                              f"Adresse expéditrice invalide : {email!r}.")
        key = email.lower()
        if key in seen:
            raise AuthzDenied(400, "duplicate_sender", f"Adresse en double : {email}.")
        seen.add(key)
        clean: dict = {"email": email}
        if s.get("name"):
            clean["name"] = str(s["name"]).strip()
        if s.get("reply_to"):
            clean["reply_to"] = str(s["reply_to"]).strip()
        out.append(clean)
    return out


def _validate_quiet_hours(qh: dict) -> dict:
    tz = (qh.get("tz") or DEFAULT_QUIET_HOURS["tz"]).strip()
    try:
        ZoneInfo(tz)
    except Exception:
        raise AuthzDenied(400, "bad_tz", f"Fuseau horaire inconnu : {tz!r} (ex. Europe/Paris).")
    try:
        start, end = int(qh["start"]), int(qh["end"])
    except (KeyError, TypeError, ValueError):
        raise AuthzDenied(400, "bad_quiet_hours", "`start` et `end` (heures 0..23) requis.")
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise AuthzDenied(400, "bad_quiet_hours", "`start`/`end` doivent être dans 0..23.")
    if start == end:
        raise AuthzDenied(400, "bad_quiet_hours", "`start` et `end` doivent différer.")
    return {"tz": tz, "start": start, "end": end}


def _validate_footer(footer: dict) -> dict:
    """Le pied de l'org remplace le nôtre SI ET SEULEMENT SI il porte un moyen de se
    désabonner — décision d'Alexis du 12/09/2026, pour une raison de conformité : un
    prospect à qui une org écrit avec SA clé doit pouvoir refuser auprès d'elle, pas
    auprès de nous qu'il ne connaît pas. C'est le contrat de la fonction d'envoi, pas
    une garde devant un outil : aucun autre refus ici que ce que ce contrat exige.

    - ni lien ni adresse → refus qui NOMME le geste (`_PIED_SANS_DESABONNEMENT`) ;
    - un lien non `https://` → refus : le gabarit le refuserait à l'envoi
      (`email_brand._lien_desinscription`), mieux vaut le dire à la déclaration ;
    - une adresse sans `@` ou avec un blanc → refus, même règle que les expéditeurs."""
    url = str(footer.get("unsubscribe_url") or "").strip()
    adresse = str(footer.get("unsubscribe_email") or "").strip()
    if not url and not adresse:
        raise AuthzDenied(400, "unsubscribe_required", _PIED_SANS_DESABONNEMENT)
    if url and not url.startswith("https://"):
        raise AuthzDenied(400, "bad_unsubscribe_url",
                          f"`footer.unsubscribe_url` doit commencer par https:// (reçu "
                          f"{url[:24]!r}) : un lien de désabonnement en clair est bloqué "
                          "ou marqué non sûr par les clients mail.")
    if adresse and ("@" not in adresse or any(c.isspace() for c in adresse)):
        raise AuthzDenied(400, "bad_unsubscribe_email",
                          f"`footer.unsubscribe_email` invalide : {adresse!r}.")
    clean: dict = {}
    if url:
        clean["unsubscribe_url"] = url
    if adresse:
        clean["unsubscribe_email"] = adresse
    return clean


def _get_email_settings(ctx: ResolvedCtx, inp: GetEmailSettingsInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    return {
        "org_id": inp.org_id,
        "settings": org_store.get_org_email_settings(inp.org_id),   # keyé par connecteur
        "connectors": list(providers.EMAIL_CONNECTOR_TRANSPORT),
        "transports": dict(providers.EMAIL_CONNECTOR_TRANSPORT),
        "quiet_hours_default": DEFAULT_QUIET_HOURS,
        "resend_key_set": org_store.has_org_secret(inp.org_id, "resend"),
    }


def _set_email_settings(ctx: ResolvedCtx, inp: SetEmailSettingsInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    connector = (inp.connector or "").strip()
    if connector not in providers.EMAIL_CONNECTOR_TRANSPORT:
        raise AuthzDenied(400, "unknown_email_connector",
                          f"Connecteur email inconnu : {connector!r} "
                          f"(attendu {list(providers.EMAIL_CONNECTOR_TRANSPORT)}).")
    if inp.clear_quiet_hours and inp.quiet_hours is not None:
        raise AuthzDenied(400, "bad_quiet_hours",
                          "`quiet_hours` et `clear_quiet_hours` sont exclusifs.")
    if inp.clear_footer and inp.footer is not None:
        raise AuthzDenied(400, "bad_footer", "`footer` et `clear_footer` sont exclusifs.")
    if (inp.senders is None and inp.quiet_hours is None and not inp.clear_quiet_hours
            and inp.footer is None and not inp.clear_footer):
        raise AuthzDenied(400, "nothing_to_set",
                          "Fournis `senders`, `quiet_hours`, `clear_quiet_hours`, `footer` "
                          "ou `clear_footer`.")
    # Tout se valide AVANT la première écriture : un pied refusé n'écrit pas non plus
    # les expéditeurs passés dans le même appel.
    senders = _validate_senders(inp.senders) if inp.senders is not None else None
    quiet = _validate_quiet_hours(inp.quiet_hours) if inp.quiet_hours is not None else None
    pied = _validate_footer(inp.footer) if inp.footer is not None else None
    if senders is not None or quiet is not None or inp.clear_quiet_hours:
        org_store.set_org_email_settings(inp.org_id, connector, senders=senders,
                                         quiet_hours=quiet,
                                         clear_quiet_hours=inp.clear_quiet_hours)
    if pied is not None or inp.clear_footer:
        org_store.set_org_email_footer(inp.org_id, connector, pied)
    out: dict = {"ok": True, "org_id": inp.org_id, "connector": connector}
    if senders is not None:
        out["senders"] = senders
        out["count"] = len(senders)
    if quiet is not None:
        out["quiet_hours"] = quiet
    if inp.clear_quiet_hours:
        out["quiet_hours"] = None
    if pied is not None:
        out["footer"] = pied
    if inp.clear_footer:
        out["footer"] = None
    return out


CAPABILITIES += [
    Capability(
        key="org.email_settings.get", handler=_get_email_settings, Input=GetEmailSettingsInput,
        authz=ORG_MEMBER_OF("org_id"), Output=EmailSettingsView,
        description=("Read the org's email config keyed by connector (scaleway = Otomata-"
                     "hosted, resend = BYOK): per-connector senders + quiet hours, the known "
                     "email connectors, connector→transport map, and whether the org's Resend "
                     "key is set. `settings.<connector>.footer` = the org's own unsubscribe "
                     "on that connector (present = its sends carry the org's footer instead "
                     "of the platform's)."),
        rest=RestBinding("GET", "/api/orgs/{id}/email-settings", _ID),
    ),
    Capability(
        key="org.email_settings.set", handler=_set_email_settings, Input=SetEmailSettingsInput,
        authz=ORG_ADMIN_OF("org_id"), Output=EmailSettingsSet,
        description=("Set ONE email connector's config for `email_send`. `connector` ∈ "
                     "{scaleway (Otomata-hosted via Scaleway TEM — domain verified + in the "
                     "service allowlist), resend (BYOK — set the org's Resend key via "
                     "oto_set_org_secret provider=resend; domain verified on Resend)}; the "
                     "transport is DERIVED from the connector. `senders` = [{email, name?, "
                     "reply_to?}] (no transport) — replaces this connector's list; the first "
                     "sender across connectors is the default when `email_send` omits "
                     "`from_email`. `quiet_hours` = {tz, start, end} (hours 0..23, wrap-around "
                     "midnight ok): emails composed inside the window are auto-deferred to the "
                     "next `end`. `clear_quiet_hours=true` removes this connector's window. "
                     "`footer` = {unsubscribe_url? (https), unsubscribe_email?} — the org's OWN "
                     "unsubscribe: once declared, sends through this connector (the org's own "
                     "key) carry the org's footer INSTEAD of the platform's; asking for it "
                     "without either field is refused, and the platform footer stays. "
                     "`clear_footer=true` brings the platform footer back. Sends under the "
                     "platform brand always keep the platform footer. Pass any field (merge)."),
        rest=RestBinding("PUT", "/api/orgs/{id}/email-settings/{connector}", _ID_CONNECTOR),
    ),
]
