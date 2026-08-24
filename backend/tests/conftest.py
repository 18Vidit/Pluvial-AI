"""Shared test fixtures. Tests that touch the memory store run against a
dedicated `pluvial_test` schema in the same Neon database (not a separate
service to stand up, not the production `public` schema) — created once per
session and truncated before each test so tests never see each other's data.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

from pluvial.memory.dal import SCHEMA_PATH

load_dotenv()

TEST_SCHEMA = "pluvial_test"

MEMORY_TABLES = [
    "segments", "moisture_history", "complaints", "verdicts",
    "outcomes", "calibration", "precedents",
]


@pytest.fixture(scope="session", autouse=True)
def _test_schema():
    # DATABASE_URL is Neon's pooled (PgBouncer transaction-mode) endpoint —
    # `SET search_path` is session state that can leak onto a pooled
    # backend a later, unrelated connection then picks up. Every connection
    # that touches TEST_SCHEMA here must RESET it before closing, or the
    # next real request (e.g. `dal.connect()` in the API) can inherit a
    # search_path pointing at a schema this fixture is about to drop.
    url = os.environ["DATABASE_URL"]
    con = psycopg.connect(url)
    with con.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
        cur.execute(SCHEMA_PATH.read_text())
    con.execute("RESET search_path")
    con.commit()
    con.close()
    yield
    con = psycopg.connect(url)
    con.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
    con.execute("RESET search_path")
    con.commit()
    con.close()


@pytest.fixture()
def db():
    url = os.environ["DATABASE_URL"]
    con = psycopg.connect(url, row_factory=dict_row)
    con.execute(f"SET search_path TO {TEST_SCHEMA}")
    con.execute(f"TRUNCATE {', '.join(MEMORY_TABLES)} RESTART IDENTITY CASCADE")
    con.commit()
    try:
        yield con
    finally:
        con.execute("RESET search_path")
        con.commit()
        con.close()
