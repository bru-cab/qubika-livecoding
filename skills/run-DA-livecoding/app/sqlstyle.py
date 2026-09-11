"""Interviewer-only SQL good-practice flags for one candidate query.

Deliberately narrow: every rule is either objectively inconsistent (mixed
indentation widths) or a widely cited practice (SELECT *, unaliased expression
columns). Keyword casing is NOT checked — lowercase and uppercase are both
legitimate conventions.

Zero new dependencies: duckdb.tokenize gives the token kinds, which is enough to
avoid the classic false positives (a keyword inside a string literal, COUNT(*)
read as SELECT *). Two tokenizer facts drive the implementation: offsets are
UTF-8 BYTE offsets, and comments emit no token of their own (their bytes fall
inside the previous token's span). So token text is re-read with a regex at each
offset rather than sliced up to the next one.

Nothing here is ever sent to the candidate.
"""
import math
import re
import sys

import guardrails

# Only these letters can appear in the console's style cell, in this order.
CANONICAL_ORDER = "TILS*A"

MAX_LINE_CHARS = 100
ONE_LINE_MIN_CHARS = 90
CLAUSE_KEYWORDS = frozenset("""
    SELECT FROM WHERE JOIN INNER LEFT RIGHT FULL CROSS ON GROUP HAVING ORDER
    LIMIT OFFSET WITH UNION AND OR QUALIFY
""".split())
# A one-liner is only flagged once it really is a multi-clause query.
HEAVY_KEYWORDS = frozenset("JOIN GROUP HAVING".split())
COMPARISON_OPS = (b"<=", b">=", b"<>", b"!=", b"=", b"<", b">")
_WHITESPACE = b" \t\r\n\f\v"

_WORD_RE = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_OP_RE = re.compile(rb"<>|!=|>=|<=|[=<>,*().]")

SEVERITY = {"T": "minor", "I": "minor", "L": "minor", "S": "minor",
            "*": "major", "A": "major"}

_warned = False


def analyze(sql, result_columns=None):
    """-> {"flags": "IS", "issues": [{"code", "severity", "message"}]} or None.

    None means the analyzer itself was unavailable (duckdb missing) or failed;
    it never raises, so a style bug can never affect a candidate's run.
    """
    global _warned
    # Over-long SQL is rejected upstream and is never a real answer, but it
    # still reaches this function. duckdb.tokenize is quadratic in unclosed
    # comment openers, so a pasted 250 KB of "/*" would freeze the interview
    # for minutes while the run lock is held. Refuse to look at it.
    if sql is not None and len(sql) > guardrails.MAX_SQL_CHARS:
        return None
    try:
        found = _issues(sql or "", result_columns)
    except Exception as e:
        if not _warned:
            _warned = True
            print(f"[warn] SQL style check unavailable: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        return None
    # One flag letter per rule, even when a rule has more than one thing to
    # say, so the console cell stays a fixed set of letters.
    merged = {}
    for issue in found:
        merged.setdefault(issue["code"], []).append(issue["message"])
    issues = [{"code": code, "severity": SEVERITY[code],
               "message": "; ".join(merged[code])}
              for code in CANONICAL_ORDER if code in merged]
    return {"flags": "".join(i["code"] for i in issues), "issues": issues}


def _issues(sql, result_columns):
    tokens = _tokens(sql)
    found = []

    def add(code, message):
        found.append({"code": code, "message": message})

    if any("\t" in line[:len(line) - len(line.lstrip())]
           for line in sql.splitlines()):
        add("T", "indented with tabs (use spaces)")

    uneven = _uneven_indent(_indent_widths(sql))
    if uneven:
        add("I", uneven)

    for message in _layout_issues(sql, tokens):
        add("L", message)

    spacing = _spacing_count(sql, tokens)
    if spacing:
        add("S", f"missing space around an operator or comma "
                 f"({spacing} place{'' if spacing == 1 else 's'})")

    if _selects_star(tokens):
        add("*", "SELECT * instead of the columns you need")

    unaliased = [c for c in (result_columns or []) if "(" in c]
    if unaliased:
        add("A", "expression column without an alias: "
                 + ", ".join(unaliased[:3]))

    return found


def _tokens(sql):
    """[(offset, kind, text_bytes or None)] — text re-read at each offset.

    Every token is kept, including the ones whose text no rule reads (string
    and numeric literals) and the ones the regexes cannot read (the '::' cast,
    quoted or non-ASCII identifiers), which carry text None. Dropping them
    would corrupt the "previous token" a rule looks back at: in
    `SELECT 100 * x` the star's real neighbour is the number, not SELECT.
    """
    import duckdb  # lazy, so a missing duckdb is a None result, not an ImportError

    raw = sql.encode("utf-8")
    out = []
    for offset, kind in duckdb.tokenize(sql):
        if offset >= len(raw):
            continue
        name = kind.name
        if name in ("keyword", "identifier"):
            match = _WORD_RE.match(raw, offset)
        elif name == "operator":
            match = _OP_RE.match(raw, offset)
        else:
            match = None  # a literal or a comment: kept only as a placeholder
        out.append((offset, name, match.group(0) if match else None))
    return out


def _indent_widths(sql):
    """Sorted leading-space widths of indented continuation lines.

    Lines that start with a clause keyword are excluded so right-aligned
    ("river") formatting is not reported as inconsistent.
    """
    widths = set()
    for line in sql.expandtabs(4).splitlines()[1:]:
        if not line.strip():
            continue
        width = len(line) - len(line.lstrip(" "))
        if width == 0:
            continue
        first = line.strip().split(None, 1)[0].lstrip("(").upper()
        if first in CLAUSE_KEYWORDS:
            continue
        widths.add(width)
    return sorted(widths)


def _uneven_indent(widths):
    """The message for genuinely sloppy indentation, or "".

    Only two patterns count, both objectively inconsistent rather than a
    matter of taste: indenting by a single space, and two indent levels that
    differ by one space. Deeper alignment (a river layout, arguments lined up
    under an opening paren) uses large, unrelated widths on purpose and is
    left alone.
    """
    if len(widths) < 2:
        return ""
    listed = ", ".join(str(w) for w in widths)
    if widths[0] < 2:
        return f"one-space indentation mixed with others (widths {listed})"
    if any(b - a == 1 for a, b in zip(widths, widths[1:])):
        return f"indent levels one space apart (widths {listed})"
    return ""


def _layout_issues(sql, tokens):
    messages = []
    longest = max((len(l.rstrip()) for l in sql.expandtabs(4).splitlines()),
                  default=0)
    if longest > MAX_LINE_CHARS:
        messages.append(f"line of {longest} characters (over {MAX_LINE_CHARS})")

    stripped = sql.strip()
    if stripped and "\n" not in stripped:
        clauses = {t.decode("ascii", "ignore").upper() for _, kind, t in tokens
                   if kind == "keyword" and t} & CLAUSE_KEYWORDS
        if clauses & HEAVY_KEYWORDS or len(stripped) >= ONE_LINE_MIN_CHARS:
            messages.append("whole query on one line")
    return messages


def _spacing_count(sql, tokens):
    """Comparison operators and commas run together with their neighbours."""
    raw = sql.encode("utf-8")
    count = 0
    for offset, kind, text in tokens:
        if kind != "operator" or (text not in COMPARISON_OPS and text != b","):
            continue
        after = raw[offset + len(text):offset + len(text) + 1]
        if after and after not in _WHITESPACE:
            count += 1
            continue
        if text == b",":
            continue  # nothing is expected before a comma
        before = raw[offset - 1:offset] if offset else b""
        if before and before not in _WHITESPACE:
            count += 1
    return count


def _starts_a_select_item(token):
    """True for the token a select-list item can begin right after."""
    if token is None:
        return False
    _, kind, text = token
    if kind == "operator":
        return text == b","
    return (kind == "keyword" and text is not None
            and text.decode("ascii", "ignore").upper() in ("SELECT", "DISTINCT"))


def _selects_star(tokens):
    """`SELECT *` / `SELECT a, t.*` — never `COUNT(*)` or `100 * x`.

    A star only counts where a select-list item begins, so multiplication
    (whose left operand is a value) and a star inside a call (preceded by
    '(') are both left alone.
    """
    for i, (_, kind, text) in enumerate(tokens):
        if kind != "operator" or text != b"*":
            continue
        if _starts_a_select_item(tokens[i - 1] if i else None):
            return True
        # t.* / schema.t.* — walk back over the qualifier
        j = i
        while j >= 2 and tokens[j - 1][2] == b"." and tokens[j - 2][1] == "identifier":
            j -= 2
        if j != i and _starts_a_select_item(tokens[j - 1] if j else None):
            return True
    return False
