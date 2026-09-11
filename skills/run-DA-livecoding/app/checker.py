"""Interviewer-only result check: does a candidate run return the same DATA as
solution.sql?

The comparison is on VALUES, never on SQL text and never on column names: a CTE,
a subquery, other aliases or a different join order all pass as long as the
result set matches. What it does look at, in order: the number of columns, the
number of rows, the multiset of rows (so duplicates count), and — only for
exercises whose statement fixes an order — the sequence of the ORDER BY key
columns, which keeps tied rows order-independent.

Nothing here is ever sent to the candidate; server.py writes it to the terminal
and the JSONL log only. Pure Python: no duckdb import, so it is cheap to test.
"""
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

MAX_PERMUTE_COLUMNS = 6  # 6! = 720 orderings; beyond that only identity is tried
MAX_REASON_CHARS = 70
# Wide enough for DECIMAL(38, n) and HUGEINT, which the default 28-digit
# context would push off the numeric path.
NUMERIC_PRECISION = 80

# Read statements that are not an answer attempt: the result cell stays blank
# instead of claiming the candidate got the exercise wrong.
_EXPLORATORY = {"EXPLAIN", "DESCRIBE", "DESC", "SHOW", "SUMMARIZE", "PRAGMA"}
_LEADING_NOISE = re.compile(r"(?:\s|--[^\n]*|/\*.*?\*/)*", re.S)
_FIRST_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_FAMILY_LABELS = {"number": "number", "text": "text", "date": "date",
                  "other": "value", "any": "value"}


@dataclass
class Expected:
    """The reference result of one exercise, ready to compare against."""
    columns: list          # solution column names (for messages only)
    rows: list             # normalized tuples
    ordered: bool = False
    key_indexes: list = field(default_factory=list)
    decimals: int = 2
    row_cap: int = 200


DISABLED = "disabled in exercise.json"


def build_expected(engine, exercises):
    """Run every solution.sql once and keep its result as the reference.

    Returns ({name: Expected|None}, {name: off_reason}, {name: degraded_reason},
    [warning]). Never raises: an exercise whose reference cannot be computed
    simply has its result column switched off, and the interview still runs.
    A "degraded" exercise still gets a verdict, but part of the check (so far
    only the row order) is not being applied — the banner says so, because an
    interviewer must not trust a check that is not running.
    """
    expected, off, degraded, warnings = {}, {}, {}, []
    for ex in exercises:
        try:
            exp, off_reason, caveat = _expected_for(engine, ex)
        except Exception as e:  # a checker bug must not stop the interview
            exp, off_reason, caveat = None, f"{type(e).__name__}: {e}", None
        expected[ex.name] = exp
        if exp is None:
            off[ex.name] = off_reason
            if off_reason != DISABLED:
                warnings.append(f"{ex.name}: {off_reason} — result column off")
        elif caveat:
            short, detail = caveat
            degraded[ex.name] = short
            warnings.append(f"{ex.name}: {detail}")
    return expected, off, degraded, warnings


def _expected_for(engine, ex):
    """-> (Expected|None, off_reason|None, (short, detail)|None)."""
    if not ex.check.get("enabled", True):
        return None, DISABLED, None
    if not ex.solution_sql:
        return None, "no solution.sql", None

    result = engine.run(ex.name, ex.solution_sql)
    if result.status != "ok":
        first_line = (result.error or "unknown error").strip().splitlines()[0]
        return None, f"solution.sql failed ({first_line})", None
    row_cap = getattr(engine, "row_cap", 200)
    if len(result.raw_rows) > row_cap:
        return None, f"solution returns more than {row_cap} rows", None

    decimals = ex.check.get("decimals", 2)
    ordered = bool(ex.check.get("ordered", False))
    caveat = None
    key_indexes = []
    for column in ex.check.get("order_by", []):
        if column not in result.columns:
            caveat = ("row order not checked",
                      f"check.order_by {column!r} is not a solution column "
                      f"{result.columns} — row order not checked")
            ordered, key_indexes = False, []
            break
        key_indexes.append(result.columns.index(column))

    return Expected(
        columns=list(result.columns),
        rows=[_normalize_row(r, decimals) for r in result.raw_rows],
        ordered=ordered,
        key_indexes=key_indexes,
        decimals=decimals,
        row_cap=row_cap,
    ), None, caveat


def is_exploratory(sql):
    """True for EXPLAIN/DESCRIBE/SHOW/SUMMARIZE/PRAGMA (not answer attempts)."""
    if not sql:
        return False
    rest = sql[_LEADING_NOISE.match(sql).end():]
    word = _FIRST_WORD.match(rest)
    return bool(word) and word.group(0).upper() in _EXPLORATORY


def normalize_cell(v, decimals):
    """Map a raw DuckDB value onto a hashable form that ignores harmless
    representation differences.

    Numbers agree across DECIMAL/DOUBLE/INTEGER and across a VARCHAR cast
    (rounded to `decimals`); a date equals its ISO string and a midnight
    timestamp. Text stays verbatim: 'Brazil' vs 'brazil' and '' vs NULL are
    real differences a candidate should see.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        # Tagged, so TRUE cannot silently equal the number 1.
        return ("bool", bool(v))
    if isinstance(v, (int, float, Decimal)):
        return _quantize(v, decimals)
    if isinstance(v, (datetime, date, dtime)):
        return _temporal(v)
    if isinstance(v, str):
        try:
            return _quantize(Decimal(v), decimals)
        except (InvalidOperation, ValueError):
            pass
        parsed = _parse_temporal(v)
        return v if parsed is None else _temporal(parsed)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def _temporal(v):
    """A date, a time or a timestamp as text; midnight collapses to the date."""
    if isinstance(v, datetime):
        if v.tzinfo is None and v.time() == dtime(0, 0):
            return v.date().isoformat()
        return v.isoformat(sep=" ")
    return v.isoformat()


def _parse_temporal(text):
    """The date or timestamp `text` spells out, else None.

    Lets a candidate's CAST(... AS VARCHAR) of a timestamp match the reference
    timestamp itself, the same way the raw values already match.
    """
    for parse in (datetime.fromisoformat, date.fromisoformat):
        try:
            return parse(text)
        except ValueError:
            continue
    return None


def _quantize(v, decimals):
    """A number rounded to `decimals`, so DECIMAL, DOUBLE and INTEGER agree.

    The result is always tagged as numeric — including infinities and NaN,
    which are canonicalized rather than turned into bare text, so a column of
    them is not reported as "expected number, got text".
    """
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return str(v)
    if not d.is_finite():
        return ("num", str(d))  # Infinity / -Infinity / NaN, spelled one way
    with localcontext() as ctx:
        ctx.prec = NUMERIC_PRECISION
        try:
            return d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            return ("num", str(d.normalize()))


def _normalize_row(row, decimals):
    return tuple(normalize_cell(v, decimals) for v in row)


def compare(expected, result, sql=None):
    """Judge one run. Returns {"verdict", "label", "reason"} or None (blank cell).

    verdict: PASS (same data) · NEAR (right data, other column/row order) ·
    FAIL (wrong) · NA (no reference) · ? (checker error).
    """
    try:
        return _compare(expected, result, sql)
    except Exception as e:
        return {"verdict": "?", "label": "?",
                "reason": f"checker error: {type(e).__name__}: {e}"}


def _compare(expected, result, sql):
    if result is None or result.status != "ok" or is_exploratory(sql):
        return None
    if expected is None:
        return _verdict("NA", "n/a", "no check for this exercise")

    n_cols, n_expected_cols = len(result.columns), len(expected.columns)
    if n_cols != n_expected_cols:
        return _verdict("FAIL", "FAIL:cols",
                        f"{n_cols} column{'' if n_cols == 1 else 's'}, "
                        f"expected {n_expected_cols}")

    n_expected = len(expected.rows)
    if len(result.raw_rows) > expected.row_cap:
        return _verdict("FAIL", "FAIL:rows",
                        f"more than {expected.row_cap} rows, expected {n_expected}")

    actual = [_normalize_row(r, expected.decimals) for r in result.raw_rows]
    n_actual = len(actual)
    if n_actual != n_expected:
        return _verdict("FAIL", "FAIL:rows",
                        f"{n_actual} rows, expected {n_expected}"
                        + _row_count_hint(actual, expected.rows))

    perm = _matching_permutation(actual, expected.rows, n_cols)
    if perm is None:
        return _mismatch(actual, expected)

    if expected.ordered and not _order_matches(actual, expected, perm):
        return _verdict("NEAR", "NEAR:order", "same rows, different order")
    if perm != tuple(range(n_cols)):
        return _verdict("NEAR", "NEAR:cols", "columns in a different order")
    return _verdict("PASS", "PASS", None)


def _verdict(verdict, label, reason):
    if reason and len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS - 1] + "…"
    return {"verdict": verdict, "label": label, "reason": reason}


def _row_count_hint(actual, expected_rows):
    a, e = set(actual), set(expected_rows)
    if a == e:
        return " (duplicates)"
    if e < a:
        return " (extra rows)"
    if a < e:
        return " (missing rows)"
    return ""


def _matching_permutation(actual, expected_rows, n_cols):
    """The column ordering under which the two row multisets are equal, if any.

    Identity first, so a candidate who used the solution's column order is
    never reported as having reordered them.
    """
    target = Counter(expected_rows)
    identity = tuple(range(n_cols))
    if Counter(actual) == target:
        return identity
    if n_cols < 2 or n_cols > MAX_PERMUTE_COLUMNS:
        return None
    for perm in itertools.permutations(range(n_cols)):
        if perm == identity:
            continue
        if Counter(tuple(row[i] for i in perm) for row in actual) == target:
            return perm
    return None


def _order_matches(actual, expected, perm):
    """Compare only the ORDER BY key columns, so ties may come back either way."""
    if not expected.key_indexes:
        return [tuple(row[i] for i in perm) for row in actual] == expected.rows
    return ([tuple(row[perm[k]] for k in expected.key_indexes) for row in actual]
            == [tuple(row[k] for k in expected.key_indexes) for row in expected.rows])


def _mismatch(actual, expected):
    """No column ordering matched: say what differs, in the identity ordering."""
    for i, (exp_col, act_col) in enumerate(zip(zip(*expected.rows) if expected.rows else [],
                                               zip(*actual) if actual else [])):
        fam_e, fam_a = _family(exp_col), _family(act_col)
        if fam_e != fam_a and "any" not in (fam_e, fam_a):
            name = expected.columns[i] if i < len(expected.columns) else f"#{i + 1}"
            return _verdict("FAIL", "FAIL:vals",
                            f"col {i + 1} ({name}): expected "
                            f"{_FAMILY_LABELS[fam_e]}, got {_FAMILY_LABELS[fam_a]}")

    unmatched_e = list((Counter(expected.rows) - Counter(actual)).elements())
    unmatched_a = list((Counter(actual) - Counter(expected.rows)).elements())
    reason = f"{len(unmatched_e)}/{len(expected.rows)} rows differ"
    if unmatched_e and unmatched_a:
        want = unmatched_e[0]
        # Pair with the closest actual row so the example shows the real
        # difference instead of two unrelated rows (row order is arbitrary).
        got = max(unmatched_a, key=lambda r: sum(1 for x, y in zip(want, r) if x == y))
        reason += f", e.g. expected {_cells(want)} got {_cells(got)}"
    return _verdict("FAIL", "FAIL:vals", reason)


def _family(values):
    """Rough type of a normalized column, for the 'expected text, got number'
    message. Computed AFTER normalization, so a formatted date still reads as a
    date."""
    families = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, tuple) or isinstance(v, (int, float, Decimal)):
            families.add("number")  # tuple = a tagged bool or non-finite number
        elif isinstance(v, str):
            families.add("date" if _ISO_DATE.match(v) else "text")
        else:
            families.add("other")
    if not families:
        return "any"
    if len(families) == 1:
        return families.pop()
    return "text" if families == {"date", "text"} else "other"


def _cells(row):
    return "(" + ", ".join(_cell(v) for v in row) + ")"


def _cell(v):
    if v is None:
        return "NULL"
    if isinstance(v, tuple):  # tagged bool or non-finite number
        return str(v[1])
    return str(v)
