"""Shared fixtures for all Fridge-server integration tests.

Session-scoped NetClient instances are built once per test run from .env /
environment variables, so every test suite can import api_client and gf_client
without duplicating connection setup.
"""

import os

import pytest
from dotenv import load_dotenv

from client import NetClient

load_dotenv()

# ── Files not yet migrated from standalone-script style to pytest ─────────────
# Remove a file from this list once it has been converted.
collect_ignore = [
    "testui/e2e_mail_test.py",       # standalone script for manual production runs
    "testui/test_no_data_state.py",  # standalone script, not yet converted to pytest
    "testslack/test_alerts.py",      # standalone CLI tool, not yet converted to pytest
]


# ── Service clients ───────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def gf_client() -> NetClient:
    """Grafana admin client, authenticated via GF_ADMIN_USER / GF_ADMIN_PASSWORD."""
    return NetClient(
        base_url=os.getenv("GRAFANA_URL", "http://localhost:3000"),
        username=os.getenv("GF_ADMIN_USER", ""),
        password=os.getenv("GF_ADMIN_PASSWORD", ""),
    )


@pytest.fixture(scope="session")
def api_client() -> NetClient:
    """Alert-api client, authenticated via GF_ADMIN_USER / GF_ADMIN_PASSWORD."""
    return NetClient(
        base_url=os.getenv("ALERT_API_URL", "http://localhost:8000/api"),
        username=os.getenv("GF_ADMIN_USER", ""),
        password=os.getenv("GF_ADMIN_PASSWORD", ""),
    )


# ── Default test parameters (overridable via env) ─────────────────────────────


@pytest.fixture(scope="session")
def test_fridge() -> str:
    return os.getenv("TEST_FRIDGE", "fridge-dodo")


@pytest.fixture(scope="session")
def test_metric() -> str:
    return os.getenv("TEST_METRIC", "ch1_t_kelvin")


@pytest.fixture(scope="session")
def mailpit_url() -> str:
    """Mailpit HTTP API base URL. Used for inbox checks in e2e tests.
    Set MAILPIT_URL env var in CI; leave unset to skip mailpit-dependent tests."""
    return os.getenv("MAILPIT_URL", "http://localhost:8025")


@pytest.fixture(scope="session")
def pushgateway_url() -> str:
    return os.getenv("PUSHGATEWAY_URL", "http://localhost:9091")
