"""
Database connection manager for the Result API.

Provides async database operations using psycopg2 (no ORM).
Includes a retry mechanism so the service can start before Postgres is ready.
"""

import os
import time
import logging
import psycopg2
import psycopg2.extras
from threading import Lock

logger = logging.getLogger(__name__)

# Database URL from environment, with a sensible default for docker-compose
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://quizvote:quizvote@postgres:5432/quizvote",
)

# Connection state — lazily initialized behind a lock
_connection = None
_connection_lock = Lock()


def get_connection():
    """
    Return the shared database connection, creating it if necessary.

    The connection is lazily created on first call and reused thereafter.
    If the connection is closed or broken, a new one is created automatically.
    """
    global _connection
    with _connection_lock:
        if _connection is None or _connection.closed:
            _connection = psycopg2.connect(DATABASE_URL)
            _connection.autocommit = True
        return _connection


def execute_query(query: str, params: tuple = None):
    """
    Execute a query and return all rows as a list of dicts.

    Args:
        query: SQL query string.
        params: Optional tuple of parameters for the query.

    Returns:
        List of dictionaries representing rows.
    """
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        if cur.description is not None:
            return cur.fetchall()
        return []


def wait_for_db(max_retries: int = 15, retry_interval: int = 2):
    """
    Block until the database is reachable, retrying every `retry_interval`
    seconds up to `max_retries` attempts.

    This allows the service to start before Postgres is fully ready.
    """
    logger.info("Waiting for database...")
    for attempt in range(1, max_retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.close()
            logger.info("Database is available (attempt %d/%d)", attempt, max_retries)
            return True
        except psycopg2.OperationalError as e:
            logger.warning(
                "Database not ready yet (attempt %d/%d): %s",
                attempt,
                max_retries,
                e,
            )
            if attempt < max_retries:
                time.sleep(retry_interval)
    logger.error("Could not connect to database after %d attempts", max_retries)
    return False