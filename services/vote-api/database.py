"""
Database connection manager for the Vote API.

Provides async database operations using psycopg2 (no ORM).
Includes a retry mechanism so the service can start before Postgres is ready.

Connection pool: A single persistent connection is used per request via
get_connection(), which lazily initializes on first call.
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


def execute_insert(query: str, params: tuple = None):
    """
    Execute an INSERT and return the generated id (SERIAL / RETURNING).

    Args:
        query: SQL INSERT query string.
        params: Optional tuple of parameters.

    Returns:
        The id of the inserted row, or None.
    """
    conn = get_connection()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        result = cur.fetchone()
        if result:
            return result.get("id")
        return None


def execute_update(query: str, params: tuple = None):
    """
    Execute an UPDATE/INSERT without returning a row.

    Args:
        query: SQL query string.
        params: Optional tuple of parameters.
    """
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(query, params)


def wait_for_db(max_retries: int = 15, retry_interval: int = 2):
    """
    Block until the database is reachable, retrying every `retry_interval`
    seconds up to `max_retries` attempts.

    This allows the service to start before Postgres is fully ready
    (e.g. during docker-compose initial startup).
    """
    logger.info(
        "Waiting for database at %s ...",
        DATABASE_URL.replace(DATABASE_URL.split("@")[0] if "@" in DATABASE_URL else "", "<credentials>@"),
    )
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


def init_db():
    """
    Create tables and seed default data if the quizzes table is empty.

    Called once on application startup.  Creates the quizzes, options, and
    votes tables if they do not exist, then seeds one default quiz if no
    quizzes are present.

    Default quiz seeded:
        Title: "General Knowledge"
        Question: "What is the capital of Indonesia?"
        Options:
            A: Jakarta   (correct)
            B: Surabaya
            C: Bandung
            D: Bali
    """
    if not wait_for_db():
        raise RuntimeError("Database unavailable after maximum retries")

    logger.info("Initializing database schema...")

    # Create tables
    execute_update("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            question TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    execute_update("""
        CREATE TABLE IF NOT EXISTS options (
            id SERIAL PRIMARY KEY,
            quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
            label VARCHAR(10) NOT NULL,
            text VARCHAR(255) NOT NULL
        )
    """)

    execute_update("""
        CREATE TABLE IF NOT EXISTS votes (
            id SERIAL PRIMARY KEY,
            quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
            option_id INTEGER REFERENCES options(id),
            voter_id VARCHAR(100),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Seed default quiz only if quizzes table is empty
    existing = execute_query("SELECT COUNT(*) AS count FROM quizzes")
    if existing and existing[0]["count"] == 0:
        logger.info("Seeding default quiz...")
        quiz_id = execute_insert(
            "INSERT INTO quizzes (title, question) VALUES (%s, %s) RETURNING id",
            ("General Knowledge", "What is the capital of Indonesia?"),
        )
        options_data = [
            ("A", "Jakarta"),
            ("B", "Surabaya"),
            ("C", "Bandung"),
            ("D", "Bali"),
        ]
        for label, text in options_data:
            execute_insert(
                "INSERT INTO options (quiz_id, label, text) VALUES (%s, %s, %s) RETURNING id",
                (quiz_id, label, text),
            )
        logger.info("Default quiz seeded with id=%s", quiz_id)
    else:
        logger.info("Quizzes table already has data, skipping seed.")