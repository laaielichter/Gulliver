import pytest

from gulliver.config import ConfigError, connection_from_dsn, load_settings


def test_dsn_parsing():
    assert connection_from_dsn("data.duckdb").params == {"path": "data.duckdb"}
    assert connection_from_dsn("duckdb:///tmp/x.duckdb").params["path"] == "/tmp/x.duckdb"
    assert connection_from_dsn(":memory:").params == {"path": ":memory:"}
    pg = connection_from_dsn("postgresql://me:secret@db:5432/app")
    assert pg.engine == "postgres"
    assert "secret" not in str(pg.public_dict())
    bq = connection_from_dsn("bigquery://my-proj?location=EU")
    assert bq.engine == "bigquery" and bq.params == {"project": "my-proj", "location": "EU"}
    sf = connection_from_dsn("snowflake://acme-xy12?warehouse=WH&user=ME")
    assert sf.engine == "snowflake" and sf.params["warehouse"] == "WH"
    with pytest.raises(ConfigError):
        connection_from_dsn("mysql://x")


def test_toml_connections_and_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GULLIVER_DSN", raising=False)
    (tmp_path / "gulliver.toml").write_text(
        'default_connection = "bq"\n'
        "[connections.local]\nengine = \"duckdb\"\npath = \"x.duckdb\"\n"
        "[connections.bq]\ndsn = \"bigquery://proj\"\nprice_per_tib_usd = 5.0\n"
        "[budget]\nmax_query_usd = 2.5\n"
    )
    monkeypatch.setenv("GULLIVER_DAY_USD", "40")
    s = load_settings()
    assert s.default_connection == "bq"
    assert s.connection().params["price_per_tib_usd"] == 5.0
    assert s.connection("local").engine == "duckdb"
    assert s.budget.max_query_usd == 2.5
    assert s.budget.day_usd == 40.0
    with pytest.raises(ConfigError):
        s.connection("nope")


def test_unknown_budget_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gulliver.toml").write_text("[budget]\nmax_qeury_usd = 1\n")
    with pytest.raises(ConfigError):
        load_settings()


def test_unknown_top_level_section_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gulliver.toml").write_text("[budjet]\nmax_query_usd = 0.0001\n")
    with pytest.raises(ConfigError, match=r"Unknown section\(s\) in gulliver.toml: budjet\. Known: .*budget"):
        load_settings()
    # a typo without a table header is the same mistake
    (tmp_path / "gulliver.toml").write_text("budjet = 0.0001\n")
    with pytest.raises(ConfigError, match="budjet"):
        load_settings()


def test_every_known_section_loads(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "gulliver.toml").write_text(
        'default_connection = "local"\n'
        'state_dir = ".state"\n'
        "[connections.local]\nengine = \"duckdb\"\npath = \":memory:\"\n"
        "[budget]\nmax_query_usd = 2.5\n"
        "[approval]\nchannel = \"cli\"\n"
        "[lint]\nfail_on = \"error\"\n"
        "[review]\nfail_on = \"cost\"\n"
        "[checks.events]\ntarget = \"events\"\nmin_rows = 1\n"
    )
    s = load_settings()
    assert s.default_connection == "local"
    assert s.state_dir == Path(".state")
    assert s.budget.max_query_usd == 2.5
    assert s.approval_channel == "cli"
    assert "events" in s.checks and s.checks["events"].target == "events"


def test_default_is_in_memory_duckdb(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GULLIVER_DSN", raising=False)
    monkeypatch.delenv("GULLIVER_CONFIG", raising=False)
    s = load_settings()
    assert s.connection().engine == "duckdb"
    assert s.connection().params["path"] == ":memory:"


def test_example_config_loads(monkeypatch):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "gulliver.example.toml"
    monkeypatch.delenv("GULLIVER_DSN", raising=False)
    s = load_settings(config_path=example)
    assert set(s.connections) == {"local", "bq", "sf", "pg"}
    assert s.connection("bq").engine == "bigquery"
    assert s.connection("pg").engine == "postgres"
    assert s.budget.max_query_usd == 1.0


def _write_budget(tmp_path, body):
    (tmp_path / "gulliver.toml").write_text("[budget]\n" + body)


def test_non_finite_budget_from_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for body in ("max_query_usd = nan\n", "day_usd = inf\n", "max_bytes = inf\n", "max_runtime_s = -inf\n"):
        _write_budget(tmp_path, body)
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        key = body.split(" = ")[0]
        assert key in str(excinfo.value)


def test_non_finite_budget_from_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for env_key, key, value in (("GULLIVER_MAX_QUERY_USD", "max_query_usd", "nan"),
                                ("GULLIVER_DAY_USD", "day_usd", "inf"),
                                ("GULLIVER_MAX_BYTES", "max_bytes", "-inf"),
                                ("GULLIVER_REPEAT_WINDOW_MIN", "repeat_window_min", "nan")):
        monkeypatch.setenv(env_key, value)
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        assert key in str(excinfo.value)
        monkeypatch.delenv(env_key)


def test_whole_number_budget_rejects_fractions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for body in ("max_output_rows = 1.9\n", "max_repeats = 1.5\n", 'max_bytes = "20.5"\n'):
        _write_budget(tmp_path, body)
        with pytest.raises(ConfigError, match="whole number"):
            load_settings()
    # a float that is a whole number, such as 2e9, still loads
    _write_budget(tmp_path, "max_output_rows = 2e0\nmax_bytes = 2e9\n")
    s = load_settings()
    assert s.budget.max_output_rows == 2
    assert s.budget.max_bytes == 2_000_000_000


def test_whole_number_budget_from_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GULLIVER_MAX_OUTPUT_ROWS", "20")
    assert load_settings().budget.max_output_rows == 20
    monkeypatch.setenv("GULLIVER_MAX_OUTPUT_ROWS", "1.9")
    with pytest.raises(ConfigError, match="whole number"):
        load_settings()


def test_boolean_budget_values_are_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for body in ("max_query_usd = true\n", "max_output_rows = true\n",
                 "agent_day_usd = true\n", "agent_day_usd = {default = true}\n"):
        _write_budget(tmp_path, body)
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        assert "boolean" in str(excinfo.value)


def test_negative_budget_rejected_and_zero_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for body in ("max_query_usd = -1\n", "max_output_rows = -1\n", "agent_day_usd = -5\n"):
        _write_budget(tmp_path, body)
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        assert body.split(" = ")[0] in str(excinfo.value)

    _write_budget(tmp_path, "max_output_rows = 0\nsession_usd = 0\nagent_day_usd = 0\n")
    s = load_settings()
    assert s.budget.max_output_rows == 0
    assert s.budget.session_usd == 0
    assert s.budget.agent_day_usd == {"default": 0}


def test_non_finite_agent_day_usd_table(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for body in ('agent_day_usd = {default = nan}\n', 'agent_day_usd = {default = 5, "claude-code" = -1}\n'):
        _write_budget(tmp_path, body)
        with pytest.raises(ConfigError) as excinfo:
            load_settings()
        assert "agent_day_usd" in str(excinfo.value)



def test_checks_toml_parsing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gulliver.toml").write_text(
        "[connections.local]\nengine = \"duckdb\"\npath = \":memory:\"\n"
        "[checks.events]\n"
        'target = "events"\n'
        'connection = "local"\n'
        'where = "ts >= current_date - 7"\n'
        'unique = ["event_id", ["user_id", "session_id"], "a,b"]\n'
        'not_null = ["user_id", "amount"]\n'
        'accepted = { event_type = ["view", "click"] }\n'
        "range = { amount = [0, 10000] }\n"
        'fresh = { ts = "24h" }\n'
        "min_rows = 1000\n"
        "max_change_pct = 20\n"
        "samples = 5\n"
    )
    s = load_settings()
    assert "events" in s.checks
    cfg = s.checks["events"]
    assert cfg.name == "events"
    assert cfg.target == "events"
    assert cfg.connection == "local"
    assert cfg.where == "ts >= current_date - 7"
    assert cfg.samples == 5
    assert cfg.spec.unique == [["event_id"], ["user_id", "session_id"], ["a", "b"]]
    assert cfg.spec.not_null == ["user_id", "amount"]
    assert cfg.spec.accepted == {"event_type": ["view", "click"]}
    assert cfg.spec.ranges == {"amount": (0.0, 10000.0)}
    assert cfg.spec.fresh == {"ts": "24h"}
    assert cfg.spec.min_rows == 1000
    assert cfg.spec.max_change_pct == 20.0


def test_checks_unknown_keys_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "gulliver.toml").write_text(
        "[checks.events]\n"
        'target = "events"\n'
        "unknown_foo = 123\n"
    )
    with pytest.raises(ConfigError, match=r"Unknown check keys in \[checks.events\]: unknown_foo"):
        load_settings()


def test_checks_range_shapes_and_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # open bounds
    (tmp_path / "gulliver.toml").write_text(
        "[checks.t1]\nrange = { a = [0, ''] }\n"
        "[checks.t2]\nrange = { b = ['', 50] }\n"
        "[checks.t3]\nrange = { c = [10] }\n"
        "[checks.t4]\nrange = { d = { min = 1, max = 5 } }\n"
    )
    s = load_settings()
    assert s.checks["t1"].spec.ranges["a"] == (0.0, None)
    assert s.checks["t2"].spec.ranges["b"] == (None, 50.0)
    assert s.checks["t3"].spec.ranges["c"] == (10.0, None)
    assert s.checks["t4"].spec.ranges["d"] == (1.0, 5.0)

    # inverted bounds
    (tmp_path / "gulliver.toml").write_text("[checks.bad]\nrange = { x = [100, 1] }\n")
    with pytest.raises(ConfigError, match=r"min 100\.0 > max 1\.0"):
        load_settings()


def test_checks_validation_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # empty target
    (tmp_path / "gulliver.toml").write_text('[checks.bad]\ntarget = ""\n')
    with pytest.raises(ConfigError, match="target must be a non-empty string"):
        load_settings()

    # section with no checks configured
    (tmp_path / "gulliver.toml").write_text('[checks.empty]\ntarget = "users"\n')
    with pytest.raises(ConfigError, match=r"\[checks\.empty\] has no checks configured; give at least one check"):
        load_settings()

    # empty accepted list
    (tmp_path / "gulliver.toml").write_text('[checks.bad_acc]\ntarget = "users"\naccepted = { col = [] }\n')
    with pytest.raises(ConfigError, match=r"\[checks\.bad_acc\] accepted values list for 'col' cannot be empty"):
        load_settings()

    # negative min_rows
    (tmp_path / "gulliver.toml").write_text("[checks.bad]\nmin_rows = -1\n")
    with pytest.raises(ConfigError, match="min_rows must be a non-negative integer"):
        load_settings()

    # negative max_change_pct
    (tmp_path / "gulliver.toml").write_text("[checks.bad]\nmax_change_pct = -5\n")
    with pytest.raises(ConfigError, match="max_change_pct must be a non-negative number"):
        load_settings()

    # out of bounds samples
    (tmp_path / "gulliver.toml").write_text("[checks.bad]\nsamples = 25\n")
    with pytest.raises(ConfigError, match="samples must be an integer from 0 to 20"):
        load_settings()
