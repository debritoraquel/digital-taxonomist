"""Unit tests for the Neo4j connection module (no live Neo4j required)."""

import os
from unittest.mock import MagicMock, patch

import pytest
from neo4j.exceptions import ServiceUnavailable

# Patch at the module level so GraphDatabase is replaced before get_driver runs
_NEO4J_MOD = "graph.connection.GraphDatabase"


# ---------------------------------------------------------------------------
# get_driver — credential resolution
# ---------------------------------------------------------------------------


class TestGetDriver:
    def test_uses_explicit_args(self):
        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = MagicMock()
            from graph.connection import get_driver

            get_driver("bolt://host:7687", "alice", "secret")

        mock_gdb.driver.assert_called_once_with(
            "bolt://host:7687", auth=("alice", "secret")
        )

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "bolt://envhost:9999")
        monkeypatch.setenv("NEO4J_USER", "envuser")
        monkeypatch.setenv("NEO4J_PASSWORD", "envpass")

        import importlib
        import graph.connection as conn_mod
        importlib.reload(conn_mod)

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = MagicMock()
            conn_mod.get_driver()

        mock_gdb.driver.assert_called_once_with(
            "bolt://envhost:9999", auth=("envuser", "envpass")
        )

    def test_falls_back_to_defaults_when_no_env(self, monkeypatch):
        monkeypatch.delenv("NEO4J_URI", raising=False)
        monkeypatch.delenv("NEO4J_USER", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

        import importlib
        import graph.connection as conn_mod
        importlib.reload(conn_mod)

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = MagicMock()
            conn_mod.get_driver()

        mock_gdb.driver.assert_called_once_with(
            "bolt://localhost:7687", auth=("neo4j", "taxonomist2026")
        )

    def test_explicit_args_override_env(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "bolt://envhost:9999")
        monkeypatch.setenv("NEO4J_PASSWORD", "envpass")

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = MagicMock()
            from graph.connection import get_driver

            get_driver(uri="bolt://explicit:7687", password="explicit_pass")

        mock_gdb.driver.assert_called_once_with(
            "bolt://explicit:7687", auth=("neo4j", "explicit_pass")
        )


# ---------------------------------------------------------------------------
# get_driver_with_retry — retry behaviour
# ---------------------------------------------------------------------------


class TestGetDriverWithRetry:
    def test_returns_driver_on_first_success(self):
        mock_driver = MagicMock()

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = mock_driver
            mock_driver.verify_connectivity.return_value = None

            from graph.connection import get_driver_with_retry

            driver = get_driver_with_retry(retries=3)

        assert driver is mock_driver
        assert mock_gdb.driver.call_count == 1

    def test_retries_on_service_unavailable(self):
        mock_driver = MagicMock()
        mock_driver.verify_connectivity.side_effect = [
            ServiceUnavailable("not ready"),
            None,  # second attempt succeeds
        ]

        with patch(_NEO4J_MOD) as mock_gdb, patch("graph.connection.time.sleep"):
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import get_driver_with_retry

            driver = get_driver_with_retry(retries=2)

        assert mock_driver.verify_connectivity.call_count == 2

    def test_raises_after_all_retries_exhausted(self):
        mock_driver = MagicMock()
        mock_driver.verify_connectivity.side_effect = ServiceUnavailable("still down")

        with patch(_NEO4J_MOD) as mock_gdb, patch("graph.connection.time.sleep"):
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import get_driver_with_retry

            with pytest.raises(ServiceUnavailable):
                get_driver_with_retry(retries=2)


# ---------------------------------------------------------------------------
# check_connectivity
# ---------------------------------------------------------------------------


class TestCheckConnectivity:
    def test_returns_true_when_reachable(self):
        mock_driver = MagicMock()
        mock_driver.verify_connectivity.return_value = None

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import check_connectivity

            assert check_connectivity() is True

        mock_driver.close.assert_called_once()

    def test_returns_false_when_unreachable(self):
        mock_driver = MagicMock()
        mock_driver.verify_connectivity.side_effect = ServiceUnavailable("down")

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import check_connectivity

            assert check_connectivity() is False

        mock_driver.close.assert_called_once()

    def test_closes_driver_even_on_error(self):
        mock_driver = MagicMock()
        mock_driver.verify_connectivity.side_effect = RuntimeError("unexpected")

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import check_connectivity

            assert check_connectivity() is False

        mock_driver.close.assert_called_once()


# ---------------------------------------------------------------------------
# managed_driver — context manager
# ---------------------------------------------------------------------------


class TestManagedDriver:
    def test_yields_driver_and_closes(self):
        mock_driver = MagicMock()

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import managed_driver

            with managed_driver() as d:
                assert d is mock_driver

        mock_driver.close.assert_called_once()

    def test_closes_driver_even_on_exception(self):
        mock_driver = MagicMock()

        with patch(_NEO4J_MOD) as mock_gdb:
            mock_gdb.driver.return_value = mock_driver
            from graph.connection import managed_driver

            with pytest.raises(ValueError):
                with managed_driver():
                    raise ValueError("boom")

        mock_driver.close.assert_called_once()
