"""The engine contract. Each warehouse adapter implements these few methods."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Connection

# Accuracy labels, from most to least trustworthy.
EXACT = "exact"              # the engine itself reports what it will bill / scan
UPPER_BOUND = "upper_bound"  # the engine reports what it may read; the real run is <= this
HEURISTIC = "heuristic"      # Gulliver's own estimate from metadata and plan shape
UNKNOWN = "unknown"


class EngineError(RuntimeError):
    """A readable error from an engine (bad SQL, missing table, timeout...)."""


class MissingDriver(EngineError):
    """The optional Python driver for this engine is not installed."""


@dataclass
class TableInfo:
    name: str
    rows: int | None = None
    bytes: int | None = None
    columns: list[dict[str, Any]] | None = None
    partitioned_by: str | None = None
    clustered_by: str | None = None
    kind: str = "table"
    notes: list[str] = field(default_factory=list)

    def as_dict(self, with_columns: bool = False) -> dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if v not in (None, [], "")}
        if not with_columns:
            out.pop("columns", None)
        return out


@dataclass
class Estimate:
    bytes: int | None = None
    rows: int | None = None
    cost_usd: float | None = None
    accuracy: str = UNKNOWN
    source: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out["cost_usd"] is not None:
            out["cost_usd"] = round(out["cost_usd"], 6)
        if not out["notes"]:
            del out["notes"]
        return out


@dataclass
class ExecResult:
    columns: list[str]
    preview: list[dict[str, Any]]
    rows: int
    elapsed_s: float
    bytes_processed: int | None = None
    cost_usd: float | None = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


class Engine(ABC):
    """Base class for adapters. Subclasses set `name` and `dialect`."""

    name = "base"
    dialect = "duckdb"
    #: the engine bills money per query (BigQuery, Snowflake) rather than being free/self-hosted
    billed = False

    def __init__(self, connection: Connection, read_only: bool = True):
        self.connection = connection
        self.params = connection.params
        self.read_only = read_only
        #: labels for the next execute (who, why, which receipt); engines attach them where they can
        self.tags: dict[str, str] = {}

    # -- lifecycle -----------------------------------------------------------------
    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- metadata ------------------------------------------------------------------
    @abstractmethod
    def list_tables(self, limit: int = 50) -> list[TableInfo]:
        """Largest tables first."""

    @abstractmethod
    def table_info(self, ref: str) -> TableInfo | None:
        """Metadata for one table reference (as written in SQL). None if unknown."""

    # -- estimation and execution ---------------------------------------------------
    @abstractmethod
    def estimate(self, sql: str) -> Estimate:
        """Estimate bytes, rows and cost without running the query."""

    @abstractmethod
    def execute(self, sql: str, preview_rows: int, timeout_s: float | None = None,
                max_bytes_billed: int | None = None) -> ExecResult:
        """Run the query, stream the result, keep `preview_rows` rows, count the rest."""

    @abstractmethod
    def sample_clause(self, fraction: float, seed: int, method: str) -> str:
        """The clause that follows a table name to sample it, e.g. 'TABLESAMPLE 1% (system, 42)'."""

    def supported_methods(self) -> tuple[str, ...]:
        return ("system", "bernoulli")

    def data_version(self, tables: list[str]) -> str | None:
        """A value that changes whenever the data in these tables changes, or None when there is no
        reliable signal. The runaway guard only reuses an earlier result when this is not None."""
        return None

    def cost_for(self, bytes_processed: int | None, runtime_s: float | None) -> float | None:
        """Money for a run with these measurements. Free engines return 0."""
        return 0.0

    def max_bytes_for_cost(self, usd: float) -> int | None:
        """Bytes that `usd` buys (used for engine-side hard caps). None if not byte-billed."""
        return None

    def baseline_latency(self, tries: int = 3) -> float:
        """Fixed overhead of one trivial query, in seconds (the smallest of a few tries)."""
        best = float("inf")
        for _ in range(tries):
            start = time.perf_counter()
            try:
                self.execute("SELECT 1", preview_rows=1)
            except EngineError:
                return 0.0
            best = min(best, time.perf_counter() - start)
        return best if best != float("inf") else 0.0

    # -- portable SQL pieces for data-diff and checks ---------------------------------
    #: SQL type name for text casts
    text_type = "VARCHAR"

    def text(self, expr: str) -> str:
        """Expression as text, NULL shown as a fixed marker (so NULL and '' differ)."""
        return f"COALESCE(CAST({expr} AS {self.text_type}), '<gv-null>')"

    def text_part(self, expr: str) -> str:
        """Escape a text part before joining keys or rows with '|'."""
        return f"REPLACE(REPLACE({self.text(expr)}, '~', '~~'), '|', '~p')"

    def md5_int(self, text_expr: str) -> str:
        """First 7 hex digits of MD5(text) as an integer (0..268435455). Same value on every engine."""
        return f"CAST(('0x' || SUBSTR(MD5({text_expr}), 1, 7)) AS BIGINT)"

    def quote(self, name: str) -> str:
        import sqlglot.expressions as e

        return e.to_identifier(name, quoted=True).sql(dialect=self.dialect)

    def query_columns(self, sql: str) -> list[str]:
        """Output column names of a query without reading data."""
        return self.execute(f"SELECT * FROM ({sql}) AS gv_q LIMIT 0", preview_rows=0).columns

    def describe(self) -> dict[str, Any]:
        return {"engine": self.name, "dialect": self.dialect, "billed": self.billed, "read_only": self.read_only}
