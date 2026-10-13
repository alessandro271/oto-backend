"""GitHub : une liste est un INDEX (`oto_mcp/tools/github_index.py`).

Les objets REST v3 de GitHub sont surtout des liens : une issue porte une douzaine de
`*_url`, son auteur est un objet utilisateur entier (dix-huit clés, seize liens), une
pull request embarque les DEUX dépôts entiers sous `head` et `base`. Le défaut des
opérations de liste rend ce qu'un agent départage ; `full=True` rend la page brute ;
un objet seul (`op='get'`) est servi entier et refuse `full`.

Les formes ci-dessous suivent les exemples de la documentation REST v3 de GitHub
(issue, pull request, dépôt), réduites aux clés qui comptent pour la règle.
"""
import asyncio
import json
from unittest.mock import patch

import pytest

from oto_mcp.mcp_errors import McpError
from oto_mcp.tools import github_index


def _user(login):
    base = f"https://api.github.com/users/{login}"
    return {"login": login, "id": 1, "node_id": "MDQ6VXNlcjE=",
            "avatar_url": "https://github.com/images/error/octocat_happy.gif",
            "gravatar_id": "", "url": base, "html_url": f"https://github.com/{login}",
            "followers_url": f"{base}/followers", "following_url": f"{base}/following{{/other_user}}",
            "gists_url": f"{base}/gists{{/gist_id}}", "starred_url": f"{base}/starred{{/owner}}{{/repo}}",
            "subscriptions_url": f"{base}/subscriptions", "organizations_url": f"{base}/orgs",
            "repos_url": f"{base}/repos", "events_url": f"{base}/events{{/privacy}}",
            "received_events_url": f"{base}/received_events", "type": "User",
            "site_admin": False}


def _repo(full_name):
    owner = full_name.split("/")[0]
    base = f"https://api.github.com/repos/{full_name}"
    liens = {f"{k}_url": f"{base}/{k}" for k in (
        "forks", "keys", "collaborators", "teams", "hooks", "issue_events", "events",
        "assignees", "branches", "tags", "blobs", "git_tags", "git_refs", "trees",
        "statuses", "languages", "stargazers", "contributors", "subscribers",
        "subscription", "commits", "git_commits", "comments", "issue_comment",
        "contents", "compare", "merges", "archive", "downloads", "issues", "pulls",
        "milestones", "notifications", "labels", "releases", "deployments")}
    return {"id": 1296269, "node_id": "MDEwOlJlcG9zaXRvcnkxMjk2MjY5",
            "name": full_name.split("/")[1], "full_name": full_name,
            "owner": _user(owner), "private": False,
            "html_url": f"https://github.com/{full_name}", "description": "This your first repo!",
            "fork": False, "url": base, **liens, "default_branch": "main",
            "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT",
                        "url": "https://api.github.com/licenses/mit", "node_id": "MDc6TGljZW5zZW1pdA=="},
            "stargazers_count": 80, "open_issues_count": 0, "pushed_at": "2011-01-26T19:06:43Z"}


def _label(name):
    return {"id": 208045946, "node_id": "MDU6TGFiZWwyMDgwNDU5NDY=",
            "url": f"https://api.github.com/repos/octocat/Hello-World/labels/{name}",
            "name": name, "description": "Something isn't working", "color": "f29513",
            "default": True}


def _issue(n):
    base = f"https://api.github.com/repos/octocat/Hello-World/issues/{n}"
    return {"id": 1, "node_id": "MDU6SXNzdWUx", "url": base,
            "repository_url": "https://api.github.com/repos/octocat/Hello-World",
            "labels_url": f"{base}/labels{{/name}}", "comments_url": f"{base}/comments",
            "events_url": f"{base}/events", "html_url": f"https://github.com/octocat/Hello-World/issues/{n}",
            "number": n, "state": "open", "title": "Found a bug",
            "body": "I'm having a problem with this. " * 40, "user": _user("octocat"),
            "labels": [_label("bug")], "assignee": _user("octocat"),
            "assignees": [_user("octocat")],
            "milestone": {"url": "https://api.github.com/repos/octocat/Hello-World/milestones/1",
                          "id": 1002604, "number": 1, "state": "open", "title": "v1.0",
                          "creator": _user("octocat"), "open_issues": 4, "closed_issues": 8},
            "locked": True, "comments": 0, "closed_at": None,
            "created_at": "2011-04-22T13:33:48Z", "updated_at": "2011-04-22T13:33:48Z",
            "author_association": "COLLABORATOR",
            "reactions": {"url": f"{base}/reactions", "total_count": 0, "+1": 0, "-1": 0},
            "timeline_url": f"{base}/timeline", "performed_via_github_app": None}


def _pull(n):
    return {"url": f"https://api.github.com/repos/octocat/Hello-World/pulls/{n}",
            "id": 1, "node_id": "MDExOlB1bGxSZXF1ZXN0MQ==",
            "html_url": f"https://github.com/octocat/Hello-World/pull/{n}",
            "diff_url": f"https://github.com/octocat/Hello-World/pull/{n}.diff",
            "number": n, "state": "open", "title": "Amazing new feature",
            "user": _user("octocat"), "body": "Please pull these awesome changes in!",
            "labels": [_label("bug")], "requested_reviewers": [_user("other_user")],
            "head": {"label": "octocat:new-topic", "ref": "new-topic", "sha": "6dcb09b",
                     "user": _user("octocat"), "repo": _repo("octocat/Hello-World")},
            "base": {"label": "octocat:main", "ref": "main", "sha": "6dcb09b",
                     "user": _user("octocat"), "repo": _repo("octocat/Hello-World")},
            "_links": {"self": {"href": "https://api.github.com/repos/octocat/Hello-World/pulls/1"}},
            "merged_at": None, "draft": False}


# --- la vue par défaut ---------------------------------------------------------

def test_une_issue_se_lit_par_ce_qui_la_departage():
    (vue,) = github_index.index([_issue(1347)], bodies=("body",))
    assert vue["number"] == 1347 and vue["title"] == "Found a bug"
    assert vue["html_url"] == "https://github.com/octocat/Hello-World/issues/1347"
    assert vue["user"] == "octocat" and vue["assignees"] == ["octocat"]
    assert vue["labels"] == ["bug"] and vue["milestone"] == "v1.0"
    assert vue["body_length"] == len(_issue(1)["body"]) and "body" not in vue
    for cle in ("url", "node_id", "comments_url", "timeline_url", "reactions",
                "performed_via_github_app"):
        assert cle not in vue, cle


def test_une_pull_request_ne_porte_plus_ses_deux_depots():
    (vue,) = github_index.index([_pull(1)], bodies=("body",))
    assert vue["head"] == {"label": "octocat:new-topic", "ref": "new-topic",
                           "sha": "6dcb09b", "user": "octocat",
                           "repo": "octocat/Hello-World"}
    assert vue["requested_reviewers"] == ["other_user"]
    assert "_links" not in vue and "diff_url" not in vue


def test_une_ligne_de_la_liste_n_est_jamais_reduite_a_son_nom():
    """`op='contributors'` rend des utilisateurs, `op='labels'` des labels, les listes
    de dépôts des dépôts : les réduire à leur nom viderait la liste."""
    (contrib,) = github_index.index([{**_user("octocat"), "contributions": 32}])
    assert contrib["login"] == "octocat" and contrib["contributions"] == 32
    assert "followers_url" not in contrib
    (label,) = github_index.index([_label("bug")])
    assert label["name"] == "bug" and label["color"] == "f29513"
    (depot,) = github_index.index([_repo("octocat/Hello-World")])
    assert depot["full_name"] == "octocat/Hello-World" and depot["owner"] == "octocat"
    assert depot["license"] == {"key": "mit", "name": "MIT License", "spdx_id": "MIT"}
    assert not [k for k in depot if k.endswith("_url") and k != "html_url"]


def test_une_forme_inattendue_passe_intacte():
    assert github_index.index({"message": "Not Found"}) == {"message": "Not Found"}
    assert github_index.index([1, "x", None]) == [1, "x", None]


def test_la_mesure():
    """La raison d'être : une page de 30 issues et une de 30 PR, avant et après."""
    for ligne, corps, plafond in ((_issue, ("body",), 0.15), (_pull, ("body",), 0.05)):
        page = [ligne(n) for n in range(1, 31)]
        avant = len(json.dumps(page))
        apres = len(json.dumps(github_index.index(page, bodies=corps)))
        assert apres < avant * plafond, (ligne.__name__, avant, apres)


# --- le câblage dans les outils -----------------------------------------------

@pytest.fixture(autouse=True)
def _fake_creds(monkeypatch):
    monkeypatch.setattr("oto_mcp.access.resolve_credential_fields",
                        lambda provider, account=None: {"token": "t", "base_url": ""})


class _Fake:
    def __init__(self, retour):
        self.retour = retour

    def __getattr__(self, name):
        return lambda *a, **kw: self.retour


def _call(tool_name, **kw):
    from fastmcp import FastMCP
    from oto_mcp.tools import github

    # Le client est importé à l'ENREGISTREMENT (`register`) : le patch doit l'englober.
    with patch("oto.tools.github.client.GitHubClient",
               return_value=_Fake(kw.pop("_retour"))):
        m = FastMCP("t")
        github.register(m)
        (tool,) = [t for t in asyncio.run(m._list_tools()) if t.name == tool_name]
        res = asyncio.run(tool.run(kw))
    # Une liste n'a pas de `structured_content` (sortie `Any`) : on lit le texte servi.
    contenu = getattr(res, "structured_content", None)
    return contenu if contenu is not None else json.loads(res.content[0].text)


def _rows(contenu):
    return contenu["result"] if isinstance(contenu, dict) and "result" in contenu else contenu


def test_les_listes_servent_l_index_par_defaut_et_la_page_sur_full():
    page = [_issue(1)]
    assert _rows(_call("github_issues", op="search", owner="o", repo="r",
                       _retour=page))[0]["user"] == "octocat"
    assert _rows(_call("github_issues", op="search", owner="o", repo="r", full=True,
                       _retour=page))[0]["user"]["login"] == "octocat"
    assert "head" in _rows(_call("github_pulls", op="search", owner="o", repo="r",
                                 _retour=[_pull(1)]))[0]
    assert _rows(_call("github_repos", op="mine", _retour=[_repo("o/r")]))[0]["owner"] == "o"


def test_un_objet_seul_est_servi_entier_et_refuse_full():
    entier = _call("github_issues", op="get", owner="o", repo="r", number=1,
                   _retour=_issue(1))
    assert entier["user"]["login"] == "octocat" and "body" in entier
    for outil, kw in (("github_issues", {"op": "get", "number": 1}),
                      ("github_pulls", {"op": "get", "number": 1}),
                      ("github_repos", {"op": "get"})):
        with pytest.raises(Exception) as e:
            _call(outil, owner="o", repo="r", full=True, _retour={}, **kw)
        assert "only applies to list ops" in str(e.value)
