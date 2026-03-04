"""Pytest fixtures for system tests."""

import os
import sys

import pytest

# Ensure tests/system/ is on sys.path so helpers and test_app are importable
sys.path.insert(0, os.path.dirname(__file__))

from test_app import create_test_sandbox
from helpers import start_airflow_and_wait_ready

# Don't collect DAG files as tests
collect_ignore_glob = ["dags/*"]


@pytest.fixture(scope="module")
def sandbox():
    """Create a Modal Sandbox for the test module, terminate on teardown."""
    sb = create_test_sandbox()
    yield sb
    sb.terminate()


@pytest.fixture(scope="module")
def airflow_ready(sandbox):
    """Start Airflow standalone and wait until it's ready."""
    start_airflow_and_wait_ready(sandbox)
    yield
