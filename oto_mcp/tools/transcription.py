"""Transcription — un audio du projet devient une page du projet (ADR 0074).

Face MCP. Deux outils. `transcription_create` : l'agent désigne un fichier par sa
RÉFÉRENCE (`file_source`, typiquement `project_file`), le serveur lit les octets
et dépose un TRAVAIL — il ne bloque PAS l'agent (#674, arbitrage du 22/09/2026:
un connecteur long ne doit jamais l'attendre en ligne). `transcription_status`
relit ce travail : en cours, terminé (avec la page), ou en échec (avec le refus).

Le dépôt et la relecture sont ceux de la ressource REST (`capabilities/transcription.py`,
gardes et ordre documentés là) ; ce module ne porte que le projet ambiant (`_project`),
la traduction des refus en erreur MCP, et la sonde du connecteur. Le travail de fond
(appel Mistral, page) vit dans `oto_mcp/transcription_worker.py`.

Credential à 3 champs (ADR 0011) : la clé (secret), la langue et le vocabulaire (non
secrets) — une instance = une clé × un vocabulaire, rattachable à un projet par slot.
"""
from __future__ import annotations

from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access, file_source
from ..capabilities import transcription as _transcription
from ..capabilities._types import AuthzDenied
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError


def _refus(message: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=message))


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — couvre `auth` SEUL : `GET /v1/models` n'est
    pas facturé et refuse une clé invalide. Elle ne dit rien du solde du compte."""
    from oto.tools.mistral import MistralClient

    MistralClient(api_key=fields["api_key"]).list_models()


def register(mcp: FastMCP) -> None:
    connector_verify.register("transcription", _verify)

    @mcp.tool()
    def transcription_create(source: dict, vocabulary: str | None = None,
                             vocabulary_replace: bool = False) -> dict:
        """Start transcribing an audio recording (site visit, meeting, voice note)
        into a new page of the project — ASYNCHRONOUS, returns a job reference
        immediately (a ~30 min recording takes 20 s to 5 min to process). Needs
        `_project` (the page's project). Poll `transcription_status(job_id)` for
        the result — the page, one paragraph per speaker turn, never the raw text.

        Args:
            source: the file, never its bytes. Project file:
                `{"kind":"project_file","project_id":<id>,"file_id":<id>}` (ids from
                oto_project_files op=list); also `drive`, `gmail`, `url`.
            vocabulary: optional extra words to spell right for THIS recording
                (names, materials), separated by commas or newlines. Added to the
                connector's vocabulary; 100 words at most overall, the surplus is
                reported by transcription_status (`vocabulary_dropped`).
            vocabulary_replace: true = use only `vocabulary`, ignore the connector's.
        """
        pid = access.current_project()
        if pid is None:
            raise _refus("transcription_create écrit une page de projet : passe "
                         "`_project=<id>` (le projet qui recevra la page).")
        sub = access.current_user_sub_or_raise()
        try:
            ref = _transcription.deposer(
                sub, pid,
                lambda: file_source.resolve(source, max_bytes=_transcription.MAX_AUDIO_BYTES),
                vocabulary=vocabulary, vocabulary_replace=vocabulary_replace)
        except AuthzDenied as e:
            # Le refus du résolveur de credential est déjà une erreur MCP actionnable
            # (connecteur à activer, quota) : la rendre telle quelle, pas sa traduction.
            if isinstance(e.__cause__, McpError):
                raise e.__cause__ from None
            raise _refus(str(e)) from None
        return {**ref, "note": "Relire avec transcription_status(job_id)."}

    @mcp.tool()
    def transcription_status(job_id: int) -> dict:
        """Read a transcription job started by `transcription_create`. Returns
        `{status: pending|running|done|failed, ...}` — on `done`, the page
        `{id, title, url}` plus words/duration/speakers; on `failed`, `error`."""
        sub = access.current_user_sub_or_raise()
        try:
            return _transcription.relire(sub, int(job_id), transcript=False)
        except AuthzDenied as e:
            raise _refus(str(e)) from None
