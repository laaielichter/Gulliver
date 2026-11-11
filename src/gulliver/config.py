"""Configuration: connections, budgets and where Gulliver keeps its local state.

Sources, later ones win:
  1. built-in defaults
  2. gulliver.toml (current directory, or the path in GULLIVER_CONFIG)
  3. environment variables (GULLIVER_DSN, GULLIVER_MAX_QUERY_USD, ...)
  4. explicit arguments (CLI flags / tool arguments)

Credentials never live in the config file: engines read them from their own
environment variables or default credential chains (for example Google ADC).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from .checks import CheckConfig

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(ValueError):
    """Raised when the configuration cannot be used."""


# top-level keys of gulliver.toml; [lint] is read in lint_files.py and [review] in review.py
KNOWN_TOP_LEVEL = frozenset(
    {"connections", "default_connection", "budget", "approval", "state_dir", "lint", "review", "checks"}
)


@dataclass(frozen=True)
class Budget:
    """Spending and safety limits. Money is in US dollars."""

    max_query_usd: float = 1.00          # hard stop for one query
    approval_over_usd: float = 0.25      # above this a human must approve the run
    rehearse_over_usd: float = 0.10      # above this a rehearsal is required first
    rehearse_over_bytes: int = 10 * 1024**3   # same rule when the cost is unknown / free
    max_bytes: int = 500 * 1024**3       # hard stop on estimated bytes scanned
    max_runtime_s: float = 900.0         # hard stop on predicted runtime
    day_usd: float = 10.00               # total spend per calendar day (local ledger)
    max_output_rows: int = 200           # rows handed back to the agent
    max_output_chars: int = 50_000       # characters of preview handed back (0 = no limit); see guard.py
    max_cell_chars: int = 1_000          # longer text cells are cut in the preview (0 = no limit)
    # runaway guard (runaway.py): 0 switches a limit off
    session_usd: float = 0.0             # spend per agent session (MCP connection / GULLIVER_SESSION)
    agent_day_usd: dict[str, float] = field(default_factory=dict, hash=False)  # {default = 5, "claude-code" = 10}
    max_repeats: int = 5                 # identical runs allowed per window before the loop guard steps in
    repeat_window_min: float = 30.0
    velocity_usd: float = 2.00           # one agent/session spending more than this ...
    velocity_window_min: float = 10.0    # ... within this many minutes needs a human approval

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Budget:
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"Unknown budget keys: {', '.join(sorted(unknown))}")
        return cls(**{k: (_agent_limits(v) if k == "agent_day_usd" else _coerce(cls, k, v)) for k, v in data.items()})

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_ENV_BUDGET = {
    "GULLIVER_MAX_QUERY_USD": "max_query_usd",
    "GULLIVER_APPROVAL_OVER_USD": "approval_over_usd",
    "GULLIVER_REHEARSE_OVER_USD": "rehearse_over_usd",
    "GULLIVER_REHEARSE_OVER_BYTES": "rehearse_over_bytes",
    "GULLIVER_MAX_BYTES": "max_bytes",
    "GULLIVER_MAX_RUNTIME_S": "max_runtime_s",
    "GULLIVER_DAY_USD": "day_usd",
    "GULLIVER_MAX_OUTPUT_ROWS": "max_output_rows",
    "GULLIVER_MAX_OUTPUT_CHARS": "max_output_chars",
    "GULLIVER_MAX_CELL_CHARS": "max_cell_chars",
    "GULLIVER_SESSION_USD": "session_usd",
    "GULLIVER_AGENT_DAY_USD": "agent_day_usd",
    "GULLIVER_MAX_REPEATS": "max_repeats",
    "GULLIVER_REPEAT_WINDOW_MIN": "repeat_window_min",
    "GULLIVER_VELOCITY_USD": "velocity_usd",
}


def _agent_limits(value: Any) -> dict[str, float]:
    """agent_day_usd = 5 (every agent) or a table {default = 5, "claude-code" = 10}."""
    items = value.items() if isinstance(value, dict) else [("default", value)]
    limits: dict[str, float] = {}
    for name, limit in items:
        if isinstance(limit, bool):
            raise ConfigError(f"Budget value agent_day_usd[{name!r}]={limit!r} must be a number, not a boolean")
        try:
            number = float(limit)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"Budget value agent_day_usd={value!r} must be a number or a table of numbers"
            ) from exc
        limits[str(name)] = _check_limit(f"agent_day_usd[{name!r}]", limit, number)
    return limits


def _check_limit(key: str, raw: Any, number: float) -> float:
    if not math.isfinite(number):
        raise ConfigError(f"Budget value {key}={raw!r} must be finite, not NaN or infinity")
    if number < 0:
        raise ConfigError(f"Budget value {key}={raw!r} must not be negative; use 0 to switch a limit off")
    return number


def _coerce(cls: type, key: str, value: Any) -> Any:
    default = getattr(cls, key)
    if isinstance(value, bool):
        raise ConfigError(f"Budget value {key}={value!r} must be a number, not a boolean")
    if isinstance(default, int):
        # whole-number limits: a fraction would be silently truncated, and 0 switches a limit off
        if isinstance(value, int):
            coerced = value
        elif isinstance(value, float) and value.is_integer():
            coerced = int(value)
        elif isinstance(value, str):
            try:
                coerced = int(value)
            except ValueError as exc:
                raise ConfigError(f"Budget value {key}={value!r} must be a whole number") from exc
        else:
            raise ConfigError(f"Budget value {key}={value!r} must be a whole number")
        if coerced < 0:
            raise ConfigError(f"Budget value {key}={value!r} must not be negative; use 0 to switch a limit off")
        return coerced
    try:
        coerced = type(default)(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"Budget value {key}={value!r} is not a valid {type(default).__name__}") from exc
    _check_limit(key, value, coerced)
    return coerced


@dataclass(frozen=True)
class Connection:
    """One named data source. `params` holds engine-specific settings."""

    name: str
    engine: str
    params: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        """What an agent may see: no secrets, no DSN passwords."""
        safe = {k: v for k, v in self.params.items() if not _looks_secret(k)}
        if "dsn" in safe:
            safe["dsn"] = _redact_dsn(str(safe["dsn"]))
        return {"name": self.name, "engine": self.engine, **safe}


@dataclass(frozen=True)
class Settings:
    connections: dict[str, Connection]
    default_connection: str
    budget: Budget
    state_dir: Path
    approval_channel: str = "cli"   # [approval] channel: "cli" (default) or "elicitation" (MCP client asks the human)
    checks: dict[str, CheckConfig] = field(default_factory=dict)

    def connection(self, name: str | None = None) -> Connection:
        key = name or self.default_connection
        if key not in self.connections:
            known = ", ".join(sorted(self.connections)) or "(none)"
            raise ConfigError(f"Unknown connection '{key}'. Known connections: {known}")
        return self.connections[key]


def _looks_secret(key: str) -> bool:
    k = key.lower()
    return any(word in k for word in ("password", "secret", "token", "private_key", "passphrase"))


def _redact_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    if parsed.password:
        netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
        return parsed._replace(netloc=netloc).geturl()
    return dsn


def connection_from_dsn(dsn: str, name: str = "default") -> Connection:
    """Turn a DSN string into a Connection.

    duckdb:///path/to/file.duckdb   duckdb://:memory:   path/to/file.duckdb
    postgresql://user@host:5432/db  (password via PGPASSWORD)
    bigquery://my-project?location=EU
    snowflake://ACCOUNT?warehouse=WH&database=DB&schema=PUBLIC&user=ME
    """
    dsn = dsn.strip()
    if dsn in ("", ":memory:", "duckdb://:memory:", "duckdb://"):
        return Connection(name, "duckdb", {"path": ":memory:"})
    if "://" not in dsn:
        return Connection(name, "duckdb", {"path": dsn})
    parsed = urlparse(dsn)
    scheme = parsed.scheme.lower()
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    if scheme == "duckdb":
        path = (parsed.netloc + parsed.path) or ":memory:"
        if path.startswith("/") and dsn.startswith("duckdb:///"):
            path = parsed.path
        return Connection(name, "duckdb", {"path": path, **query})
    if scheme in ("postgres", "postgresql"):
        return Connection(name, "postgres", {"dsn": dsn})
    if scheme == "bigquery":
        params: dict[str, Any] = {"project": parsed.netloc or None, **query}
        return Connection(name, "bigquery", params)
    if scheme == "snowflake":
        return Connection(name, "snowflake", {"account": parsed.netloc, **query})
    raise ConfigError(f"Unsupported DSN scheme '{scheme}'. Use duckdb, postgresql, bigquery or snowflake.")


def find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("GULLIVER_CONFIG"):
        candidates.append(Path(os.environ["GULLIVER_CONFIG"]))
    candidates.append(Path.cwd() / "gulliver.toml")
    for path in candidates:
        if path.is_file():
            return path
    if explicit:
        raise ConfigError(f"Config file not found: {explicit}")
    return None


def load_settings(
    config_path: str | os.PathLike[str] | None = None,
    dsn: str | None = None,
    budget_overrides: dict[str, Any] | None = None,
) -> Settings:
    data: dict[str, Any] = {}
    path = find_config_file(config_path)
    if path:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        unknown = set(data) - KNOWN_TOP_LEVEL
        if unknown:
            raise ConfigError(
                f"Unknown section(s) in gulliver.toml: {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(KNOWN_TOP_LEVEL))}."
            )

    connections: dict[str, Connection] = {}
    for conn_name, conn_data in (data.get("connections") or {}).items():
        conn_data = dict(conn_data)
        if "dsn" in conn_data and "engine" not in conn_data:
            conn = connection_from_dsn(str(conn_data.pop("dsn")), conn_name)
            conn = replace(conn, params={**conn.params, **conn_data})
        else:
            engine = conn_data.pop("engine", None)
            if not engine:
                raise ConfigError(f"Connection '{conn_name}' needs an 'engine' or a 'dsn'.")
            conn = Connection(conn_name, str(engine).lower(), conn_data)
        connections[conn_name] = conn

    default = data.get("default_connection")
    env_dsn = dsn or os.environ.get("GULLIVER_DSN")
    if env_dsn:
        connections["default"] = connection_from_dsn(env_dsn, "default")
        default = "default"
    if not connections:
        connections["default"] = Connection("default", "duckdb", {"path": ":memory:"})
    if not default or default not in connections:
        default = next(iter(connections))

    budget_data = dict(data.get("budget") or {})
    for env_key, attr in _ENV_BUDGET.items():
        if os.environ.get(env_key):
            budget_data[attr] = os.environ[env_key]
    if budget_overrides:
        budget_data.update({k: v for k, v in budget_overrides.items() if v is not None})
    budget = Budget.from_mapping(budget_data)

    state_dir = Path(os.environ.get("GULLIVER_HOME") or data.get("state_dir") or (Path.cwd() / ".gulliver"))
    checks: dict[str, Any] = {}
    for check_name, check_data in (data.get("checks") or {}).items():
        if not isinstance(check_data, dict):
            raise ConfigError(f"[checks.{check_name}] must be a table")
        from .checks import parse_check_config

        checks[check_name] = parse_check_config(str(check_name), check_data)

    return Settings(connections=connections, default_connection=default, budget=budget, state_dir=state_dir,
                    approval_channel=_approval_channel(data.get("approval") or {}), checks=checks)


APPROVAL_CHANNELS = ("cli", "elicitation")


def _approval_channel(data: dict[str, Any]) -> str:
    """[approval] in gulliver.toml only (a human setting, no environment variable on purpose)."""
    unknown = set(data) - {"channel"}
    if unknown:
        raise ConfigError(f"Unknown approval keys: {', '.join(sorted(unknown))}")
    channel = str(data.get("channel", "cli")).lower()
    if channel not in APPROVAL_CHANNELS:
        raise ConfigError(f"[approval] channel must be one of {', '.join(APPROVAL_CHANNELS)}, not {channel!r}")
    return channel
