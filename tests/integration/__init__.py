"""Integration tests, these need a real PostgreSQL.

They are skipped automatically when no database is reachable, so
``pytest tests/`` still works on a laptop with nothing running. CI provides a
PostgreSQL service container, so they do run there (M17 / N6).
"""
