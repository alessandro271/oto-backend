"""Instagram (statistiques) — les cinq outils, en enveloppes minces.

Le client vit dans oto-core (`oto.tools.instagram_meta`) ; la résolution du jeton
et son renouvellement dans `instagram_meta_session.py`. Ici, rien d'autre que : un
schéma, un appel, et le JSON de Meta rendu **tel quel**.

Rendre le brut est un choix, pas une paresse : ces chiffres partent à un agent qui
sait lire un objet, et toute recomposition maison — renommer une métrique, en
dériver un ratio, arrondir — deviendrait un contrat qu'il faudrait tenir alors que
Meta fait déjà évoluer le sien. Les noms de métriques sont ceux de l'API.

Les noms d'outils et leurs schémas reprennent ceux du serveur MCP qui a précédé ce
connecteur, au préfixe près : ce sont des noms qu'un agent et une utilisatrice
connaissent déjà.

⚠️ **Lecture seule, et sans exception** : rien n'est publié, modifié ni supprimé.
Le consentement demandé ne le permettrait d'ailleurs pas — les permissions
`instagram_business_basic` et `instagram_business_manage_insights` ne portent
aucune écriture.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from . import instagram_meta_session as session
from .instagram_meta_session import _client, appeler


def register(mcp: FastMCP) -> None:
    # ⚠️ AUCUN import du cœur ici. Le connecteur reste MONTÉ même quand oto-core
    # est trop ancien pour le porter, ou que les coordonnées de l'application ne
    # sont pas posées : il est alors visible, sélectionnable, et chaque appel
    # refuse en nommant ce qui manque. Un connecteur qui disparaît du catalogue ne
    # se remarque pas et ne s'explique pas — c'est l'utilisatrice qui paie la
    # différence.
    session.avertir_au_demarrage()

    @mcp.tool()
    async def instagram_meta_get_profile() -> dict:
        """Profile of the connected Instagram professional account: username, name,
        biography, followers/follows/media counts, profile picture URL, website.

        Use it first to confirm WHICH account is connected.
        """
        ig = await _client()
        return await appeler("le profil du compte", ig.get_profile)

    @mcp.tool()
    async def instagram_meta_get_recent_media(
        limit: Annotated[int, Field(ge=1, le=50,
                                    description="Number of posts to fetch")] = 10,
    ) -> list[dict]:
        """List the N most recent posts with their basic metrics.

        Returns: id, caption, timestamp, media_type (IMAGE/VIDEO/CAROUSEL_ALBUM),
        media_product_type (FEED/REELS/STORY), like_count, comments_count, permalink.

        Useful to browse history, spot recent posts, and get a media_id to pass to
        instagram_meta_get_media_insights.
        """
        ig = await _client()
        return await appeler("les publications récentes", ig.get_recent_media, limit)

    @mcp.tool()
    async def instagram_meta_get_media_insights(
        media_id: Annotated[str, Field(
            description="Post id (from instagram_meta_get_recent_media)")],
    ) -> dict:
        """Detailed insights for one post.

        The metrics requested depend on the media type:
        - feed post / carousel: reach, views, likes, comments, saved, shares,
          total_interactions, follows, profile_visits
        - reel: same minus follows/profile_visits, plus ig_reels_avg_watch_time
          and ig_reels_video_view_total_time
        - story: reach, views, replies, shares, total_interactions, follows,
          profile_visits
        """
        ig = await _client()
        return await appeler("les insights de cette publication",
                             ig.get_media_insights, media_id)

    @mcp.tool()
    async def instagram_meta_get_account_insights(
        days: Annotated[int, Field(ge=1, le=30,
                                   description="Window in days, max 30")] = 30,
    ) -> dict:
        """Account-level stats over the last N days (max 30, the API's since/until
        window): reach, views, accounts_engaged, total_interactions, likes,
        comments, saves, shares and profile_links_taps (taps on the profile links —
        replaces the retired profile_views and website_clicks).
        """
        ig = await _client()
        return await appeler("les statistiques du compte",
                             ig.get_account_insights, days)

    @mcp.tool()
    async def instagram_meta_get_best_hours(
        sample_size: Annotated[int, Field(ge=5, le=50,
                                          description="Number of posts to analyse")] = 30,
    ) -> dict:
        """Heuristic: best hours and weekdays to publish, from the average
        engagement (likes + comments) of the N most recent posts.

        Returns by_hour and by_weekday sorted by average engagement, plus
        sample_size. Hours are those of the timestamps returned by the API (UTC).
        This is a local computation over posts, not a metric Instagram publishes —
        read it with sample_size in mind.
        """
        ig = await _client()
        media = await appeler("les publications récentes", ig.get_recent_media,
                              sample_size)
        return session._coeur().compute_best_hours(media)
