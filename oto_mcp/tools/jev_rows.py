"""`jev_rows` helpers: check the call against the table schema, build states, map answers
to cells. No I/O here; the tool itself lives in `tools/jev.py`.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..datastore import acces_agent as aga
from ..datastore import par_reference as pr

#: Rows per call, and per dry run.
MAX_ROWS = 500
MAX_DRY_RUN = 20
#: Rows read per page (each page is one quota check).
PAGE = 100
PARALLEL_DEFAULT = 25
PARALLEL_MAX = 50
#: Marker written to `model_column` for a row Jev can never answer.
ERROR_PREFIX = "jev_error: "
#: Below this, an answer counts as `low_confidence` (reported, never acted on).
LOW_CONFIDENCE = 0.5
#: A state this thin is reported as `thin_state`: it tends to score low for lack of evidence.
THIN_CHARS = 200
THIN_FIELDS = 2

#: Column types each question type may write to.
_TYPES = {"choice": {"text", "enum"}, "score": {"number"}, "noul": {"number"}}


class Refusal(ValueError):
    """A call the table cannot take; raised before any upstream call."""


def fields_of(schema: Optional[dict]) -> dict[str, dict]:
    return {f["key"]: f for f in (schema or {}).get("fields") or [] if f.get("key")}


def check_state_fields(state_fields: Any, fields: dict, hidden: frozenset) -> list[tuple[str, Optional[int]]]:
    """`[(column, max_chars|None)]` from `["col", {"col": 150}, …]`."""
    if not isinstance(state_fields, list) or not state_fields:
        raise Refusal("`state_fields`: a non-empty list of columns is required.")
    out: list[tuple[str, Optional[int]]] = []
    for item in state_fields:
        if isinstance(item, str) and item:
            out.append((item, None))
        elif (isinstance(item, dict) and len(item) == 1
              and isinstance(next(iter(item.values())), int)
              and next(iter(item.values())) > 0):
            (col, n), = item.items()
            out.append((col, n))
        else:
            raise Refusal(f"`state_fields`: {item!r} is not a column name or "
                          "`{column: max_chars}`.")
    unknown = [c for c, _ in out if c not in fields or c in hidden]
    if unknown:
        raise Refusal(f"`state_fields`: unknown column(s) {', '.join(unknown)}.")
    return out


def check_outputs(questions: Any, output: Any, model_column: Any, fields: dict,
                  closed: frozenset) -> None:
    """Every question has a typed target column, a `<col>_p` number for choice/score,
    and `model_column` is a text column."""
    if not isinstance(questions, dict) or not questions:
        raise Refusal("`questions`: at least one question is required.")
    no_criteria = [q for q, spec in questions.items()
                   if not isinstance(spec, dict) or not spec.get("criteria")]
    if no_criteria:
        raise Refusal(f"`criteria` is required on every question: missing on "
                      f"{', '.join(map(str, no_criteria))}.")
    if not isinstance(output, dict) or set(output) != set(questions):
        got = set(output) if isinstance(output, dict) else set()
        raise Refusal("`output` must map every question to a column: "
                      f"missing {sorted(set(questions) - got)}, extra {sorted(got - set(questions))}.")
    problems: list[str] = []
    for q, col in output.items():
        qtype = questions[q].get("type")
        f = fields.get(col) if isinstance(col, str) else None
        if f is None or col in closed:
            problems.append(f"`{col}` is not a column agents can write")
            continue
        if qtype not in _TYPES:
            problems.append(f"question `{q}` has unknown type {qtype!r}")
            continue
        if f.get("type", "text") not in _TYPES[qtype]:
            problems.append(f"`{col}` is {f.get('type')!r}, a {qtype} needs "
                            f"{' or '.join(sorted(_TYPES[qtype]))}")
        if qtype == "choice" and f.get("type") == "enum":
            missing = [k for k in questions[q]["criteria"] if k not in (f.get("options") or [])]
            if missing:
                problems.append(f"enum `{col}` lacks option(s) {', '.join(missing)}")
        if qtype in ("choice", "score"):
            p = fields.get(f"{col}_p")
            if p is None or p.get("type") != "number" or f"{col}_p" in closed:
                problems.append(f"`{col}_p` must be a declared number column (confidence)")
    mf = fields.get(model_column) if isinstance(model_column, str) else None
    if mf is None or model_column in closed or mf.get("type", "text") != "text":
        problems.append("`model_column` must be a declared text column")
    if problems:
        raise Refusal("Nothing sent: " + "; ".join(problems) + ".")


def hidden_of(schema: Optional[dict]) -> frozenset:
    return frozenset(aga.masquees(schema) or ())


def closed_of(schema: Optional[dict]) -> frozenset:
    return frozenset(aga.fermees(schema) or ())


def state_of(row: dict, cols: list[tuple[str, Optional[int]]]) -> dict:
    """The state sent for a row: non-empty cells only, truncated where asked."""
    state = {}
    for col, n in cols:
        v = pr.valeur(row, col)
        if v is None:
            continue
        if n and isinstance(v, str) and len(v) > n:
            v = v[:n]
        state[col] = v
    return state


def is_thin(state: dict, first: str) -> bool:
    rest = [k for k in state if k != first]
    size = len(json.dumps(state, ensure_ascii=False, default=str))
    return len(rest) < THIN_FIELDS or size < THIN_CHARS


def patch_of(answers: dict, output: dict, model_column: str, model: str) -> tuple[dict, list[float]]:
    """Cells to write, and the confidences written. Raises KeyError on a missing answer."""
    patch: dict = {}
    conf: list[float] = []
    for q, col in output.items():
        a = answers[q]
        t = a.get("type")
        if t == "noul":
            patch[col] = a["noul"]
            continue
        patch[col] = a["choice"] if t == "choice" else a["score"]
        c = a.get("confidence")
        if c is not None:
            patch[f"{col}_p"] = c
            conf.append(c)
    patch[model_column] = model
    return patch, conf
