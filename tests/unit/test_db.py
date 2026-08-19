"""The shared database connection helper.

Carried over from dev and repointed at ``approvalflow.db.dsn``. The defaults
asserted here are the ones docker-compose actually provisions, so a mismatch
between the code and the compose file shows up as a failing test rather than a
connection refused at run time.
"""

from __future__ import annotations

from approvalflow.db import dsn


def test_dsn_is_built_from_the_environment(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "db.example.com")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_USER", "testuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")
    monkeypatch.setenv("POSTGRES_DB", "testdb")

    assert dsn() == "postgresql://testuser:testpass@db.example.com:5433/testdb"


def test_dsn_defaults_match_what_compose_provisions(monkeypatch):
    for name in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(name, raising=False)

    assert dsn() == "postgresql://approvalflow:approvalflow@postgres:5432/approvalflow"
