"""
Centralized Neo4j connection factory with environment variable support,
retry logic, and health verification.

Environment variables (all optional, fallback to defaults):
    NEO4J_URI       bolt://localhost:7687
    NEO4J_USER      neo4j
    NEO4J_PASSWORD  taxonomist2026
"""

import logging
import os
import time
from contextlib import contextmanager
from typing import Optional

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable

logger = logging.getLogger(__name__)

_DEFAULT_URI = "bolt://localhost:7687"
_DEFAULT_USER = "neo4j"
_DEFAULT_PASSWORD = "taxonomist2026"

_RETRY_DELAYS = (2, 4, 8)  # seconds between retries


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def get_driver(
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Driver:
    """
    Return an authenticated Neo4j driver.

    Parameters fall back to NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD env vars,
    then to built-in defaults for local Docker Compose.
    """
    uri = uri or _env("NEO4J_URI", _DEFAULT_URI)
    user = user or _env("NEO4J_USER", _DEFAULT_USER)
    password = password or _env("NEO4J_PASSWORD", _DEFAULT_PASSWORD)
    return GraphDatabase.driver(uri, auth=(user, password))


def get_driver_with_retry(
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    retries: int = 3,
) -> Driver:
    """
    Like get_driver, but retries on ServiceUnavailable with exponential back-off.
    Useful during container startup before Neo4j is fully ready.
    """
    uri = uri or _env("NEO4J_URI", _DEFAULT_URI)
    user = user or _env("NEO4J_USER", _DEFAULT_USER)
    password = password or _env("NEO4J_PASSWORD", _DEFAULT_PASSWORD)

    delays = _RETRY_DELAYS[:retries]
    last_exc: Exception = ServiceUnavailable("No connection attempted")

    for attempt, delay in enumerate(delays, start=1):
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            logger.info("Connected to Neo4j at %s (attempt %d)", uri, attempt)
            return driver
        except ServiceUnavailable as exc:
            last_exc = exc
            logger.warning(
                "Neo4j not ready at %s (attempt %d/%d). Retrying in %ds…",
                uri, attempt, len(delays), delay,
            )
            time.sleep(delay)

    # One final attempt without catching — let caller handle it
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    return driver


def check_connectivity(
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> bool:
    """Return True if Neo4j is reachable, False otherwise."""
    driver = get_driver(uri, user, password)
    try:
        driver.verify_connectivity()
        return True
    except Exception:
        return False
    finally:
        driver.close()


@contextmanager
def managed_driver(
    uri: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
):
    """Context manager that opens and closes a Neo4j driver automatically."""
    driver = get_driver(uri, user, password)
    try:
        yield driver
    finally:
        driver.close()
