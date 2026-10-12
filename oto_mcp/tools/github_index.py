"""GitHub list views — what a list op serves by default (`full=False`).

A list is an INDEX (ADR 0047): it serves to choose what to open next, not to read.
GitHub's REST v3 objects are mostly hypermedia: an issue carries a dozen `*_url`
links, its author is a full user object (eighteen keys, sixteen of them links), each
label repeats its colour and id, a pull request embeds BOTH repositories in full
under `head` and `base`. A page of 30 PRs weighs hundreds of kilobytes for what an
agent decides on: number, title, state, who, when.

What the default view removes, read in GitHub's REST v3 schemas — a DENYLIST of
named families, never an allowlist, so a key GitHub adds tomorrow is served:

- every API link: `url` and the `*_url` keys (`html_url`, the page a human opens,
  stays), the `_links` block, `node_id` (the GraphQL id), `gravatar_id`;
- `reactions`, `performed_via_github_app`, a commit's `verification` (signature and
  signed payload);
- NESTED objects become the one key that names them: a user (`login` + `type`) its
  `login`, a label its `name`, a milestone its `title`, a repository its
  `full_name`. A row of the list itself is never collapsed — `op='contributors'`
  rows are users, `op='labels'` rows are labels;
- a BODY (an issue's description, a release's notes, a file's `patch`) becomes its
  size, `<key>_length`: projecting is not truncating — the agent knows what it has
  NOT read, and opens the one object (`op='get'`, served whole) to read it.
"""
from __future__ import annotations

from typing import Any, Iterable

#: Keys removed everywhere in the default view.
_RETIRE = frozenset({"url", "_links", "node_id", "gravatar_id", "reactions",
                     "performed_via_github_app", "verification"})


def _lien(cle: str) -> bool:
    return cle.endswith("_url") and cle != "html_url"


def _nom(objet: dict) -> Any:
    """The key that names a NESTED GitHub object, or `None` if it is not one."""
    if "login" in objet and "type" in objet:                 # simple-user
        return objet["login"]
    if {"name", "color", "default"} <= objet.keys():         # label
        return objet["name"]
    if {"number", "title", "open_issues"} <= objet.keys():   # milestone
        return objet["title"]
    if {"full_name", "owner", "private"} <= objet.keys():    # repository
        return objet["full_name"]
    return None


def _propre(valeur: Any, imbrique: bool) -> Any:
    if isinstance(valeur, list):
        return [_propre(v, imbrique) for v in valeur]
    if not isinstance(valeur, dict):
        return valeur
    if imbrique:
        nom = _nom(valeur)
        if nom is not None:
            return nom
    return {k: _propre(v, True) for k, v in valeur.items()
            if k not in _RETIRE and not _lien(k)}


def index(payload: Any, *, bodies: Iterable[str] = ()) -> Any:
    """The default view of a list page. A payload that is not a list (an error
    body, a shape GitHub changed) passes untouched: a projection that raises would
    turn a useful answer into a failure."""
    if not isinstance(payload, list):
        return payload
    corps = tuple(bodies)
    out = []
    for ligne in payload:
        vue = _propre(ligne, False)
        if isinstance(vue, dict):
            for b in corps:
                if isinstance(vue.get(b), str):
                    vue[f"{b}_length"] = len(vue.pop(b))
        out.append(vue)
    return out
