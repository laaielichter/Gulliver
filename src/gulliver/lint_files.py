"""Lint files and folders for CI: positions, inline ignores, [lint] config and same-rows fixes per file.

`gulliver lint "SQL"` (one string) is unchanged. This module adds `gulliver lint PATH...`:

  - walks folders for *.sql (skips hidden folders, target/, dbt_packages/, node_modules/)
  - splits each file into statements with the sqlglot tokenizer (a ';' inside a string or comment is not a split)
  - renders dbt Jinja ({{ ref() }}, {{ source() }}, {% ... %}) like lineage.render, and maps every position in
    the rendered SQL back to the original file, so line and column point at the file you edit
  - lints read statements (SELECT, and the SELECT part of CREATE/INSERT ... SELECT); other statements
    (DDL, DELETE, ...) are counted as skipped, not reported: migrations folders are normal
  - inline ignores:  -- gulliver: ignore GV001,GV006   (this line; on a comment-only line: the next line, and
                                                        above a statement: the whole statement)
                     -- gulliver: ignore-file GV004    (the whole file; no rule list means every rule)
  - [lint] in gulliver.toml: disable, severity overrides, fail_on, exclude, dialect, warn_unused_ignores
  - fixes: the same-rows rewrites of fixer.rewrite, placed as exact text edits in the file and checked by
    re-parsing (the edited statement must parse to the same tree as the fixer's rewrite, or the edit is dropped)

Two meta rules exist only here: GV900 parse-error (a statement Gulliver could not parse, so it was not checked)
and GV901 unused-ignore (an unreadable rule list always; unused ignores only with --warn-unused-ignores).
"""

from __future__ import annotations

import bisect
import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

from .config import ConfigError, find_config_file, table_section
from .lineage import BLOCK_RE, COMMENT_RE, CONFIG_RE, EXPR_RE, REF_RE, SOURCE_RE, THIS_RE
from .lint import RULES, SEVERITY_ORDER, Finding, lint
from .sqlsafety import classify

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

META_RULES = {"GV900": "parse-error", "GV901": "unused-ignore"}
SEVERITIES = ("error", "warn", "info")
FAIL_ON = ("error", "warn", "info", "never")
DEFAULT_EXCLUDE_DIRS = ("target", "dbt_packages", "node_modules", "__pycache__")
DIALECTS = ("duckdb", "postgres", "bigquery", "snowflake")


def rule_name(rule: str) -> str:
    return RULES.get(rule) or META_RULES.get(rule) or rule


# -- [lint] config ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class LintConfig:
    disable: frozenset[str] = frozenset()
    severity: dict[str, str] = field(default_factory=dict)
    fail_on: str = "error"
    exclude: tuple[str, ...] = ()
    dialect: str | None = None
    warn_unused_ignores: bool = False
    baseline: str | None = None   # path of the baseline file (made relative to gulliver.toml by load_lint_config)

    def as_dict(self) -> dict[str, Any]:
        return {"disable": sorted(self.disable), "severity": dict(self.severity), "fail_on": self.fail_on,
                "exclude": list(self.exclude), "dialect": self.dialect,
                "warn_unused_ignores": self.warn_unused_ignores, "baseline": self.baseline}


_LINT_KEYS = {"disable", "severity", "fail_on", "exclude", "dialect", "warn_unused_ignores", "baseline"}


def _rule_id(token: str) -> str | None:
    """GV001, gv001 or select-star -> GV001; None when the rule does not exist.

    A well-formed id that no rule uses (GV099, a typo for GV009) is not a rule:
    letting it through as an id made `disable = ["GV099"]` load fine and turn
    nothing off, so the warning kept showing and nothing said why.
    """
    t = token.strip()
    if re.fullmatch(r"(?i)GV\d+", t):
        rid = t.upper()
        return rid if rid in RULES or rid in META_RULES else None
    by_name = {name: rid for rid, name in {**RULES, **META_RULES}.items()}
    return by_name.get(t.lower())


def lint_config_from_mapping(data: dict[str, Any], base_dir: Path | None = None) -> LintConfig:
    unknown = set(data) - _LINT_KEYS
    if unknown:
        raise ConfigError(f"Unknown [lint] keys: {', '.join(sorted(unknown))}. Known: {', '.join(sorted(_LINT_KEYS))}")

    def rules(values: Any, key: str) -> list[str]:
        if isinstance(values, str):
            values = [values]
        out = []
        for v in values or []:
            rid = _rule_id(str(v))
            if rid is None:
                raise ConfigError(f"[lint] {key}: unknown rule '{v}'. Run `gulliver rules` for the list.")
            out.append(rid)
        return out

    severity: dict[str, str] = {}
    for key, value in dict(data.get("severity") or {}).items():
        rid = rules([key], "severity")[0]
        level = str(value).lower()
        if level == "warning":
            level = "warn"
        if level not in SEVERITIES:
            raise ConfigError(f"[lint] severity {key} = '{value}': use error, warn or info.")
        severity[rid] = level
    fail_on = str(data.get("fail_on", "error")).lower()
    if fail_on not in FAIL_ON:
        raise ConfigError(f"[lint] fail_on = '{fail_on}': use error, warn, info or never.")
    exclude = data.get("exclude") or []
    if isinstance(exclude, str):
        exclude = [exclude]
    dialect = data.get("dialect")
    if dialect is not None and str(dialect).lower() not in DIALECTS:
        raise ConfigError(f"[lint] dialect = '{dialect}': use {', '.join(DIALECTS)}.")
    baseline = data.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, str) or not baseline.strip():
            raise ConfigError('[lint] baseline: give a file path, e.g. "gulliver-baseline.json".')
        # relative to gulliver.toml, so the same setting works from any folder
        if base_dir is not None and not Path(baseline).is_absolute():
            baseline = str(base_dir / baseline)
    return LintConfig(disable=frozenset(rules(data.get("disable"), "disable")), severity=severity, fail_on=fail_on,
                      exclude=tuple(str(e) for e in exclude),
                      dialect=str(dialect).lower() if dialect else None,
                      warn_unused_ignores=bool(data.get("warn_unused_ignores", False)), baseline=baseline)


def load_lint_config(config_path: str | os.PathLike[str] | None = None) -> LintConfig:
    """The [lint] section of gulliver.toml (same file lookup as load_settings); defaults without one."""
    path = find_config_file(config_path)
    if path is None:
        return LintConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return lint_config_from_mapping(table_section(data, "lint"), path.resolve().parent)


# -- finding files ------------------------------------------------------------------------------------
def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _excluded(rel: str, patterns: Iterable[str]) -> bool:
    parts = rel.split("/")
    for raw in patterns:
        pat = raw.strip().rstrip("/")
        if not pat:
            continue
        pat = pat[2:] if pat.startswith("./") else pat
        candidates = {pat, pat[3:] if pat.startswith("**/") else pat}
        for p in candidates:
            if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p + "/*"):
                return True
            if "/" not in p and any(fnmatch.fnmatch(part, p) for part in parts):
                return True
            if "/" in p and any(fnmatch.fnmatch("/".join(parts[i:]), p) or fnmatch.fnmatch("/".join(parts[i:]), p + "/*")
                                for i in range(1, len(parts))):
                return True
    return False


def discover(paths: Iterable[str], exclude: Iterable[str] = ()) -> tuple[list[Path], list[str]]:
    """Files to lint (sorted, unique) and paths that do not exist. Folders give their *.sql files.

    `exclude` globs apply to named files too (pre-commit passes file names). Hidden folders, target/,
    dbt_packages/ and node_modules/ are skipped only while walking a folder."""
    exclude = list(exclude)
    files: dict[str, Path] = {}
    missing: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            if not _excluded(display_path(path), exclude):
                files.setdefault(str(path.resolve()), path)
        elif path.is_dir():
            for root, dirs, names in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in DEFAULT_EXCLUDE_DIRS
                                 and not _excluded(display_path(Path(root) / d), exclude))
                for name in sorted(names):
                    if name.lower().endswith(".sql"):
                        file = Path(root) / name
                        if not _excluded(display_path(file), exclude):
                            files.setdefault(str(file.resolve()), file)
        else:
            missing.append(raw)
    return sorted(files.values(), key=display_path), missing


# -- dbt Jinja with a position map --------------------------------------------------------------------
_JINJA = re.compile("|".join(f"(?P<{name}>{rx.pattern})" for name, rx in (
    ("comment", COMMENT_RE), ("config", CONFIG_RE), ("ref", REF_RE), ("source", SOURCE_RE), ("this", THIS_RE),
    ("block", BLOCK_RE), ("expr", EXPR_RE))), re.S)


@dataclass
class Rendered:
    """SQL with the Jinja replaced, and a map from rendered offsets back to the original file."""

    text: str
    source: str
    regions: list[tuple[int, int, int, int]] = field(default_factory=list)  # (r_start, r_end, s_start, s_end)

    def to_source(self, offset: int, end: bool = False) -> int:
        """Rendered offset -> source offset (inside a replaced Jinja region: its start).

        end=True maps the end of a span: an offset right where a Jinja region starts stays before that region
        (a region that renders to nothing, like {% if %} or {# #}, has r_start == r_end and would otherwise
        map past the Jinja, so an edit ending there would swallow it)."""
        delta = 0
        for r_start, r_end, s_start, s_end in self.regions:
            if offset < r_start or (end and offset == r_start):
                break
            if offset < r_end:
                return s_start
            delta = s_end - r_end
        return offset + delta

    def clean(self, r_start: int, r_end: int) -> bool:
        """True when [r_start, r_end) touches no Jinja: the text is the same in the original file."""
        return all(r_end <= rs or r_start >= re_ for rs, re_, _, _ in self.regions)


def render_jinja(source: str, model_name: str = "this") -> Rendered:
    if "{{" not in source and "{%" not in source and "{#" not in source:
        return Rendered(source, source)
    out: list[str] = []
    regions: list[tuple[int, int, int, int]] = []
    pos = 0
    length = 0
    for m in _JINJA.finditer(source):
        out.append(source[pos:m.start()])
        length += m.start() - pos
        kind = m.lastgroup
        if kind == "ref":
            # group numbering inside the combined pattern: find the ref's own groups by re-matching
            inner = REF_RE.fullmatch(m.group(0))
            repl = (inner.group(2) or inner.group(1)) if inner else "NULL"
        elif kind == "source":
            inner = SOURCE_RE.fullmatch(m.group(0))
            repl = f"{inner.group(1)}.{inner.group(2)}" if inner else "NULL"
        elif kind == "this":
            repl = model_name
        elif kind == "expr":
            repl = "NULL"
        else:  # comment, config, block
            repl = ""
        regions.append((length, length + len(repl), m.start(), m.end()))
        out.append(repl)
        length += len(repl)
        pos = m.end()
    out.append(source[pos:])
    return Rendered("".join(out), source, regions)


# -- statements -----------------------------------------------------------------------------------------
@dataclass
class Chunk:
    index: int          # 1-based statement number in the file
    start: int          # rendered offset of the first token
    end: int            # rendered offset after the last token
    tokens: list[Token]

    def text(self, rendered: str) -> str:
        return rendered[self.start:self.end]


def split_statements(text: str, dialect: str) -> list[Chunk]:
    tokens = Dialect.get_or_raise(dialect).tokenize(text)
    chunks: list[Chunk] = []
    current: list[Token] = []
    for tok in tokens:
        if tok.token_type == TokenType.SEMICOLON:
            if current:
                chunks.append(Chunk(len(chunks) + 1, current[0].start, current[-1].end + 1, current))
            current = []
        else:
            current.append(tok)
    if current:
        chunks.append(Chunk(len(chunks) + 1, current[0].start, current[-1].end + 1, current))
    return chunks


def _meta(node: exp.Expression) -> dict | None:
    meta = getattr(node, "_meta", None)
    return meta if meta and "start" in meta else None


def _leaf_span(nodes: Iterable[exp.Expression]) -> tuple[int, int] | None:
    starts, ends = [], []
    for root in nodes:
        for n in root.walk():
            m = _meta(n)
            if m:
                starts.append(m["start"])
                ends.append(m["end"] + 1)
    return (min(starts), max(ends)) if starts else None


_JOIN_WORDS = {getattr(TokenType, n) for n in ("JOIN", "CROSS", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "NATURAL",
                                               "SEMI", "ANTI", "ASOF", "POSITIONAL", "LATERAL", "COMMA")
               if hasattr(TokenType, n)}


def _back_up(tokens: list[Token], start: int, words: set, stop_after: set | None = None) -> int:
    """Move a start offset back over the keyword tokens just before it (JOIN, SELECT, ORDER BY ...)."""
    idx = next((i for i, t in enumerate(tokens) if t.start >= start), len(tokens))
    while idx > 0 and tokens[idx - 1].token_type in words:
        idx -= 1
        if stop_after and tokens[idx].token_type in stop_after:
            break
    return tokens[idx].start if idx < len(tokens) else start


def node_span(node: exp.Expression | None, chunk: Chunk) -> tuple[int, int]:
    """Rendered [start, end) of the code a finding is about; the statement start when unknown."""
    base = chunk.start
    tokens = chunk.tokens
    fallback = (chunk.start, chunk.tokens[0].end + 1)
    if node is None:
        return fallback
    if isinstance(node, exp.Select):
        span = _leaf_span(node.expressions)
        if span is None:
            return fallback
        start = _back_up(tokens, span[0] + base, {TokenType.SELECT, TokenType.DISTINCT, TokenType.ALL},
                         {TokenType.SELECT})
        return start, span[1] + base
    if isinstance(node, exp.Union):
        left = _leaf_span([node.this])
        if left is not None:
            tok = next((t for t in tokens if t.start >= left[1] + base and t.token_type == TokenType.UNION), None)
            if tok is not None:
                return tok.start, tok.end + 1
    span = _leaf_span([node])
    if span is None:
        return fallback
    start, end = span[0] + base, span[1] + base
    if isinstance(node, exp.Join):
        start = _back_up(tokens, start, _JOIN_WORDS, {TokenType.COMMA})
    elif isinstance(node, exp.Order):
        start = _back_up(tokens, start, {TokenType.ORDER_BY})
    return start, end


# -- results ----------------------------------------------------------------------------------------------
@dataclass
class FileFinding:
    path: str
    rule: str
    severity: str
    message: str
    fix: str
    line: int
    col: int
    end_line: int
    end_col: int
    statement: int
    snippet: str
    ignored_by: str | None = None   # "line 3" / "statement" / "file" / "config" when ignored

    @property
    def name(self) -> str:
        return rule_name(self.rule)

    def as_dict(self) -> dict[str, Any]:
        out = {"path": self.path, "rule": self.rule, "name": self.name, "severity": self.severity,
               "message": self.message, "fix": self.fix,
               "position": {"line": self.line, "col": self.col, "end_line": self.end_line, "end_col": self.end_col},
               "statement": self.statement}
        if self.ignored_by:
            out["ignored_by"] = self.ignored_by
        return out


@dataclass
class Edit:
    start: int      # source offsets [start, end)
    end: int
    line: int
    col: int
    end_line: int
    end_col: int
    before: str
    after: str


@dataclass
class FileFix:
    path: str
    statement: int
    rules: list[str]
    edits: list[Edit]
    notes: list[str]
    placed: bool        # False: a same-rows rewrite exists, but Gulliver could not place it as an exact edit

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "statement": self.statement, "rules": self.rules, "guarantee": "same",
                "meaning": "returns exactly the same rows", "placed": self.placed,
                "edits": [{"line": e.line, "col": e.col, "end_line": e.end_line, "end_col": e.end_col,
                           "before": e.before, "after": e.after} for e in self.edits],
                "notes": self.notes}


@dataclass
class FileResult:
    path: str
    file: Path
    source: str = ""
    statements: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    findings: list[FileFinding] = field(default_factory=list)
    ignored: list[FileFinding] = field(default_factory=list)
    fixes: list[FileFix] = field(default_factory=list)
    bom: bool = False   # the file starts with a UTF-8 BOM (not part of `source`)


class _Lines:
    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def line_col(self, offset: int) -> tuple[int, int]:
        i = bisect.bisect_right(self.starts, offset) - 1
        return i + 1, offset - self.starts[i] + 1

    def line_text(self, line: int) -> str:
        start = self.starts[line - 1]
        end = self.starts[line] - 1 if line < len(self.starts) else len(self.text)
        return self.text[start:end].rstrip("\r")


# -- inline ignores -----------------------------------------------------------------------------------------
_DIRECTIVE = re.compile(r"(?:--|/\*|\{#|#)\s*gulliver:\s*(ignore-file|ignore)\b([^\n]*)", re.I)
_COMMENT_START = re.compile(r"^\s*(--|/\*|\{#|#)")


@dataclass
class Directive:
    kind: str           # "ignore" | "ignore-file"
    line: int
    col: int
    rules: list[str]    # empty: every rule
    comment_only: bool
    used: set[str] = field(default_factory=set)
    invalid: str | None = None   # a rule list Gulliver cannot read: the directive ignores nothing


def _looks_like_rule(word: str, first: bool) -> bool:
    """A word that was meant as a rule id or name (GV-001, select_star, cartesian_join), not a reason."""
    import difflib

    if re.match(r"(?i)gv\W*\w", word):
        return True
    if not first:
        return False
    names = [n for n in {**RULES, **META_RULES}.values()]
    norm = word.lower().replace("_", "-")
    return "_" in word or bool(difflib.get_close_matches(norm, names, n=1, cutoff=0.8))


def parse_directives(lines: _Lines, code_spans: list[tuple[int, int]] = ()) -> list[Directive]:
    """`gulliver: ignore` comments. A match inside a SQL token (a string literal, a quoted name) in
    `code_spans` (source offsets) is not a comment and is skipped."""
    out: list[Directive] = []
    starts = [a for a, _ in code_spans]
    for m in _DIRECTIVE.finditer(lines.text):
        i = bisect.bisect_right(starts, m.start()) - 1
        if i >= 0 and code_spans[i][0] <= m.start() < code_spans[i][1]:
            continue
        line, col = lines.line_col(m.start())
        rules: list[str] = []
        invalid: str | None = None
        rest = m.group(2).replace("*/", " ").replace("#}", " ")
        if rest[:1] and not rest[:1].isspace():
            invalid = f"write the rules after a space (`gulliver: {m.group(1).lower()} GV001`), not '{rest.strip()}'"
        words = [w for w in re.split(r"[\s,]+", rest.strip()) if w]
        if invalid is None and words and words[0].lower() in ("all", "*"):
            words = []
        for n, word in enumerate(words if invalid is None else []):
            rid = _rule_id(word)
            if rid is None:
                if _looks_like_rule(word, first=n == 0):
                    invalid = f"unknown rule '{word}' (run `gulliver rules` for the list)"
                break  # the rest is a free-text reason
            if rid not in RULES and rid not in META_RULES:
                invalid = f"unknown rule '{word}' (run `gulliver rules` for the list)"
                break
            rules.append(rid)
        out.append(Directive(m.group(1).lower(), line, col, rules,
                             bool(_COMMENT_START.match(lines.line_text(line))), invalid=invalid))
    return out


def _covers(d: Directive, rule: str) -> bool:
    if d.invalid is not None:
        return False
    return not d.rules or rule in d.rules


def _apply_ignores(found: list[FileFinding], directives: list[Directive], lines: _Lines,
                   statement_lines: dict[int, int]) -> tuple[list[FileFinding], list[FileFinding]]:
    """Split findings into (kept, ignored) and mark which directives were used."""
    by_line: dict[int, list[Directive]] = {}
    for d in directives:
        if d.kind == "ignore":
            by_line.setdefault(d.line, []).append(d)

    def comment_block_above(line: int) -> list[Directive]:
        out: list[Directive] = []
        n = line - 1
        while n >= 1 and _COMMENT_START.match(lines.line_text(n)):
            out.extend(d for d in by_line.get(n, []) if d.comment_only)
            n -= 1
        return out

    kept: list[FileFinding] = []
    ignored: list[FileFinding] = []
    for f in found:
        hit: tuple[Directive, str] | None = None
        for d in directives:
            if d.kind == "ignore-file" and _covers(d, f.rule):
                hit = (d, "file")
                break
        if hit is None:
            for d in by_line.get(f.line, []):
                if _covers(d, f.rule):
                    hit = (d, f"line {d.line}")
                    break
        if hit is None:
            for d in by_line.get(f.line - 1, []):
                if d.comment_only and _covers(d, f.rule):
                    hit = (d, f"line {d.line}")
                    break
        if hit is None and f.statement in statement_lines:
            for d in comment_block_above(statement_lines[f.statement]):
                if _covers(d, f.rule):
                    hit = (d, "statement")
                    break
        if hit is None:
            kept.append(f)
        else:
            hit[0].used.add(f.rule)
            f.ignored_by = hit[1]
            ignored.append(f)
    return kept, ignored


# -- metadata (optional) -------------------------------------------------------------------------------------
class Metadata:
    """Table metadata from a connection (free: no data is read), cached per table ref."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.cache: dict[str, dict | None] = {}
        self.notes: list[str] = []
        self.missing: list[str] = []   # tables the connection does not have (no metadata for them)

    def for_tables(self, refs: Iterable[str]) -> dict[str, dict] | None:
        from .engines import EngineError

        out: dict[str, dict] = {}
        for ref in refs:
            key = ref.lower()
            if key not in self.cache:
                try:
                    info = self.engine.table_info(ref)
                    self.cache[key] = info.as_dict(with_columns=True) if info is not None else None
                    if info is None:
                        self.missing.append(ref)
                except EngineError as exc:
                    self.cache[key] = None
                    self.notes.append(f"{ref}: {exc}")
            if self.cache[key] is not None:
                out[key] = self.cache[key]  # type: ignore[assignment]
        return out or None


# -- one file ---------------------------------------------------------------------------------------------
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)


def _balanced_span(tokens: list[Token], start: int, end: int) -> tuple[int, int] | None:
    """Grow [start, end) over brackets so it covers whole tokens with balanced parentheses."""
    idx = [i for i, t in enumerate(tokens) if t.start >= start and t.end < end]
    if not idx:
        return None
    i, j = idx[0], idx[-1]

    def depth(a: int, b: int) -> tuple[int, int]:
        opened = closed = low = 0
        for t in tokens[a:b + 1]:
            if t.token_type == TokenType.L_PAREN:
                opened += 1
            elif t.token_type == TokenType.R_PAREN:
                closed += 1
                low = min(low, opened - closed)
        return opened - closed, low

    balance, low = depth(i, j)
    while low < 0 and i > 0 and tokens[i - 1].token_type == TokenType.L_PAREN:
        i -= 1
        balance, low = depth(i, j)
    while balance > 0 and j + 1 < len(tokens) and tokens[j + 1].token_type == TokenType.R_PAREN:
        j += 1
        balance, low = depth(i, j)
    if balance != 0 or low < 0:
        return None
    return tokens[i].start, tokens[j].end + 1


def _locate_changes(chunk: Chunk, chunk_sql: str, dialect: str, table_info: dict[str, dict] | None,
                    changes: list[dict[str, Any]]) -> list[tuple[int, int, str]] | None:
    """Rendered (start, end, new text) for each change, or None when one cannot be placed.

    GV005 range rewrites are replayed on a fresh parse so the replaced comparison nodes (and their token
    positions) are known; other rewrites are found by matching tokens. The caller proves the result."""
    from .fixer import _fix_ranges, _Log, table_columns

    spans: dict[str, list[tuple[int, int]]] = {}
    statement = classify(chunk_sql, dialect)
    if statement.select is not None:
        before_nodes = list(statement.select.find_all(*_COMPARISONS))
        try:
            _fix_ranges(statement.select, dialect, table_columns(table_info), "safe", _Log())
        except Exception:
            pass
        alive = {id(n) for n in statement.tree.walk()}
        for node in before_nodes:
            if id(node) in alive:
                continue
            leaf = _leaf_span([node])
            if leaf is None:
                continue
            span = _balanced_span(chunk.tokens, leaf[0] + chunk.start, leaf[1] + chunk.start)
            if span is not None:
                spans.setdefault(node.sql(dialect=dialect), []).append(span)
    tokenizer = Dialect.get_or_raise(dialect)

    def key(tok: Token) -> tuple[Any, str]:
        return tok.token_type, tok.text.upper()

    toks = chunk.tokens
    edits: list[tuple[int, int, str]] = []
    for before in dict.fromkeys(c["before"] for c in changes):
        afters = {c["after"] for c in changes if c["before"] == before}
        wanted = sum(1 for c in changes if c["before"] == before)
        hits = spans.get(before) or []
        if len(hits) != wanted:
            try:
                pattern = [key(t) for t in tokenizer.tokenize(before)]
            except SqlglotError:
                return None
            hits = [(toks[i].start, toks[i + len(pattern) - 1].end + 1)
                    for i in range(len(toks) - len(pattern) + 1)
                    if [key(t) for t in toks[i:i + len(pattern)]] == pattern]
        if len(afters) != 1 or len(hits) != wanted:
            return None
        edits.extend((s, e, next(iter(afters))) for s, e in hits)
    return edits



def _has_comment(text: str, tokens: list[Token], start: int, end: int) -> bool:
    """True when [start, end) holds more than tokens and whitespace (a comment between its tokens)."""
    inside = [t for t in tokens if t.start >= start and t.end < end]
    pos = start
    for t in inside:
        if text[pos:t.start].strip():
            return True
        pos = t.end + 1
    return bool(text[pos:end].strip())


def _place_fix(result: FileResult, rendered: Rendered, lines: _Lines, chunk: Chunk, chunk_sql: str,
               dialect: str, table_info: dict[str, dict] | None) -> None:
    from .fixer import SAME, rewrite

    try:
        out = rewrite(chunk_sql, dialect, mode="safe", table_info=table_info)
    except Exception:  # a fix hint must never break lint
        return
    changes = [c for c in out.get("changes") or [] if c.get("guarantee") == SAME]
    if not changes or len(changes) != len(out.get("changes") or []):
        return
    rules = sorted({c["rule"] for c in changes})

    def fail(note: str) -> None:
        result.fixes.append(FileFix(result.path, chunk.index, rules, [], [note], placed=False))

    located = _locate_changes(chunk, chunk_sql, dialect, table_info, changes)
    if located is None:
        return fail("Could not find the exact text of each rewrite in the file; run `gulliver fix` on this "
                    "statement and edit it by hand.")
    edits = located
    edits.sort()
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:], strict=False)):
        return fail("Rewrites overlap; run `gulliver fix` on this statement.")
    if not all(rendered.clean(s, e) for s, e, _ in edits):
        return fail("The rewrite touches dbt Jinja; edit it by hand.")
    if any(_has_comment(rendered.text, chunk.tokens, s, e) for s, e, _ in edits):
        return fail("The rewrite would remove a comment inside the changed code; edit it by hand.")
    # Proof that the text edits are the fixer's rewrite: the edited statement parses to the same tree.
    edited = chunk_sql
    for s, e, after in reversed(edits):
        edited = edited[:s - chunk.start] + after + edited[e - chunk.start:]
    try:
        same = classify(edited, dialect).tree.sql(dialect=dialect) == classify(out["fixed_sql"], dialect).tree.sql(
            dialect=dialect)
    except Exception:
        same = False
    if not same:
        return fail("The text edit did not reproduce the rewrite exactly; run `gulliver fix` on this statement.")
    placed: list[Edit] = []
    for s, e, after in edits:
        ss, se = rendered.to_source(s), rendered.to_source(e, end=True)
        if rendered.source[ss:se] != rendered.text[s:e]:
            return fail("The rewrite touches dbt Jinja; edit it by hand.")
        (l1, c1), (l2, c2) = lines.line_col(ss), lines.line_col(se)
        placed.append(Edit(ss, se, l1, c1, l2, c2, rendered.source[ss:se], after))
    notes = [c["note"] for c in changes]
    result.fixes.append(FileFix(result.path, chunk.index, rules, placed, notes, placed=True))


def lint_file(file: Path, dialect: str = "duckdb", config: LintConfig | None = None,
              metadata: Metadata | None = None, with_fixes: bool = True,
              warn_unused_ignores: bool | None = None) -> FileResult:
    config = config or LintConfig()
    unused_flag = config.warn_unused_ignores if warn_unused_ignores is None else warn_unused_ignores
    result = FileResult(display_path(file), file)
    try:
        # newline="": keep \r\n so edits and the written file keep the original line endings
        with open(file, encoding="utf-8", newline="") as fh:
            source = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        result.findings.append(FileFinding(result.path, "GV900", config.severity.get("GV900", "error"),
                                           f"Could not read the file: {exc}", "Save it as UTF-8 text.",
                                           1, 1, 1, 1, 0, ""))
        return result
    if source.startswith("\ufeff"):  # a UTF-8 BOM (Windows editors): not SQL; fix --write puts it back
        result.bom = True
        source = source[1:]
    result.source = source
    lines = _Lines(source)
    rendered = render_jinja(source, file.stem.lower())
    uses_this = bool(THIS_RE.search(source))
    found: list[FileFinding] = []

    def add(rule: str, severity: str, message: str, fix: str, r_start: int, r_end: int, statement: int) -> None:
        if rule in config.disable:
            return
        severity = config.severity.get(rule, severity)
        s, e = rendered.to_source(r_start), rendered.to_source(max(r_end, r_start), end=True)
        (l1, c1), (l2, c2) = lines.line_col(s), lines.line_col(max(e, s))
        found.append(FileFinding(result.path, rule, severity, message, fix, l1, c1, l2, c2, statement,
                                 lines.line_text(l1).strip()))

    try:
        chunks = split_statements(rendered.text, dialect)
    except SqlglotError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "tokenizer error"
        add("GV900", "warn", f"Could not read the SQL for dialect '{dialect}': {first}. Nothing in this file was "
            "checked.", "Check the dialect (--dialect or [lint] dialect), or fix the syntax.", 0, 0, 0)
        chunks = []
    statement_lines: dict[int, int] = {}
    for chunk in chunks:
        result.statements += 1
        statement_lines[chunk.index] = lines.line_col(rendered.to_source(chunk.start))[0]
        chunk_sql = chunk.text(rendered.text)
        try:
            statement = classify(chunk_sql, dialect)
        except Exception as exc:  # one odd statement must not stop a CI run
            statement = None
            reason = f"Could not parse SQL for dialect '{dialect}': {type(exc).__name__}"
        else:
            reason = statement.reason
        if statement is None or statement.kind == "block" or statement.select is None:
            if reason.startswith("Could not parse"):
                add("GV900", "warn", f"{reason.rstrip('.')}. This statement was not checked.",
                    "Check the dialect (--dialect or [lint] dialect), or the Jinja in this model.",
                    chunk.start, chunk.tokens[0].end + 1, chunk.index)
            else:
                line = statement_lines[chunk.index]
                result.skipped.append({"statement": chunk.index, "line": line, "reason": reason or "not a read"})
            continue
        # {{ this }} renders to the model's own name, which the warehouse need not have yet
        refs = [t for t in statement.tables if not (uses_this and t.lower() == file.stem.lower())]
        table_info = metadata.for_tables(refs) if metadata is not None else None
        try:
            findings: list[Finding] = lint(statement.select, dialect, table_info)
        except Exception as exc:
            add("GV900", "warn", f"Gulliver could not lint this statement ({type(exc).__name__}: {exc}).",
                "Report this as a Gulliver bug.", chunk.start, chunk.tokens[0].end + 1, chunk.index)
            continue
        for f in findings:
            s, e = node_span(f.node, chunk)
            add(f.rule, f.severity, f.message, f.fix, s, e, chunk.index)
        if with_fixes and findings:
            _place_fix(result, rendered, lines, chunk, chunk_sql, dialect, table_info)

    code_spans = sorted((rendered.to_source(t.start), rendered.to_source(t.end + 1, end=True))
                        for chunk in chunks for t in chunk.tokens)
    directives = parse_directives(lines, code_spans)
    kept, ignored = _apply_ignores(found, directives, lines, statement_lines)
    # Fixes only for findings that survived [lint] disable and the ignore comments.
    kept_rules = {(f.statement, f.rule) for f in kept}
    result.fixes = [x for x in result.fixes if all((x.statement, r) in kept_rules for r in x.rules)]
    for d in directives:
        if d.invalid is not None and "GV901" not in config.disable:
            kept.append(FileFinding(result.path, "GV901", config.severity.get("GV901", "warn"),
                                    f"This `gulliver: {d.kind}` comment ignores nothing: {d.invalid}.",
                                    "Use rule ids or names from `gulliver rules`, separated by spaces or commas.",
                                    d.line, d.col, d.line, d.col, 0, lines.line_text(d.line).strip()))
    if unused_flag:
        for d in directives:
            if d.invalid is not None:
                continue
            unused = [r for r in d.rules if r not in d.used] if d.rules else ([] if d.used else ["all rules"])
            if unused and "GV901" not in config.disable:
                kept.append(FileFinding(result.path, "GV901", config.severity.get("GV901", "warn"),
                                        f"This `gulliver: {d.kind}` comment ignores nothing: {', '.join(unused)}.",
                                        "Remove the comment or the rule ids that no longer apply.",
                                        d.line, d.col, d.line, d.col, 0, lines.line_text(d.line).strip()))
    kept.sort(key=lambda f: (f.line, f.col, SEVERITY_ORDER.get(f.severity, 9), f.rule))
    result.findings = kept
    result.ignored = ignored
    return result


# -- baseline ------------------------------------------------------------------------------------------------
BASELINE_VERSION = 1


def fingerprint_base(path: str, rule: str, snippet: str) -> str:
    """Stable across line moves: path + rule + the whitespace-normalised source line."""
    return hashlib.sha1(f"{path}\0{rule}\0{' '.join(snippet.split())}".encode(), usedforsecurity=False).hexdigest()


def fingerprint(f: FileFinding, seen: dict[str, int]) -> str:
    """fingerprint_base, plus a counter so repeats of the same line get distinct ids (SARIF, GitLab)."""
    base = fingerprint_base(f.path, f.rule, f.snippet)
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else hashlib.sha1(f"{base}:{seen[base]}".encode(), usedforsecurity=False).hexdigest()


def _baseline_path(file: Path, root: Path) -> str:
    """The file's path relative to the baseline's folder, with forward slashes (same on Windows and Linux)."""
    resolved = file.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        if resolved.drive == root.drive:
            return Path(os.path.relpath(resolved, root)).as_posix()
        return resolved.as_posix()


def _baseline_key(path: str, f: FileFinding) -> tuple[str, str, str]:
    return path, f.rule, fingerprint_base(path, f.rule, f.snippet)


def baseline_entries(results: list[FileResult], root: Path) -> list[dict[str, Any]]:
    """One entry per (path, rule, fingerprint) with a count. No SQL text. Sorted, so the file is stable."""
    counts: dict[tuple[str, str, str], int] = {}
    for r in results:
        path = _baseline_path(r.file, root)
        for f in r.findings:
            key = _baseline_key(path, f)
            counts[key] = counts.get(key, 0) + 1
    return [{"path": p, "rule": rule, "fingerprint": fp, "count": n} for (p, rule, fp), n in sorted(counts.items())]


def write_baseline(path: str | os.PathLike[str], entries: list[dict[str, Any]]) -> None:
    doc = {"version": BASELINE_VERSION, "entries": entries}
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def read_baseline(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """The baseline entries. A missing or unreadable file is an error, never an empty baseline."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"Baseline file not found: {path}. Create it with --write-baseline.") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Baseline file {path} is not valid JSON ({exc}). Recreate it with --write-baseline.") from None
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or doc.get("version") != BASELINE_VERSION:
        raise ValueError(f"Baseline file {path} is not a Gulliver baseline (version {BASELINE_VERSION}). "
                         "Recreate it with --write-baseline.")
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("path"), str) and isinstance(e.get("rule"), str)
                and isinstance(e.get("fingerprint"), str) and isinstance(e.get("count"), int) and e["count"] > 0):
            raise ValueError(f"Baseline file {path} has a malformed entry. Recreate it with --write-baseline.")
    return entries


def apply_baseline(results: list[FileResult], entries: list[dict[str, Any]], root: Path) -> tuple[int, int]:
    """Move findings that the baseline already holds to `ignored` (ignored_by "baseline").

    Each entry covers `count` findings, so a copy of a baselined line beyond that count is new. Returns
    (baselined, fixed): fixed counts leftover entries of the files that were linted in this run only.
    """
    left: dict[tuple[str, str, str], int] = {}
    for e in entries:
        key = (e["path"], e["rule"], e["fingerprint"])
        left[key] = left.get(key, 0) + e["count"]
    linted = {_baseline_path(r.file, root) for r in results}
    baselined = 0
    for r in results:
        path = _baseline_path(r.file, root)
        kept: list[FileFinding] = []
        for f in r.findings:
            key = _baseline_key(path, f)
            if left.get(key, 0) > 0:
                left[key] -= 1
                f.ignored_by = "baseline"
                r.ignored.append(f)
                baselined += 1
            else:
                kept.append(f)
        r.findings = kept
        # same rule as lint_file: no rewrite is offered for a finding that is not reported
        reported = {(f.statement, f.rule) for f in kept}
        r.fixes = [x for x in r.fixes if all((x.statement, rule) in reported for rule in x.rules)]
    fixed = sum(n for (path, _, _), n in left.items() if path in linted)
    return baselined, fixed


# -- many files -------------------------------------------------------------------------------------------
def fails(severity: str, fail_on: str) -> bool:
    if fail_on == "never":
        return False
    return SEVERITY_ORDER.get(severity, 9) <= SEVERITY_ORDER[fail_on]


def lint_paths(paths: list[str], dialect: str | None = None, config: LintConfig | None = None,
               fail_on: str | None = None, exclude: Iterable[str] = (), warn_unused_ignores: bool | None = None,
               metadata: Metadata | None = None, baseline: str | os.PathLike[str] | None = None,
               write_baseline_to: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Lint files and folders. Returns a JSON-ready dict, plus the FileResult objects for the formatters under
    the private key `_results` (public() drops it)."""
    config = config or LintConfig()
    dialect = (dialect or config.dialect or "duckdb").lower()
    fail_on = (fail_on or config.fail_on).lower()
    if fail_on not in FAIL_ON:
        raise ValueError(f"fail_on must be one of {', '.join(FAIL_ON)}")
    files, missing = discover(paths, [*config.exclude, *exclude])
    if missing:
        return {"status": "error", "error": "Not found: " + ", ".join(missing),
                "next_step": "Give existing .sql files or folders (or one SQL string in quotes)."}
    results = [lint_file(f, dialect, config, metadata, warn_unused_ignores=warn_unused_ignores) for f in files]
    baseline = write_baseline_to or baseline or config.baseline
    baselined = baseline_fixed = 0
    written: int | None = None
    if baseline:
        root = Path(baseline).resolve().parent
        try:
            if write_baseline_to:   # record today's findings, then apply them: this run reports nothing new
                entries = baseline_entries(results, root)
                write_baseline(write_baseline_to, entries)
                written = sum(e["count"] for e in entries)
            else:
                entries = read_baseline(baseline)
        except (OSError, ValueError) as exc:
            return {"status": "error", "error": str(exc),
                    "next_step": "Fix the baseline path, or recreate the file with --write-baseline."}
        baselined, baseline_fixed = apply_baseline(results, entries, root)
    findings = [f for r in results for f in r.findings]
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    failing = sum(1 for f in findings if fails(f.severity, fail_on))
    fixes = [x for r in results for x in r.fixes]
    out: dict[str, Any] = {
        "action": "lint_files",
        "dialect": dialect,
        "fail_on": fail_on,
        "files": len(results),
        "statements": sum(r.statements for r in results),
        "skipped_statements": sum(len(r.skipped) for r in results),
        "counts": counts,
        "failing": failing,
        "ignored": sum(1 for r in results for f in r.ignored if f.ignored_by != "baseline"),
        "findings": [f.as_dict() for f in findings],
        "ignored_findings": [f.as_dict() for r in results for f in r.ignored],
        "fixes": [x.as_dict() for x in fixes],
        "_results": results,
    }
    if baseline:
        out["baselined"] = baselined
        out["baseline_fixed"] = baseline_fixed
    if metadata is not None and metadata.notes:
        out["notes"] = metadata.notes
    if written is not None:
        out["status"] = "ok"
        out["next_step"] = (f"Wrote {written} finding(s) to {write_baseline_to}. Commit it: from now on only new "
                            "findings are reported.")
    elif not results:
        out["status"] = "ok"
        out["next_step"] = "No .sql files found. Give a folder or files (check exclude)."
    elif failing:
        out["status"] = "block"
        out["next_step"] = (f"{failing} finding(s) at or above '{fail_on}'. Fix the SQL, or add "
                            "`-- gulliver: ignore GVxxx` with a reason where the pattern is intended.")
    else:
        out["status"] = "warn" if findings else "ok"
        out["next_step"] = ("No finding fails the check. " if findings else "No scale problems found. ") + \
            "Preflight queries against a connection for cost and size."
    if any(x.placed for x in fixes):
        out["next_step"] += " `gulliver fix --write` applies the same-rows rewrites listed in fixes."
    return out


def public(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


# -- fix --check / --write -----------------------------------------------------------------------------------
def apply_edits(source: str, edits: list[Edit]) -> str:
    for e in sorted(edits, key=lambda x: x.start, reverse=True):
        source = source[:e.start] + e.after + source[e.end:]
    return source


def fix_paths(paths: list[str], dialect: str | None = None, config: LintConfig | None = None,
              exclude: Iterable[str] = (), metadata: Metadata | None = None, write: bool = False) -> dict[str, Any]:
    """Same-rows rewrites for files. check (write=False): status block when one exists. write: apply them."""
    config = config or LintConfig()
    dialect = (dialect or config.dialect or "duckdb").lower()
    files, missing = discover(paths, [*config.exclude, *exclude])
    if missing:
        return {"status": "error", "error": "Not found: " + ", ".join(missing),
                "next_step": "Give existing .sql files or folders."}
    fixes: list[FileFix] = []
    written: list[str] = []
    for file in files:
        res = lint_file(file, dialect, config, metadata, warn_unused_ignores=False)
        fixes.extend(res.fixes)
        placed = [e for x in res.fixes if x.placed for e in x.edits]
        if write and placed:
            new = apply_edits(res.source, placed)
            if new != res.source:
                with open(file, "w", encoding="utf-8", newline="") as fh:
                    fh.write(("\ufeff" if res.bom else "") + new)
                written.append(res.path)
    ready = [x for x in fixes if x.placed]
    unplaced = [x for x in fixes if not x.placed]
    out: dict[str, Any] = {"action": "fix_files", "dialect": dialect, "mode": "write" if write else "check",
                           "files": len(files), "fixes": [x.as_dict() for x in fixes],
                           "fixable": len(ready), "unplaced": len(unplaced), "written": written}
    if metadata is not None and metadata.notes:
        out["notes"] = list(metadata.notes)
    if metadata is not None and metadata.missing:
        shown = ", ".join(metadata.missing[:10]) + (" ..." if len(metadata.missing) > 10 else "")
        out.setdefault("notes", []).append(
            f"Not found in the connection, so not checked for fixes: {shown}. Pass the warehouse connection "
            "(--dsn or -c) that has these tables.")
    if metadata is None:
        out.setdefault("notes", []).append("No connection metadata: date rewrites need the column type, so few or no "
                                           "same-rows fixes can be proven.")
    unchecked = metadata is None or bool(metadata.missing)
    if write:
        out["status"] = "warn" if unplaced or (unchecked and not written) else "ok"
        out["next_step"] = (f"Rewrote {len(written)} file(s) with same-rows changes only. Review the diff and commit."
                            if written else "Nothing to rewrite with a same-rows guarantee.")
    else:
        out["status"] = "block" if ready else ("warn" if unplaced or unchecked else "ok")
        out["next_step"] = (f"{len(ready)} same-rows rewrite(s) available. Run `gulliver fix --write` on these files."
                            if ready else "Nothing to rewrite with a same-rows guarantee.")
    if unchecked and not ready:
        out["next_step"] += " Some statements could not be checked without table metadata; see notes."
    if unplaced:
        out["next_step"] += f" {len(unplaced)} rewrite(s) could not be placed as exact edits; see fixes[].notes."
    return out
