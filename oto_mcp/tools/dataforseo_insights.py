"""DataForSEO — what AI assistants and the web say about a brand.

Second module of the `dataforseo` connector (`providers/dataforseo.py::modules`);
credential, refusals, view and billing are shared with `tools/dataforseo.py`.

- `dataforseo_ai_visibility`: brand / domain mentions in AI answers (Google AI
  Overviews, ChatGPT), asking a model directly (ChatGPT, Claude, Gemini,
  Perplexity) and reading the sources it cites, Google AI Mode.
- `dataforseo_web_mentions`: web pages citing a brand or keyword, with sentiment.
"""
from __future__ import annotations

from typing import Optional, Union

from fastmcp import FastMCP

from . import dataforseo as d

_MENTIONS_LIMIT_DEFAULT = 5   # each mention carries a full AI answer
_WEB_LIMIT_MAX = 100          # content_analysis/search caps at 100


def _targets(domain: Optional[str], keyword: Optional[str], targets: Optional[list]) -> list:
    """The LLM-mentions target list: `domain` / `keyword` shortcuts, or DataForSEO's
    own entities as is."""
    if d.present(targets):
        if d.present(domain) or d.present(keyword):
            raise d.bad("Pass either `targets` or `domain`/`keyword`, not both.")
        return targets
    out = ([{"domain": domain}] if d.present(domain) else []) + (
        [{"keyword": keyword}] if d.present(keyword) else [])
    if not out:
        raise d.bad("Name what to look for: `domain` (e.g. 'example.com'), `keyword` "
                    "(e.g. a brand name), or `targets`.")
    return out


def register(mcp: FastMCP) -> None:
    from oto.tools.dataforseo import DataForSeoClient

    def _client() -> tuple[DataForSeoClient, bool]:
        login, password, is_platform = d.resolve()
        return DataForSeoClient(login=login, password=password), is_platform

    @mcp.tool()
    def dataforseo_ai_visibility(
        op: str,
        domain: Optional[str] = None,
        keyword: Optional[str] = None,
        targets: Optional[list[dict]] = None,
        platform: Optional[str] = None,
        location: Optional[Union[int, str]] = None,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
        engine: Optional[str] = None,
        model: Optional[str] = None,
        web_search: Optional[bool] = None,
        country: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list] = None,
        order_by: Optional[list[str]] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """Is a brand cited by AI assistants? Mentions in AI answers, and asking a model.

        Mentions ops — `domain` and/or `keyword` (or DataForSEO `targets`), on
        `platform` `google` (AI Overviews) or `chat_gpt` (United States + English
        only); none = both. ⚠️ ≈$0.10 per call:
        - `metrics` — how often the target is mentioned, with AI search volume,
          by platform and cited source.
        - `mentions` — the AI answers that mention it: question, answer, sources
          (default 5).
        - `top_domains` / `top_pages` — what AI answers cite most for it.
        - `history` — mention metrics per month (since 2025-08; `date_from`).

        Other ops:
        - `ask` — send `prompt` to `engine` (`chat_gpt` | `claude` | `gemini` |
          `perplexity`) with `model` (list: `op="models"`) and read the answer
          and the sources it cites; `web_search=True` lets the model search,
          `country` (ISO, e.g. "FR") localises it. Billed on the model's tokens
          plus a small fee.
        - `models` — the model names an `engine` accepts. Free.
        - `ai_mode` — Google's AI Mode answer for `keyword`, with its sources.
          `location` + `language` required. ≈$0.004.

        For how often a keyword is ASKED to assistants: `dataforseo_keywords(op="ai_volume")`.

        Returns — `{cost_usd, …}`, trimmed (`full=True` for the raw result).

        Args:
            op: see above.
            domain / keyword / targets: mentions ops; `keyword` is also the query of `ai_mode`.
            platform: `google` | `chat_gpt` (mentions ops).
            location / language: as in `dataforseo_keywords`.
            prompt / engine / model / web_search / country: `ask` (`engine` also `models`).
            date_from / date_to: `history` (`yyyy-mm-dd`).
            limit: `mentions`, `top_domains`, `top_pages` (default 5, max 1000).
            offset, filters, order_by, options, full: as in `dataforseo_keywords`.
        """
        place = d.loc(location, language)
        d.check_place("ai", op, place)
        d.refuse(op, keywords=keywords)
        mentions_ops = ("metrics", "mentions", "top_domains", "top_pages", "history")
        if op not in mentions_ops:
            d.refuse(op, domain=domain, targets=targets, platform=platform,
                     date_from=date_from, date_to=date_to, limit=limit, offset=offset,
                     filters=filters, order_by=order_by)
        if op != "ask":
            d.refuse(op, prompt=prompt, model=model, web_search=web_search, country=country)
            if op != "models":
                d.refuse(op, engine=engine)
        if op != "history":
            d.refuse(op, date_from=date_from, date_to=date_to)
        c, is_platform = _client()
        with d.upstream(is_platform):
            if op in mentions_ops:
                t = _targets(domain, keyword, targets)
                if op == "metrics":
                    d.refuse(op, limit=limit, offset=offset, filters=filters,
                             order_by=order_by)
                    task = c.llm_mentions_metrics(t, platform=platform, options=options,
                                                  **place)
                elif op == "mentions":
                    task = c.llm_mentions_search(
                        t, platform=platform, options=options, offset=offset,
                        filters=filters, order_by=order_by,
                        limit=d.limit_of(limit, _MENTIONS_LIMIT_DEFAULT), **place)
                elif op in ("top_domains", "top_pages"):
                    d.refuse(op, offset=offset, filters=filters, order_by=order_by)
                    fn = (c.llm_mentions_top_domains if op == "top_domains"
                          else c.llm_mentions_top_pages)
                    task = fn(t, platform=platform, options=options,
                              limit=d.limit_of(limit, _MENTIONS_LIMIT_DEFAULT), **place)
                else:
                    d.refuse(op, limit=limit, offset=offset, filters=filters,
                             order_by=order_by, options=options)
                    task = c.llm_mentions_history(t, platform=platform, date_from=date_from,
                                                  date_to=date_to, **place)
            elif op == "ask":
                d.refuse(op, keyword=keyword, location=location, language=language)
                task = c.llm_response(d.need(engine, "engine", op), d.need(prompt, "prompt", op),
                                      d.need(model, "model", op), web_search=web_search,
                                      web_search_country_iso_code=country, options=options)
            elif op == "models":
                d.refuse(op, keyword=keyword, location=location, language=language,
                         options=options)
                task = c.llm_models(d.need(engine, "engine", op))
                d.bill(task)
                return {"items": [r.get("model_name") for r in (task.get("result") or [])]}
            elif op == "ai_mode":
                task = c.google_ai_mode(d.need(keyword, "keyword", op), options=options,
                                        **place)
            else:
                raise d.bad("op must be one of: metrics, mentions, top_domains, top_pages, "
                            "history, ask, models, ai_mode.")
        d.bill(task)
        return d.view(task, full)

    @mcp.tool()
    def dataforseo_web_mentions(
        op: str,
        keyword: str,
        page_type: Optional[list[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        filters: Optional[list] = None,
        order_by: Optional[list[str]] = None,
        options: Optional[dict] = None,
        full: bool = False,
    ) -> dict:
        """Web pages that mention a brand or keyword, with their sentiment. ≈$0.02 per call.

        Ops:
        - `summary` — how many citations, positive / negative / neutral split,
          emotions, top citing domains, countries and page types.
        - `search` — the citations: URL, domain, snippet, date, sentiment
          (default 20, max 100).

        Returns — `{cost_usd, …}`, trimmed (`full=True` for the raw result).

        Args:
            op: `summary` | `search`.
            keyword: the brand or phrase (e.g. "acme").
            page_type: narrow the sources — `ecommerce`, `news`, `blogs`,
                `message-boards`, `organization`.
            limit, offset, filters, order_by: `search` only (e.g. order by
                `["content_info.sentiment_connotations.anger,desc"]`).
            options, full: as in `dataforseo_keywords`.
        """
        if op == "summary":
            d.refuse(op, limit=limit, offset=offset, filters=filters, order_by=order_by)
        c, is_platform = _client()
        with d.upstream(is_platform):
            if op == "summary":
                task = c.content_summary(keyword, page_type=page_type, options=options)
            elif op == "search":
                task = c.content_search(keyword, page_type=page_type, offset=offset,
                                        filters=filters, order_by=order_by,
                                        limit=d.limit_of(limit, maximum=_WEB_LIMIT_MAX),
                                        options=options)
            else:
                raise d.bad("op must be `summary` or `search`.")
        d.bill(task)
        return d.view(task, full)
