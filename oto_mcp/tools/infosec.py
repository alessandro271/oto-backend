"""Infosec — digital footprint of a domain (**passive** recon / OSINT).

MCP envelope of `oto.tools.infosec.InfosecClient` (oto-core): whois (RDAP), DNS, email
posture (SPF/DMARC/DKIM…), DNS blocklists and the scored deliverability report,
subdomains (Certificate Transparency), TLS and HTTP security headers. Passive only:
RDAP, DNS-over-HTTPS, public CT logs, one TLS handshake, one HTTP GET — no port scan,
no vulnerability probing.

The checks, their bounds (one DNS probe per check, global deadline, a read past it named
in the coverage, never an absence) and the domain normalisation live in the lib. Here,
the host's share only: the User-Agent of the probes (`_ua`, this instance's contact
address, #968), a local refusal (`ValueError`: unknown op, invalid domain or IP) turned
into `INVALID_PARAMS`. An upstream refusal (`UpstreamHTTPError`) is left to the error
envelope.

**Every outbound connection goes through the egress guard, as an AGENT URL**
(`_Garde`): the domain and the TLS port are chosen in a conversation, so no named
exception applies, redirects included. The HTTP checks (`headers`, `whois`,
`subdomains`) go out through a guarded session (`egress_transport.session`, holder
`egress.AGENT`); the TLS handshake resolves the host once, judges those addresses
(`egress.decide`) and connects to one of them, the name kept for SNI and certificate
verification. The DNS checks go to the lib's fixed DNS-over-HTTPS resolver only.

Open-data connector: no credential, no key. Exposed only if enabled in the DB
(activation gate, ADR 0010) — `register_all` gates on `connector_activation`.

**Consolidated surface (ADR 0047 §Amendment)**: one business object (the domain), its
facets picked by `op`.
"""
from __future__ import annotations

import socket
import ssl
from functools import lru_cache
from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS
from oto.tools.infosec import InfosecClient

from .. import config, egress, egress_transport
from ..mcp_errors import McpError

_CONNECTEUR = "infosec"
_TIMEOUT_S = 15
_REDIRECTIONS = 5


def _ua() -> str:
    """User-Agent of the probes: the contact address is that of THIS instance, never
    ours hard-coded (#968) — resolved at call time, `public_base_url` raises without it."""
    return f"oto-infosec/1.0 (+{config.public_base_url()})"


def _session():
    """The session every HTTP check goes out through: pinned at each connection,
    redirects included, with no named exception (`egress.AGENT`)."""
    s = egress_transport.session(connector=_CONNECTEUR, field="domain",
                                 titulaire=egress.AGENT)
    s.max_redirects = _REDIRECTIONS
    return s


class _Garde:
    """The lib's checks that leave the DNS, rewired through the egress guard. Placed
    BEFORE the lib's class (`_client`): its methods win, the rest is the lib's."""

    def whois(self, domain: str) -> dict:
        from oto.tools.common.errors import raise_for_upstream
        from oto.tools.infosec.client import _vcard_field

        with _session() as s:
            r = s.get(self.RDAP_URL.format(domain=domain), timeout=20,
                      headers={"user-agent": self.user_agent})
        if r.status_code == 404:
            return {"domain": domain, "found": False,
                    "note": "not registered or TLD not covered by RDAP"}
        raise_for_upstream(r, service="rdap")
        j = r.json()
        events = {e.get("eventAction"): e.get("eventDate") for e in j.get("events", []) or []}
        registrar = registrant = None
        for ent in j.get("entities", []) or []:
            roles = ent.get("roles", []) or []
            name = _vcard_field(ent.get("vcardArray", []), "fn")
            if "registrar" in roles:
                registrar = name or ent.get("handle")
            if "registrant" in roles:
                registrant = name or ent.get("handle")
        return {"domain": j.get("ldhName", domain), "found": True,
                "registrar": registrar, "registrant": registrant,
                "statuses": j.get("status", []), "created": events.get("registration"),
                "expires": events.get("expiration"),
                "last_changed": events.get("last changed"),
                "nameservers": [ns.get("ldhName") for ns in j.get("nameservers", []) or []]}

    def subdomains(self, domain: str, limit: int = 100) -> dict:
        import time

        import requests
        from oto.tools.common.errors import raise_for_upstream

        rows, last_err = None, "unknown"
        with _session() as s:
            for attempt in range(3):
                if attempt:
                    time.sleep(1.5 * attempt)
                try:
                    r = s.get(self.CRTSH_URL, params={"q": f"%.{domain}", "output": "json"},
                              headers={"user-agent": self.user_agent}, timeout=40)
                except requests.RequestException as e:
                    last_err = type(e).__name__
                    continue
                if r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}"
                    continue
                raise_for_upstream(r, service="crt.sh")
                rows = r.json()
                break
        if rows is None:
            return {"domain": domain, "error": f"crt.sh unavailable ({last_err})",
                    "subdomains": []}
        names: set[str] = set()
        for row in rows:
            for n in (row.get("name_value") or "").splitlines():
                n = n.strip().lower().lstrip("*.")
                if n.endswith(domain) and n != domain:
                    names.add(n)
        ordered = sorted(names)
        return {"domain": domain, "count": len(ordered), "subdomains": ordered[:limit]}

    def headers(self, domain: str) -> dict:
        from oto.tools.infosec.client import _SECURITY_HEADERS

        try:
            with _session() as s:
                r = s.get(f"https://{domain}", timeout=_TIMEOUT_S, allow_redirects=True,
                          headers={"user-agent": self.user_agent})
        except egress.EgressRefused:
            raise
        except Exception as e:  # rendered in the result (error), not swallowed
            return {"domain": domain, "error": f"{type(e).__name__}: {e}"}
        h = {k.lower(): v for k, v in r.headers.items()}
        checks = {key: header in h for key, header in _SECURITY_HEADERS}
        return {"domain": domain, "final_url": str(r.url), "status": r.status_code,
                "security_headers": checks,
                "security_headers_score": f"{sum(checks.values())}/{len(checks)}",
                "server": h.get("server"), "x_powered_by": h.get("x-powered-by")}

    def tls(self, host: str, port: int = 443) -> dict:
        adresse = _adresse_gardee(host, port)
        try:
            return _poignee_tls(host, adresse, port)
        except Exception as e:  # rendered in the result (error), not swallowed
            return {"host": host, "port": port, "error": f"{type(e).__name__}: {e}"}


def _adresse_gardee(host: str, port: int) -> str:
    """The address the TLS handshake connects to: `host` resolved ONCE, every address
    judged as an agent URL (no exception) — the same decision as the guarded sessions,
    on the very address joined. Raises `egress.EgressRefused`."""
    if not 0 < int(port) < 65536:
        raise egress.EgressRefused(f"`{_CONNECTEUR}` : port {port} hors plage.")
    egress.refuse_instance(host, connector=_CONNECTEUR, field="domain")
    try:
        adresses = egress.resolved_addresses(host, port)
    except OSError as e:
        raise egress.EgressRefused(
            f"`{_CONNECTEUR}` : l'hôte `{host}` ne résout pas ({e}).") from None
    if not adresses:
        raise egress.EgressRefused(f"`{_CONNECTEUR}` : l'hôte `{host}` ne résout pas.")
    egress.decide(host, port, adresses, connector=_CONNECTEUR, field="domain",
                  titulaire=egress.AGENT)
    return sorted(adresses)[0]


def _poignee_tls(host: str, adresse: str, port: int) -> dict:
    """The certificate presented on `adresse` for `host` (SNI and verification on the
    name). A failed verification is reported, the protocol read without verification
    — an inspection, no data exchanged."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((adresse, port), timeout=_TIMEOUT_S) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert() or {}
                version, cipher = ss.version(), ss.cipher()
        validated, err = True, None
    except ssl.SSLCertVerificationError as e:
        cert, validated, err = {}, False, str(e)
        uctx = ssl._create_unverified_context()
        with socket.create_connection((adresse, port), timeout=_TIMEOUT_S) as sock:
            with uctx.wrap_socket(sock, server_hostname=host) as ss:
                version, cipher = ss.version(), ss.cipher()
    subject = {k: v for t in cert.get("subject", ()) for k, v in t}
    issuer = {k: v for t in cert.get("issuer", ()) for k, v in t}
    return {"host": host, "port": port, "validated": validated, "error": err,
            "protocol": version, "cipher": cipher[0] if cipher else None,
            "issuer": issuer.get("organizationName") or issuer.get("commonName"),
            "subject_cn": subject.get("commonName"),
            "valid_from": cert.get("notBefore"), "valid_until": cert.get("notAfter"),
            "subject_alt_names": [v for k, v in cert.get("subjectAltName", ())
                                  if k == "DNS"]}


@lru_cache(maxsize=4)
def _classe(base: type) -> type:
    return type("InfosecGarde", (_Garde, base), {})


def _client() -> InfosecClient:
    return _classe(InfosecClient)(_ua())


def register(mcp: FastMCP) -> None:
    # `op` has NO default: no facet is "the" natural reading of a
    # domain, and a default would answer something other than what was asked.
    @mcp.tool()
    def infosec_domain(
        op: Literal["whois", "dns", "email_security", "subdomains", "tls", "headers",
                    "blocklist", "deliverability"],
        domain: str = "",
        ip: Optional[str] = None,
        dkim_selector: Optional[str] = None,
        limit: int = 100,
        port: int = 443,
    ) -> dict:
        """Check a domain: email deliverability, blocklists, DNS, whois, TLS — `op` picks the check.

        Read-only and passive: DNS-over-HTTPS, RDAP, public CT logs, a TLS handshake,
        one HTTP GET. No port scan, no vulnerability probing, no key needed.

        `op` :
        - **"deliverability"** : full email-deliverability report for YOUR sending
          domain — SPF/DMARC/DKIM/MTA-STS/TLS-RPT/BIMI + blocklists, a `score` /100,
          a `grade` and `recommendations` sorted by priority. Start here to answer
          « do our emails land in spam? ». What could not be read (DNS error, time
          budget) is listed in `coverage` and NOT scored — `coverage.partial` says so. Pass `ip` for a dedicated sending IP and
          `dkim_selector` if the domain signs with a custom selector.
        - **"blocklist"** : is the domain or a sending IPv4 on a spam blocklist (DNSBL)?
          Domain lists + IP lists (SpamCop, PSBL, NordSpam, s5h — lists whose terms
          allow automated commercial lookups), with reverse DNS of each IP. IPs come from `ip`, else from the
          domain's SPF `ip4:` entries. Each list answers `listed` / `clean` / `error` —
          an `error` is NOT a clean result. ⚠️ Spamhaus, SURBL, URIBL, Barracuda,
          Mailspike and UCEPROTECT are returned under `not_checked` (public resolvers
          refused, or terms that forbid automated commercial use), never as clean. A domain
          sending through Google/Microsoft/an emailing tool has no IP of its own to
          check — the response says so in `notes`.
        - **"email_security"** : email authentication posture — SPF (record, `all`
          qualifier, DNS lookup count vs the 10 limit), DMARC (policy, sp, pct, rua,
          alignment), DKIM on common selectors (+ `dkim_selector`), MTA-STS, TLS-RPT,
          BIMI, MX. ⚠️ DKIM selectors cannot be enumerated: an empty
          `dkim_selectors_found` does not prove DKIM is absent.
        - **"whois"** : domain registration via RDAP — registrar, creation/expiration
          dates, statuses, nameservers, registrant org when public.
        - **"dns"** : A/AAAA/MX/NS/TXT records, with stack hints (mail provider, SaaS
          verification tokens).
        - **"subdomains"** : known subdomains from Certificate Transparency logs
          (crt.sh). `count` = all distinct found, `subdomains` = truncated to `limit`.
          crt.sh often fails transiently: the response then carries `error`.
        - **"tls"** : TLS certificate of the host (issuer, validity, SANs, protocol);
          a failed validation is reported (`validated=false` + `error`), not hidden.
        - **"headers"** : HTTP security headers (HSTS, CSP, …) + server fingerprint.

        Args:
            op: deliverability | blocklist | email_security | whois | dns | subdomains | tls | headers.
            domain: a domain, URL or email address (only the domain part is kept).
                Required except for op="blocklist" with `ip`.
            ip: op="blocklist"/"deliverability" — an IPv4 sending address to check.
            dkim_selector: op="email_security"/"deliverability" — DKIM selector to test
                in addition to the common ones.
            limit: op="subdomains" — max distinct subdomains returned (default 100).
            port: op="tls" — TLS port (default 443).
        """
        try:
            return _client().check(op, domain=domain, ip=ip, dkim_selector=dkim_selector,
                                   limit=limit, port=port)
        except ValueError as e:   # EgressRefused ⊂ ValueError: a refusal, named
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
