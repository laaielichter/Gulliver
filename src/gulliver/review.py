"""gulliver review: a pull-request gate for SQL models.

For every .sql model a branch changes (git diff base...head, or two dbt manifests) it reports:
  - the scale lint findings the change introduces (new minus old)
  - what kind of change it is: cosmetic, non_breaking (only columns added) or breaking
  - which downstream models use what was removed or changed (column-level lineage)
  - with a connection: the scan and money per run before vs after, and per month (estimates only)
  - with rehearse: whether output rows now grow faster than the data (1% and 3% samples)
A [review] policy in gulliver.toml turns these flags into ok / warn / block (exit code 2).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from . import __version__, lineage
from .config import ConfigError, Settings, find_config_file, table_section
from .engines import EngineError, dialect_for
from .lint import lint
from .sqlsafety import classify

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

MARKER = "<!-- gulliver-review -->"
REHEARSAL_FRACTIONS = [0.01, 0.03]
SUPERLINEAR = 1.3
DAYS_PER_MONTH = 30
MAX_MARKDOWN = 60_000  # GitHub comments stop at 65,536 characters
# A model is meant to return all of its rows, so "no LIMIT" is not a finding here.
SKIP_RULES = frozenset({"GV002"})
FLAG_KINDS = ("cost", "breaking", "breaking_with_downstream", "new_lint_error", "new_lint_warning", "superlinear",
              "not_compiled")
ACCURACY_ORDER = ["exact", "upper_bound", "heuristic", "unknown"]
CLAUSES = {"with": "WITH (CTEs)", "with_": "WITH (CTEs)", "from": "FROM", "from_": "FROM", "joins": "JOIN",
           "where": "WHERE", "group": "GROUP BY", "having": "HAVING", "qualify": "QUALIFY", "distinct": "DISTINCT",
           "limit": "LIMIT", "offset": "OFFSET", "order": "ORDER BY", "laterals": "LATERAL", "pivots": "PIVOT"}


# -- policy ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Policy:
    """The [review] section of gulliver.toml. A flag whose kind is in fail_on blocks the merge."""

    max_increase_pct: float = 100.0        # scan per run, per model (only above ignore_below_bytes)
    max_increase_usd: float = 50.0         # money per month for the whole change (per run x runs_per_day x 30)
    runs_per_day: float = 1.0
    ignore_below_bytes: int = 1024**3      # small scans never trip max_increase_pct
    fail_on: tuple[str, ...] = ("cost", "breaking_with_downstream", "new_lint_error", "superlinear")
    ref_schema: str = ""                   # {{ ref('x') }} -> <ref_schema>.x when estimating (like dbt --defer)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Policy:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"Unknown [review] keys: {', '.join(sorted(unknown))}. Known: {', '.join(sorted(known))}")
        values: dict[str, Any] = {}
        for key, value in data.items():
            if key == "fail_on":
                items = [value] if isinstance(value, str) else list(value or [])
                kinds = [k.strip() for part in items for k in str(part).split(",") if k.strip()]
                bad = [k for k in kinds if k not in FLAG_KINDS and k != "none"]
                if bad:
                    raise ConfigError(f"Unknown fail_on value(s): {', '.join(bad)}. Use: {', '.join(FLAG_KINDS)}")
                values[key] = tuple(k for k in kinds if k != "none")
                continue
            default = getattr(cls, key)
            try:
                values[key] = type(default)(value)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"[review] {key}={value!r} is not a valid {type(default).__name__}") from exc
        return cls(**values)

    def fails(self, kind: str) -> bool:
        if kind in self.fail_on:
            return True
        if kind == "breaking_with_downstream" and "breaking" in self.fail_on:
            return True
        return kind == "new_lint_error" and "new_lint_warning" in self.fail_on

    def as_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["fail_on"] = list(self.fail_on)
        return out


def load_policy(config_path: str | os.PathLike[str] | None = None,
                overrides: dict[str, Any] | None = None) -> Policy:
    data: dict[str, Any] = {}
    path = find_config_file(config_path)
    if path:
        with path.open("rb") as fh:
            data = table_section(tomllib.load(fh), "review")
    data.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return Policy.from_mapping(data)


# -- git -------------------------------------------------------------------------------------------
class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    exe = shutil.which("git")
    if exe is None:
        raise GitError("git is not installed or not on PATH")
    # A list of arguments and no shell: refs and paths cannot inject commands.
    proc = subprocess.run([exe, "-C", str(repo), *args], input=stdin, capture_output=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(lines[-1] if lines else f"git {args[0]} failed")
    return proc.stdout


def _rev(repo: Path, ref: str) -> str | None:
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip() or None
    except GitError:
        return None


def _repo_and_prefix(models_dir: str, repo: str | None) -> tuple[Path, str]:
    anchor = Path(repo) if repo else Path.cwd()
    target = Path(models_dir) if Path(models_dir).is_absolute() else anchor / models_dir
    start = target if target.is_dir() else anchor
    top = Path(_git(start, "rev-parse", "--show-toplevel").decode().strip())
    try:
        prefix = target.resolve().relative_to(top.resolve()).as_posix()
    except ValueError as exc:
        raise GitError(f"{models_dir} is not inside the git repository at {top}") from exc
    return top, "" if prefix == "." else prefix


def _pathspec(prefix: str) -> list[str]:
    return ["--", prefix] if prefix else []


def _read_tree(repo: Path, rev: str, prefix: str) -> dict[str, str]:
    """Every .sql file under prefix at rev, read with one `git cat-file --batch`.

    cat-file gets the blob ids, not `rev:path`: its input is one line per object, so a file name with a newline
    in it would split into two requests and shift every later file onto the wrong content."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", rev, *_pathspec(prefix))
    blobs: list[tuple[str, str]] = []
    for entry in listing.decode("utf-8", "replace").split("\0"):
        meta, sep, name = entry.partition("\t")   # "<mode> <type> <sha>\t<path>"
        fields = meta.split()
        if sep and len(fields) == 3 and fields[1] == "blob" and name.endswith(".sql"):
            blobs.append((name, fields[2]))
    if not blobs:
        return {}
    data = _git(repo, "cat-file", "--batch", stdin="".join(f"{sha}\n" for _, sha in blobs).encode("ascii"))
    out: dict[str, str] = {}
    pos = 0
    for name, _ in blobs:
        end = data.index(b"\n", pos)
        header = data[pos:end].decode("utf-8", "replace")
        pos = end + 1
        if header.endswith(" missing"):
            continue
        size = int(header.split()[2])
        out[name] = data[pos:pos + size].decode("utf-8", "replace")
        pos += size + 1
    return out


def _changed_files(repo: Path, base: str, head: str, prefix: str) -> list[dict[str, str]]:
    raw = _git(repo, "diff", "--name-status", "-z", "-M", f"{base}...{head}", *_pathspec(prefix))
    parts = raw.decode("utf-8", "replace").split("\0")
    out: list[dict[str, str]] = []
    i = 0
    while i < len(parts) - 1:
        code = parts[i]
        if not code:
            i += 1
            continue
        if code[0] in "RC":
            old, new = parts[i + 1], parts[i + 2]
            i += 3
            if code[0] == "R" and old.endswith(".sql") and new.endswith(".sql"):
                out.append({"change": "renamed", "path": new, "old_path": old})
            elif code[0] == "R" and old.endswith(".sql"):
                out.append({"change": "removed", "path": old, "old_path": old})
            elif new.endswith(".sql"):
                out.append({"change": "added", "path": new})
            continue
        path = parts[i + 1]
        i += 2
        if not path.endswith(".sql"):
            continue
        kind = {"A": "added", "D": "removed"}.get(code[0], "modified")
        out.append({"change": kind, "path": path, **({"old_path": path} if kind != "added" else {})})
    return out


def _model_name(path: str) -> str:
    return PurePosixPath(path).stem.lower()


# -- SQL analysis ------------------------------------------------------------------------------------
def _models_from_texts(texts: dict[str, str]) -> dict[str, lineage.Model]:
    models: dict[str, lineage.Model] = {}
    for path in sorted(texts):
        name = _model_name(path)
        sql, refs, sources = lineage.render(texts[path], name)
        models[name] = lineage.Model(name=name, sql=sql, path=path, refs=refs, sources=sources)
    return models


def _approximate(text: str) -> bool:
    """True when the model uses Jinja beyond ref/source/config/this, which Gulliver only strips."""
    rest = lineage.COMMENT_RE.sub("", text)
    for pattern in (lineage.CONFIG_RE, lineage.REF_RE, lineage.SOURCE_RE, lineage.THIS_RE):
        rest = pattern.sub("", rest)
    return "{{" in rest or "{%" in rest


def _engine_sql(text: str, name: str, ref_schema: str) -> str:
    if ref_schema:
        text = lineage.REF_RE.sub(lambda m: f"{ref_schema}.{m.group(2) or m.group(1)}", text)
    return lineage.render(text, name)[0]


def _parse(sql: str, dialect: str) -> exp.Expression | None:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return None
    return tree if isinstance(tree, exp.Query) else None


def _canonical(tree: exp.Expression) -> exp.Expression:
    """Make `FROM t AS x ... x.a` and `FROM t ... a` compare equal, so renaming an alias is not a change.

    Only done when every table appears once (a self-join keeps its aliases: e.a and e2.a differ).
    """
    tree = tree.copy()
    tables = [t for t in tree.find_all(exp.Table) if isinstance(t.this, exp.Identifier)]
    names = [lineage.table_name(t) for t in tables]
    # qualifiers are rewritten to the last name part, so prod.orders and archive.orders must not both appear
    if len(names) != len(set(names)) or len({n.split(".")[-1] for n in names}) != len(names):
        return tree
    aliases = {t.alias.lower(): lineage.table_name(t) for t in tables if t.alias}
    for t in tables:
        if t.alias:
            t.set("alias", None)
    for col in list(tree.find_all(exp.Column)):
        qual = col.table.lower()
        if qual in aliases:
            col.set("table", exp.to_identifier(aliases[qual].split(".")[-1]))
    for select in tree.find_all(exp.Select):
        sources = [t for t in tables if t.find_ancestor(exp.Select) is select]
        if len(sources) == 1 and not select.args.get("joins"):
            short = sources[0].name.lower()
            for col in select.find_all(exp.Column):
                if col.table.lower() == short and col.find_ancestor(exp.Select) is select:
                    col.set("table", None)
    return tree


def _norm(node: Any, dialect: str) -> str:
    if node is None or node is False:
        return ""
    if isinstance(node, list):
        return ", ".join(_norm(n, dialect) for n in node)
    if isinstance(node, exp.Expression):
        return node.sql(dialect=dialect, normalize=True, comments=False)
    return str(node)


def _is_star(node: exp.Expression) -> bool:
    return isinstance(node, exp.Star) or (isinstance(node, exp.Column) and isinstance(node.this, exp.Star))


def _projections(tree: exp.Expression) -> dict[str, exp.Expression]:
    if not isinstance(tree, exp.Select):
        return {}
    out: dict[str, exp.Expression] = {}
    for i, p in enumerate(tree.expressions):
        # an unaliased expression (a + 1, SUM(a)) has no name: key it by position as sqlglot's lineage does
        key = p.sql().lower() if _is_star(p) else (p.alias_or_name.lower() or f"_col_{i}")
        out[key] = p.unalias() if isinstance(p, exp.Alias) else p
    return out


def _row_clauses(tree: exp.Expression) -> dict[str, Any]:
    """Everything but the outer column list. A top-level ORDER BY without LIMIT does not change any row."""
    args = {k: v for k, v in tree.args.items() if k != "expressions" and v not in (None, [], False)}
    if not (args.get("limit") or args.get("fetch")):
        args.pop("order", None)
    return args


def classify_change(base_sql: str, head_sql: str, dialect: str = "duckdb",
                    base_columns: set[str] | None = None, head_columns: set[str] | None = None) -> dict[str, Any]:
    """cosmetic | non_breaking | breaking | unknown, with the reasons and the lineage targets that may break.

    base_columns / head_columns: output columns from lineage (stars expanded where the schema is known).
    """
    bt, ht = _parse(base_sql, dialect), _parse(head_sql, dialect)
    if bt is None or ht is None:
        which = "base" if bt is None else "head"
        return {"classification": "unknown", "reasons": [f"the {which} version could not be parsed as one query"],
                "columns": {}, "targets": ["*"]}
    bt, ht = _canonical(bt), _canonical(ht)
    if _norm(bt, dialect) == _norm(ht, dialect):
        return {"classification": "cosmetic", "reasons": ["only formatting, comments or identifier case changed"],
                "columns": {}, "targets": []}
    bp, hp = _projections(bt), _projections(ht)
    bcols = set(base_columns) if base_columns else set(bp)
    hcols = set(head_columns) if head_columns else set(hp)
    removed = sorted(c for c in bcols - hcols if c != "*" and not c.endswith(".*"))
    added = sorted(c for c in hcols - bcols if c != "*" and not c.endswith(".*"))
    changed: list[str] = []
    retyped: list[dict[str, str]] = []
    for name in sorted(set(bp) & set(hp)):
        if name == "*" or name.endswith(".*"):
            continue
        if _norm(bp[name], dialect) != _norm(hp[name], dialect):
            changed.append(name)
            if isinstance(bp[name], exp.Cast) and isinstance(hp[name], exp.Cast):
                old, new = _norm(bp[name].to, dialect), _norm(hp[name].to, dialect)
                if old != new:
                    retyped.append({"column": name, "from": old, "to": new})
    reasons: list[str] = []
    whole = False
    if not isinstance(bt, exp.Select) or not isinstance(ht, exp.Select) or type(bt) is not type(ht):
        reasons.append("the UNION / set operation changed: every row can change")
        whole = True
    else:
        brest, hrest = _row_clauses(bt), _row_clauses(ht)
        clauses = sorted({CLAUSES.get(k, k.upper()) for k in set(brest) | set(hrest)
                          if _norm(brest.get(k), dialect) != _norm(hrest.get(k), dialect)})
        if clauses:
            reasons.append(f"changes {', '.join(clauses)}: rows can be added, dropped or duplicated")
            whole = True
        bstars = {k for k in bp if k == "*" or k.endswith(".*")}
        hstars = {k for k in hp if k == "*" or k.endswith(".*")}
        if bstars != hstars and ("*" in bcols or "*" in hcols):
            reasons.append("SELECT * changed and the columns behind it are unknown (the source schema is not known)")
            whole = True
    if removed:
        reasons.append("removes column(s) " + ", ".join(removed))
    if retyped:
        reasons.append("changes the type of " + ", ".join(f"{r['column']} ({r['from']} -> {r['to']})" for r in retyped))
    other_changed = [c for c in changed if c not in {r["column"] for r in retyped}]
    if other_changed:
        reasons.append("changes how " + ", ".join(other_changed) + " is computed")
    columns = {k: v for k, v in (("added", added), ("removed", removed), ("changed", changed),
                                 ("retyped", retyped)) if v}
    if reasons:
        targets = ["*"] if whole else sorted(set(removed) | set(changed))
        return {"classification": "breaking", "reasons": reasons, "columns": columns, "targets": targets}
    if added:
        return {"classification": "non_breaking", "reasons": ["only adds column(s) " + ", ".join(added)],
                "columns": columns, "targets": []}
    if list(bp) == list(hp):
        return {"classification": "cosmetic", "reasons": ["same columns and rows (only the outer ORDER BY without LIMIT, or how "
                                                     "aliases are written, changed)"],
                "columns": columns, "targets": []}
    return {"classification": "non_breaking", "reasons": ["only the column order changed"], "columns": columns,
            "targets": []}


def _lint(sql: str, dialect: str) -> list[dict[str, Any]]:
    statement = classify(sql, dialect)
    if statement.select is None:
        return []
    return [f.as_dict() for f in lint(statement.select, dialect) if f.rule not in SKIP_RULES]


def _introduced(base: list[dict[str, Any]], head: list[dict[str, Any]]) -> tuple[list[dict], list[str]]:
    old = {(f["rule"], f["message"]) for f in base}
    new = {(f["rule"], f["message"]) for f in head}
    introduced = [f for f in head if (f["rule"], f["message"]) not in old]
    fixed = sorted({f["rule"] for f in base if (f["rule"], f["message"]) not in new})
    return introduced, fixed


def _columns_of(model: lineage.Model | None) -> set[str] | None:
    if model is None or model.error or not model.columns:
        return None
    return set(model.columns)


# -- the review --------------------------------------------------------------------------------------
@dataclass
class _Version:
    name: str
    path: str
    sql: str            # rendered for parsing and lint
    engine_sql: str     # rendered for estimates (refs may get ref_schema)
    approximate: bool = False


def _has_data(settings: Settings | None, connection: str | None) -> bool:
    if settings is None:
        return False
    conn = settings.connection(connection)
    return not (conn.engine == "duckdb" and str(conn.params.get("path") or ":memory:") == ":memory:")


def review(settings: Settings | None, base: str = "origin/main", head: str = "HEAD", models_dir: str | None = None,
           manifest_base: str | None = None, manifest_head: str | None = None, connection: str | None = None,
           rehearse: bool = False, policy: Policy | None = None, dialect: str | None = None,
           repo: str | None = None, config_path: str | None = None) -> dict[str, Any]:
    from .core import _error

    try:
        policy = policy or load_policy(config_path)
        use_engine = _has_data(settings, connection)
        if use_engine and settings is not None:
            dialect = dialect_for(settings.connection(connection).engine)
        dialect = dialect or "duckdb"
        if manifest_base or manifest_head:
            collected = _collect_manifests(manifest_base, manifest_head)
        elif models_dir:
            collected = _collect_git(models_dir, base, head, repo, policy.ref_schema)
        else:
            return _error("Give a models folder (--models) with --base, or two dbt manifests "
                          "(--manifest-base and --manifest-head).")
    except ConfigError as exc:
        return _error(str(exc), "Fix the [review] section of gulliver.toml or the flags.")
    except GitError as exc:
        return _error(f"git: {exc}", "Run review inside a git checkout and check the --base and --head refs.")
    if "status" in collected:
        return collected
    return _review(settings if use_engine else None, connection, rehearse, policy, dialect, collected)


def _collect_git(models_dir: str, base: str, head: str, repo: str | None, ref_schema: str) -> dict[str, Any]:
    top, prefix = _repo_and_prefix(models_dir, repo)
    head_sha = _rev(top, head)
    if head_sha is None:
        return {"status": "error", "error": f"head ref '{head}' not found",
                "next_step": "Pass --head as a branch, tag or commit that exists."}
    base_sha = _rev(top, base)
    if base_sha is None:
        return {"status": "block", "action": "review", "base": {"ref": base}, "head": {"ref": head, "sha": head_sha},
                "reason": "base_missing",
                "next_step": f"The base ref '{base}' is not in this checkout, so nothing can be compared (Gulliver "
                             "does not fall back to reviewing everything). Fetch it first, e.g. `git fetch origin "
                             "main`, or use actions/checkout with fetch-depth: 0."}
    try:
        merge_base = _git(top, "merge-base", base_sha, head_sha).decode().strip()
    except GitError:
        return {"status": "block", "action": "review", "base": {"ref": base, "sha": base_sha},
                "head": {"ref": head, "sha": head_sha}, "reason": "no_merge_base",
                "next_step": "base and head share no history in this checkout (a shallow clone?). Fetch more "
                             "history, e.g. actions/checkout with fetch-depth: 0, and run again."}
    if prefix and not (top / prefix).is_dir() and not any(
            _git(top, "ls-tree", "--full-tree", rev, "--", prefix).strip() for rev in (merge_base, head_sha)):
        return {"status": "block", "action": "review", "base": {"ref": base, "sha": base_sha},
                "head": {"ref": head, "sha": head_sha}, "reason": "models_dir_missing", "models_dir": prefix,
                "next_step": f"The models folder '{models_dir}' is in neither the base, the head nor the checkout, "
                             "so nothing can be reviewed (a typo would otherwise pass every PR). Pass the folder "
                             "that holds the .sql models, e.g. --models models."}
    changes = _changed_files(top, base_sha, head_sha, prefix)
    base_texts = _read_tree(top, merge_base, prefix)
    head_texts = _read_tree(top, head_sha, prefix)

    def version(path: str, text: str) -> _Version:
        name = _model_name(path)
        return _Version(name, path, lineage.render(text, name)[0], _engine_sql(text, name, ref_schema),
                        _approximate(text))

    pairs = []
    for c in changes:
        old = version(c["old_path"], base_texts.get(c["old_path"], "")) if c.get("old_path") else None
        new = version(c["path"], head_texts.get(c["path"], "")) if c["change"] != "removed" else None
        pairs.append((c["change"], old, new))
    notes: list[str] = []
    if _rev(top, "HEAD") == head_sha:
        try:
            dirty = [line for line in _git(top, "status", "--porcelain", *_pathspec(prefix)).decode().splitlines()
                     if line.strip().endswith(".sql")]
        except GitError:
            dirty = []
        if dirty:
            notes.append(f"{len(dirty)} uncommitted .sql change(s) are not part of this review; commit them first.")
    return {"mode": "git", "base": {"ref": base, "sha": base_sha, "merge_base": merge_base},
            "head": {"ref": head, "sha": head_sha}, "models_dir": prefix or ".", "pairs": pairs,
            "base_models": _models_from_texts(base_texts), "head_models": _models_from_texts(head_texts),
            "notes": notes}


def _collect_manifests(manifest_base: str | None, manifest_head: str | None) -> dict[str, Any]:
    if not manifest_head or not Path(manifest_head).is_file():
        return {"status": "error", "error": f"head manifest not found: {manifest_head}",
                "next_step": "Run `dbt compile` (or dbt build) on the branch and pass its target/manifest.json."}
    if not manifest_base or not Path(manifest_base).is_file():
        return {"status": "block", "action": "review", "reason": "base_manifest_missing",
                "next_step": f"The base manifest ({manifest_base or 'not given'}) is missing, so nothing can be "
                             "compared. Gulliver does not fall back to reviewing every model. Download the "
                             "production manifest.json (for example from the last main build) and pass it."}
    try:
        meta_b = json.loads(Path(manifest_base).read_text(encoding="utf-8")).get("metadata") or {}
        meta_h = json.loads(Path(manifest_head).read_text(encoding="utf-8")).get("metadata") or {}
        base_models = lineage.load_models(manifest=manifest_base)
        head_models = lineage.load_models(manifest=manifest_head)
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        return {"status": "error", "error": f"could not read a manifest: {exc}",
                "next_step": "Pass dbt target/manifest.json files."}
    if meta_b.get("project_id") and meta_h.get("project_id") and meta_b["project_id"] != meta_h["project_id"]:
        return {"status": "block", "action": "review", "reason": "base_manifest_stale",
                "next_step": "The two manifests come from different dbt projects (project_id differs). Pass the "
                             "base manifest of this project."}
    if not base_models:
        return {"status": "block", "action": "review", "reason": "base_manifest_empty",
                "next_step": "The base manifest has no models, so every model would look new. Pass the real "
                             "production manifest."}

    def version(model: lineage.Model) -> _Version:
        return _Version(model.name, model.path, model.sql, model.sql)

    pairs = []
    for name in sorted(set(base_models) | set(head_models)):
        b, h = base_models.get(name), head_models.get(name)
        if b and h and b.sql == h.sql:
            continue
        change = "modified" if b and h else ("added" if h else "removed")
        pairs.append((change, version(b) if b else None, version(h) if h else None))
    return {"mode": "manifest", "base": {"manifest": manifest_base, "generated_at": meta_b.get("generated_at")},
            "head": {"manifest": manifest_head, "generated_at": meta_h.get("generated_at")}, "pairs": pairs,
            "base_models": base_models, "head_models": head_models, "notes": []}


def _review(settings: Settings | None, connection: str | None, rehearse: bool, policy: Policy, dialect: str,
            collected: dict[str, Any]) -> dict[str, Any]:
    base_models: dict[str, lineage.Model] = collected["base_models"]
    head_models: dict[str, lineage.Model] = collected["head_models"]
    lineage.build(base_models, dialect)
    lineage.build(head_models, dialect)
    notes: list[str] = list(collected["notes"])
    changed_names = {(new or old).name for _, old, new in collected["pairs"]}
    entries: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []

    def flag(kind: str, model: str, message: str) -> None:
        flags.append({"kind": kind, "model": model, "message": message, "blocks": policy.fails(kind)})

    estimator = _Estimator(settings, connection, policy, flag) if settings is not None else None

    for change, old, new in collected["pairs"]:
        name = (new or old).name
        entry: dict[str, Any] = {"model": name, "path": (new or old).path, "change": change}
        if change == "renamed" and old is not None:
            entry["renamed_from"] = old.name
        if (new and new.approximate) or (old and old.approximate):
            entry["compiled"] = "approximate"
        if change == "added":
            result = {"classification": "non_breaking", "reasons": ["new model: nothing depends on it yet"],
                      "columns": {}, "targets": []}
            if _parse(new.sql, dialect) is None:
                result = {"classification": "unknown", "reasons": ["the new model could not be parsed"],
                          "columns": {}, "targets": []}
        elif change == "removed":
            result = {"classification": "breaking", "reasons": ["the model is deleted"], "columns": {},
                      "targets": ["*"]}
        else:
            result = classify_change(old.sql, new.sql, dialect, _columns_of(base_models.get(old.name)),
                                     _columns_of(head_models.get(new.name)))
            if change == "renamed" and old.name != new.name:
                result["classification"] = "breaking"
                result["reasons"].insert(0, f"renamed from {old.name}: refs to the old name break")
                result["old_targets"] = ["*"]
        entry.update(classification=result["classification"], reasons=result["reasons"])
        if result["columns"]:
            entry["columns"] = result["columns"]

        # downstream: lineage of the head graph, from the columns (or the whole model) that changed
        source = old.name if change in ("removed",) else name
        targets = [(source, c) for c in result["targets"]]
        if result.get("old_targets") and old is not None:
            targets += [(old.name, c) for c in result["old_targets"]]
        affected = lineage.impact(head_models, targets)["affected"] if targets else []
        entry["downstream"] = [{"model": a["model"], "path": a["path"], "columns": a["columns"], "via": a["via"],
                                "depth": a["depth"], "select_star": a["select_star"],
                                **({"changed_in_this_change": True} if a["model"] in changed_names else {})}
                               for a in affected if a["model"] != name]

        # cost (with a connection) first: its lint knows the partitions, so GV004 can show up
        old_lint = new_lint = None
        if estimator is not None and entry["classification"] != "cosmetic":
            old_lint, new_lint = estimator.add(entry, old, new)
        if old_lint is None:
            old_lint = _lint(old.sql, dialect) if old else []
        if new_lint is None:
            new_lint = _lint(new.sql, dialect) if new else []
        introduced, fixed = _introduced(old_lint, new_lint)
        entry["lint"] = {"introduced": introduced, "fixed": fixed}

        if entry["classification"] == "unknown":
            flag("not_compiled", name, f"{name}: could not be parsed ({'; '.join(result['reasons'])}); "
                                       "it was not classified")
        elif entry["classification"] == "breaking":
            n = len(entry["downstream"])
            what = "; ".join(result["reasons"])
            if n:
                names = ", ".join(d["model"] for d in entry["downstream"][:5]) + (" ..." if n > 5 else "")
                flag("breaking_with_downstream", name, f"{name}: {what}; used by {n} downstream model(s): {names}")
            else:
                flag("breaking", name, f"{name}: {what}; nothing downstream uses it")
        for f in introduced:
            kind = "new_lint_error" if f["severity"] == "error" else "new_lint_warning"
            if f["severity"] in ("error", "warn"):
                flag(kind, name, f"{name}: new {f['severity']} {f['rule']} {f['name']}: {f['message']}")
        entries.append(entry)

    totals: dict[str, Any] | None = None
    if estimator is not None and entries:
        totals = estimator.totals(notes)
        if rehearse:
            _rehearse_all(settings, connection, collected["pairs"], entries, flag, notes)
    elif rehearse and entries:
        notes.append("Rehearsal needs a connection with data (--dsn or a connection in gulliver.toml); skipped.")

    return _finish(collected, entries, flags, totals, policy, notes, dialect, settings is not None, rehearse)


def _worst(accuracies: list[str]) -> str:
    known = [a for a in accuracies if a in ACCURACY_ORDER]
    return max(known, key=ACCURACY_ORDER.index) if known else "unknown"


class _Estimator:
    """Preflight both versions of each model (estimate only, no data read) and add up the deltas."""

    def __init__(self, settings: Settings, connection: str | None, policy: Policy, flag: Any):
        self.settings, self.connection, self.policy, self.flag = settings, connection, policy, flag
        self.total_bytes = 0
        self.total_run = 0.0
        self.accuracies: list[str] = []
        self.errors = 0

    def _preflight(self, sql: str) -> dict[str, Any]:
        from .core import _preflight_core

        try:
            return _preflight_core(self.settings, sql, self.connection)
        except (EngineError, ConfigError) as exc:
            return {"status": "error", "error": str(exc)}

    def add(self, entry: dict[str, Any], old: _Version | None,
            new: _Version | None) -> tuple[list[dict] | None, list[dict] | None]:
        policy = self.policy
        pre_old = self._preflight(old.engine_sql) if old else None
        pre_new = self._preflight(new.engine_sql) if new else None

        def lint_of(pre: dict[str, Any] | None) -> list[dict] | None:
            if pre is None:
                return []
            if pre.get("status") == "error" or "lint" not in pre:
                return None
            return [f for f in pre["lint"] if f["rule"] not in SKIP_RULES]

        lints = (lint_of(pre_old), lint_of(pre_new))
        failed = [p for p in (pre_old, pre_new) if p is not None and p.get("status") == "error"]
        if failed:
            self.errors += 1
            entry["cost"] = {"error": failed[0].get("error"),
                             "note": "not estimated: the engine rejected the SQL (a table or column this change "
                                     "needs may not exist in production yet)"}
            return lints
        e_old = (pre_old or {}).get("estimate") or {}
        e_new = (pre_new or {}).get("estimate") or {}
        b_old, b_new = (e_old.get("bytes"), e_new.get("bytes"))
        u_old, u_new = (e_old.get("cost_usd"), e_new.get("cost_usd"))
        if old is None:
            b_old, u_old = 0, 0.0
        if new is None:
            b_new, u_new = 0, 0.0
        accuracy = _worst([e.get("accuracy") or "unknown" for e in (e_old, e_new) if e])
        self.accuracies.append(accuracy)
        cost: dict[str, Any] = {"bytes_base": b_old, "bytes_head": b_new, "usd_base": u_old, "usd_head": u_new,
                                "accuracy": accuracy}
        if b_old is not None and b_new is not None:
            cost["bytes_delta"] = b_new - b_old
            self.total_bytes += b_new - b_old
            if b_old:
                cost["bytes_delta_pct"] = round((b_new - b_old) / b_old * 100, 1)
        if u_old is not None and u_new is not None:
            run = u_new - u_old
            cost["usd_delta_per_run"] = round(run, 6)
            cost["usd_delta_per_month"] = round(run * policy.runs_per_day * DAYS_PER_MONTH, 4)
            self.total_run += run
        if pre_new and pre_new.get("status") == "block":
            cost["head_gate"] = (pre_new.get("gate") or {}).get("blocks") or []
        entry["cost"] = cost
        pct = cost.get("bytes_delta_pct")
        if (pct is not None and pct > policy.max_increase_pct
                and max(b_old or 0, b_new or 0) >= policy.ignore_below_bytes):
            self.flag("cost", entry["model"], f"{entry['model']}: scan per run grows {pct:+.0f}% "
                                              f"(limit {policy.max_increase_pct:g}%)")
        return lints

    def totals(self, notes: list[str]) -> dict[str, Any]:
        policy = self.policy
        month = round(self.total_run * policy.runs_per_day * DAYS_PER_MONTH, 4)
        if month > policy.max_increase_usd:
            self.flag("cost", "(all)", f"the change adds about ${month:,.2f} per month at {policy.runs_per_day:g} "
                                       f"run(s) a day (limit ${policy.max_increase_usd:,.2f})")
        if self.errors:
            notes.append(f"{self.errors} model(s) could not be estimated; see their cost note.")
        conn = self.settings.connection(self.connection)
        return {"bytes_delta": self.total_bytes, "usd_delta_per_run": round(self.total_run, 6),
                "usd_delta_per_month": month, "runs_per_day": policy.runs_per_day,
                "accuracy": _worst(self.accuracies), "not_estimated": self.errors, "connection": conn.name,
                "engine": conn.engine}


def _rehearse_all(settings: Settings, connection: str | None, pairs: list, entries: list[dict], flag,
                  notes: list[str]) -> None:
    from .core import rehearse as core_rehearse

    for (_change, old, new), entry in zip(pairs, entries, strict=True):
        if new is None or entry["classification"] in ("cosmetic", "unknown") or "error" in (entry.get("cost") or {}):
            continue
        # sample="all": every occurrence is sampled on its own, so a join that multiplies rows shows up as an
        # exponent near 2 (the default mode keeps one side full and grows linearly, which hides it here)
        runs = {"head": core_rehearse(settings, new.engine_sql, connection, fractions=REHEARSAL_FRACTIONS,
                                      sample="all")}
        if old is not None:
            runs["base"] = core_rehearse(settings, old.engine_sql, connection, fractions=REHEARSAL_FRACTIONS,
                                         sample="all")
        growth: dict[str, Any] = {"fractions": REHEARSAL_FRACTIONS}
        spent = 0.0
        for side, res in runs.items():
            pred = (res.get("prediction") or {}).get("output_rows") or {}
            growth[f"{side}_status"] = res.get("status")
            growth[f"{side}_exponent"] = pred.get("exponent")
            if side == "head":
                growth["head_rows_predicted"] = pred.get("value")
                growth["confidence"] = pred.get("confidence")
            if res.get("status") in ("skipped", "error", "block"):
                growth[f"{side}_note"] = res.get("reason") or res.get("error") or res.get("next_step")
            spent += sum(r.get("cost_usd") or 0 for r in res.get("rungs") or [])
        growth["cost_usd"] = round(spent, 6)
        k_new, k_old = growth.get("head_exponent"), growth.get("base_exponent")
        if k_new is not None and k_new > SUPERLINEAR and (old is None or (k_old is not None and k_old <= SUPERLINEAR)):
            growth["superlinear"] = True
            was = f" (was {k_old:.2f})" if k_old is not None else ""
            flag("superlinear", entry["model"], f"{entry['model']}: output rows now grow faster than the data, "
                                                f"exponent {k_new:.2f}{was}; often a join that multiplies rows")
        entry["growth"] = growth
    notes.append("Rehearsals ran each changed model on 1% and 3% samples (spent money is in the ledger). Two "
                 "points give a rough exponent, not a forecast.")


def _finish(collected: dict[str, Any], entries: list[dict], flags: list[dict], totals: dict[str, Any] | None,
            policy: Policy, notes: list[str], dialect: str, estimated: bool, rehearsed: bool) -> dict[str, Any]:
    counts = {k: sum(1 for e in entries if e["classification"] == k)
              for k in ("breaking", "non_breaking", "cosmetic", "unknown")}
    downstream = sorted({d["model"] for e in entries for d in e.get("downstream", [])})
    violations = [f for f in flags if f["blocks"]]
    if not entries:
        status, next_step = "ok", "No SQL model changed between base and head."
    elif violations:
        status = "block"
        next_step = ("Do not merge yet: " + "; ".join(v["message"] for v in violations[:3])
                     + (f" (+{len(violations) - 3} more)" if len(violations) > 3 else "")
                     + ". Fix the models; only a human may relax [review] in gulliver.toml.")
    elif flags or counts["unknown"] or (totals or {}).get("not_estimated"):
        status = "warn"
        next_step = "Nothing is over the review policy, but read the flags. Post the markdown report on the PR."
    else:
        status = "ok"
        next_step = "No risky changes found. Post the markdown report on the PR."
    if not estimated:
        notes.append("No connection with data: cost was not estimated. Pass --dsn or a connection to get "
                     "scan and money per run.")
    elif not rehearsed:
        notes.append("Estimates only: nothing was run.")
    if any(e.get("compiled") == "approximate" for e in entries):
        notes.append("Some models use Jinja beyond ref/source/config; Gulliver strips it, so their analysis is "
                     "approximate. Use --manifest-base/--manifest-head with compiled dbt manifests for exact SQL.")
    result: dict[str, Any] = {
        "status": status,
        "action": "review",
        "mode": collected["mode"],
        "base": collected["base"],
        "head": collected["head"],
        "dialect": dialect,
        "summary": {"changed": len(entries), **counts, "downstream_models": len(downstream),
                    "new_lint": sum(len(e["lint"]["introduced"]) for e in entries),
                    "flags": len(flags), "violations": len(violations)},
        "models": entries,
        "flags": flags,
        "violations": violations,
        "policy": policy.as_dict(),
        "notes": notes,
        "next_step": next_step,
    }
    if collected.get("models_dir"):
        result["models_dir"] = collected["models_dir"]
    if totals is not None:
        result["cost"] = totals
    return result


# -- markdown ---------------------------------------------------------------------------------------------
def _signed_bytes(value: int | None) -> str:
    from .core import _fmt_bytes

    if value is None:
        return "unknown"
    return ("+" if value > 0 else "-" if value < 0 else "") + _fmt_bytes(abs(value))


def _signed_usd(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "$0"
    sign = "+" if value > 0 else "-"
    v = abs(value)
    return f"{sign}${v:,.4f}" if v < 1 else f"{sign}${v:,.2f}"


def _scan_cell(cost: dict[str, Any] | None) -> str:
    from .core import _fmt_bytes

    if not cost:
        return "-"
    if "error" in cost:
        return "not estimated"
    pct = cost.get("bytes_delta_pct")
    return (f"{_fmt_bytes(cost.get('bytes_base'))} -> {_fmt_bytes(cost.get('bytes_head'))}"
            + (f" ({pct:+.0f}%)" if pct is not None else ""))


def _growth_cell(growth: dict[str, Any] | None) -> str:
    if not growth:
        return "-"
    k_new, k_old = growth.get("head_exponent"), growth.get("base_exponent")
    if k_new is None:
        return str(growth.get("head_status") or "-")
    text = f"rows ^{k_new:.2f}" + (f" (was ^{k_old:.2f})" if k_old is not None else "")
    return f"**{text}**" if growth.get("superlinear") else text


def _ref_text(side: dict[str, Any]) -> str:
    if side.get("sha"):
        return f"`{side.get('ref')}` ({side['sha'][:8]})"
    return f"`{side.get('manifest')}`"


def render_markdown(result: dict[str, Any], details: bool = True) -> str:
    from .core import _fmt_usd

    if result.get("action") != "review" or "models" not in result:
        lines = [MARKER, f"## Gulliver review: {str(result.get('status', '?')).upper()}", ""]
        if result.get("error"):
            lines.append(f"**Error:** {result['error']}")
        lines += [str(result.get("next_step", "")), ""]
        return "\n".join(lines)
    summary = result["summary"]
    lines = [MARKER, f"## Gulliver review: {result['status'].upper()}", ""]
    head_line = f"{_ref_text(result['base'])} ... {_ref_text(result['head'])} - {summary['changed']} model(s) changed"
    cost = result.get("cost")
    if cost:
        head_line += (f" - scan {_signed_bytes(cost['bytes_delta'])} per run - {_signed_usd(cost['usd_delta_per_month'])}"
                      f" per month at {cost['runs_per_day']:g} run(s)/day ({cost['accuracy']})")
    lines += [head_line, ""]
    if not result["models"]:
        lines += ["No SQL model changed.", ""]
    if result["violations"]:
        lines += ["**Blocks the merge**", ""] + [f"- `{v['kind']}` {v['message']}" for v in result["violations"]] + [""]
    others = [f for f in result["flags"] if not f["blocks"]]
    if others:
        lines += ["**Worth a look**", ""] + [f"- `{f['kind']}` {f['message']}" for f in others] + [""]
    if result["models"]:
        lines += ["| Model | Change | Downstream | Scan per run | $ per month | New lint | Growth |",
                  "|---|---|---|---|---|---|---|"]
        for e in result["models"]:
            c = e.get("cost") or {}
            lint_rules = ", ".join(sorted({f["rule"] for f in e["lint"]["introduced"]})) or "-"
            month = _signed_usd(c.get("usd_delta_per_month")) if "usd_delta_per_month" in c else "-"
            lines.append(f"| `{e['model']}` | {e['classification']} ({e['change']}) | {len(e.get('downstream', []))} "
                         f"| {_scan_cell(e.get('cost'))} | {month} | {lint_rules} | {_growth_cell(e.get('growth'))} |")
        lines.append("")
    if details:
        for e in result["models"]:
            if e["classification"] == "cosmetic" and not e["lint"]["introduced"]:
                continue
            lines += [f"<details><summary><code>{e['model']}</code> ({e['path']}): {e['classification']}</summary>", ""]
            lines += [f"- {r}" for r in e["reasons"]]
            if e.get("compiled") == "approximate":
                lines.append("- Jinja beyond ref/source/config was stripped: the analysis is approximate")
            for d in e.get("downstream", []):
                cols = ", ".join(d["columns"]) or ("SELECT *" if d["select_star"] else "whole model / filters")
                again = " (also changed here)" if d.get("changed_in_this_change") else ""
                lines.append(f"- downstream `{d['model']}`{again}: {cols} <- {', '.join(d['via'])}")
            for f in e["lint"]["introduced"]:
                lines.append(f"- new lint **{f['severity']}** {f['rule']} {f['name']}: {f['message']} Fix: {f['fix']}")
            if e["lint"]["fixed"]:
                lines.append("- lint fixed: " + ", ".join(e["lint"]["fixed"]))
            c = e.get("cost") or {}
            if "error" in c:
                lines.append(f"- cost: {c['note']}: {c['error']}")
            elif c:
                lines.append(f"- cost per run: {_fmt_usd(c.get('usd_base'))} -> {_fmt_usd(c.get('usd_head'))}, "
                             f"scan {_scan_cell(c)} ({c.get('accuracy')})")
                if c.get("head_gate"):
                    lines.append("- the new version would be blocked by the budget gate: " + "; ".join(c["head_gate"]))
            g = e.get("growth") or {}
            for side in ("base", "head"):
                if g.get(f"{side}_note"):
                    lines.append(f"- rehearsal ({side}): {g[f'{side}_status']}: {g[f'{side}_note']}")
            lines += ["", "</details>", ""]
    if result.get("notes"):
        lines += ["**Notes**", ""] + [f"- {n}" for n in result["notes"]] + [""]
    policy = result["policy"]
    lines.append(f"<sub>Gulliver {__version__} - fail_on: {', '.join(policy['fail_on']) or 'none'} - "
                 f"max_increase_pct: {policy['max_increase_pct']:g} - max_increase_usd: "
                 f"${policy['max_increase_usd']:g}/month. Cost: BigQuery dry runs are exact, Snowflake is an upper "
                 "bound, DuckDB and Postgres are heuristic.</sub>")
    text = "\n".join(lines) + "\n"
    if len(text) > MAX_MARKDOWN and details:
        text = render_markdown(result, details=False)
        text = text.replace(MARKER + "\n", MARKER + "\n> Details were cut to fit a PR comment; see the JSON "
                                                    "report for everything.\n", 1)
    return text


# -- CLI and MCP glue (kept here so the shared files only gain a few lines) ------------------------------------
def add_arguments(p: Any) -> None:
    p.add_argument("--base", default=None, help="Base ref (default: origin/$GITHUB_BASE_REF, else origin/main)")
    p.add_argument("--head", default="HEAD", help="Head ref (default: HEAD)")
    p.add_argument("--models", help="Folder with SQL models inside the git repository")
    p.add_argument("--manifest-base", help="dbt manifest.json of the base (production) instead of git")
    p.add_argument("--manifest-head", help="dbt manifest.json of the branch")
    p.add_argument("--rehearse", action="store_true", help="Also run changed models on 1%% and 3%% samples")
    p.add_argument("--dialect", default=None, help="duckdb, postgres, bigquery or snowflake (default: connection's)")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown", help="Format written by -o")
    p.add_argument("-o", "--output", metavar="PATH", help="Write the report to PATH")
    p.add_argument("--fail-on", help="Comma-separated: " + ", ".join(FLAG_KINDS) + " (or none)")
    p.add_argument("--max-increase-pct", type=float)
    p.add_argument("--max-increase-usd", type=float, help="Per month")
    p.add_argument("--runs-per-day", type=float)


def default_base() -> str:
    branch = os.environ.get("GITHUB_BASE_REF")
    return f"origin/{branch}" if branch else "origin/main"


def cli(args: Any) -> int:
    from .cli import EXIT
    from .config import load_settings

    try:
        overrides = {"fail_on": args.fail_on, "max_increase_pct": args.max_increase_pct,
                     "max_increase_usd": args.max_increase_usd, "runs_per_day": args.runs_per_day}
        policy = load_policy(args.config, overrides)
        settings = load_settings(config_path=args.config, dsn=args.dsn)
        result = review(settings, base=args.base or default_base(), head=args.head, models_dir=args.models,
                        manifest_base=args.manifest_base, manifest_head=args.manifest_head,
                        connection=args.connection, rehearse=args.rehearse, policy=policy, dialect=args.dialect)
    except ConfigError as exc:
        result = {"status": "error", "error": str(exc), "next_step": "Fix gulliver.toml or the flags."}
    markdown = render_markdown(result)
    if args.output:
        text = json.dumps(result, indent=2, default=str) if args.format == "json" else markdown
        Path(args.output).write_text(text, encoding="utf-8")
    print(json.dumps(result, indent=2, default=str) if args.json else markdown)
    return EXIT.get(str(result.get("status")), 1)


def for_agent(settings: Settings, markdown: bool = False, **kwargs: Any) -> dict[str, Any]:
    """The MCP tool: the review, plus the PR-comment markdown on request."""
    result = review(settings, **kwargs)
    if markdown:
        result["markdown"] = render_markdown(result)
    return result
