# Privacy

Gulliver is a local tool. It does not collect, send or sell any data about you or your databases.

- **No telemetry.** Gulliver has no analytics, tracking or usage reporting.
- **Your SQL and your data stay with you.** Gulliver runs on your machine and only connects to the databases you
  configure (DuckDB, Postgres, BigQuery, Snowflake). Query results go back to the agent or terminal that asked
  for them, with secrets masked and personal data flagged.
- **Local state only.** Receipts, the spend ledger and approvals are JSON files in `./.gulliver/` on your machine.
  You can read or delete them at any time.
- **Credentials stay local.** Connection strings and passwords come from your own `gulliver.toml` or environment
  variables (such as `PGPASSWORD`). Gulliver passes them only to the database driver for that connection.
- **Installing.** `uvx`, `pip` and `npx` download the `gulliver-mcp` package from PyPI or npm. Those services see
  that download, under their own privacy policies.

Your AI client (for example Claude Code) sees the tool results Gulliver returns to it. How that client handles
them is covered by the client's own privacy policy.

Questions: open an issue at https://github.com/laaielichter/Gulliver/issues. Security problems: see
[SECURITY.md](SECURITY.md).
