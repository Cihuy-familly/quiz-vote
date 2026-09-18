"""
Vote API — FastAPI application for submitting quiz votes.

Endpoints:
    GET  /health          — Health check (excluded from delay/error middleware)
    GET  /api/quizzes     — List all quizzes with their options
    GET  /api/quizzes/{id} — Get a single quiz with options
    POST /api/quizzes     — Create a new seeded default quiz
    POST /api/quizzes/{id}/vote — Submit a vote for an option
    GET  /metrics         — Prometheus metrics

Environment variables:
    DATABASE_URL            — Postgres connection string (default: postgresql://...)
    REDIS_URL               — Redis connection string (default: redis://redis:6379/0)
    OTEL_SERVICE_NAME       — OpenTelemetry service name (default: "vote-api")
    OTEL_EXPORTER_OTLP_ENDPOINT — OTLP exporter endpoint (optional)
    ARTIFICIAL_DELAY_MS     — Random delay in ms for non-health endpoints (default: "0")
    ERROR_RATE              — Percentage of requests to fail (default: "0.0")
"""

import os
import random
import time
import logging
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, HTTPException

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from database import init_db, execute_query, execute_insert
from models import Quiz, Option, VoteRequest, VoteResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://quizvote:quizvote@postgres:5432/quizvote",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "vote-api")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
ARTIFICIAL_DELAY_MS = int(os.getenv("ARTIFICIAL_DELAY_MS", "0"))
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.0"))

# ---------------------------------------------------------------------------
# Redis client (lazy)
# ---------------------------------------------------------------------------
redis_client = None


def get_redis():
    """Return the shared Redis client, creating it on first call."""
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
METRIC_HTTP_REQUESTS = Counter(
    "vote_api_http_requests_total",
    "Total HTTP requests by endpoint and status",
    ["endpoint", "method", "status"],
)
METRIC_HTTP_DURATION = Histogram(
    "vote_api_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
METRIC_ERRORS = Counter(
    "vote_api_errors_total",
    "Total errors (artificial or real)",
    ["endpoint", "error_type"],
)
METRIC_VOTES = Counter(
    "vote_api_votes_total",
    "Total votes submitted",
    ["quiz_id", "option_id"],
)

# ---------------------------------------------------------------------------
# OpenTelemetry (optional)
# ---------------------------------------------------------------------------
if OTEL_EXPORTER_OTLP_ENDPOINT:
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Set up the tracer provider
        trace.set_tracer_provider(TracerProvider())
        span_processor = BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces")
        )
        trace.get_tracer_provider().add_span_processor(span_processor)

        # Instrument libraries
        FastAPIInstrumentor().instrument()
        RequestsInstrumentor().instrument()

        logger.info("OpenTelemetry enabled, exporting to %s", OTEL_EXPORTER_OTLP_ENDPOINT)
    except Exception as e:
        logger.warning("Failed to initialize OpenTelemetry: %s", e)
else:
    logger.info("OpenTelemetry disabled (OTEL_EXPORTER_OTLP_ENDPOINT not set)")


# ---------------------------------------------------------------------------
# Artificial delay / error middleware
# ---------------------------------------------------------------------------
class ArtificialDelayErrorMiddleware(BaseHTTPMiddleware):
    """
    Middleware that injects artificial latency and/or random errors.

    - If ARTIFICIAL_DELAY_MS > 0: randomly sleeps 0..ARTIFICIAL_DELAY_MS ms.
    - If ERROR_RATE > 0: randomly returns HTTP 500 with the given probability.

    The /health endpoint is always excluded from both behaviours so that
    load balancers and orchestrators can always reach it.
    """

    async def dispatch(self, request, call_next):
        # Skip health checks
        if request.url.path == "/health":
            return await call_next(request)

        # Artificial delay
        if ARTIFICIAL_DELAY_MS > 0:
            delay = random.uniform(0, ARTIFICIAL_DELAY_MS) / 1000.0
            time.sleep(delay)

        # Artificial error
        if ERROR_RATE > 0 and random.random() < ERROR_RATE:
            METRIC_ERRORS.labels(
                endpoint=request.url.path, error_type="artificial"
            ).inc()
            METRIC_HTTP_REQUESTS.labels(
                endpoint=request.url.path,
                method=request.method,
                status=500,
            ).inc()
            from starlette.responses import JSONResponse
            return JSONResponse(
                status_code=500,
                content={"detail": "Artificial server error injected by ERROR_RATE"},
            )

        # Normal request - record metrics
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time
        
        # Increment request counter
        METRIC_HTTP_REQUESTS.labels(
            endpoint=request.url.path,
            method=request.method,
            status=response.status_code,
        ).inc()
        
        # Record duration
        METRIC_HTTP_DURATION.labels(
            endpoint=request.url.path,
            method=request.method,
        ).observe(duration)

        return response


# ---------------------------------------------------------------------------
# Application lifespan — runs init_db() on startup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init database on startup, clean up on shutdown."""
    logger.info("Starting vote-api...")
    try:
        init_db()
        logger.info("Database initialization complete.")
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        raise
    yield
    logger.info("Shutting down vote-api...")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Vote API",
    description="Quiz voting service — submit votes for quiz options",
    version="1.0.0",
    lifespan=lifespan,
)

# Register middleware (order matters: outermost first)
app.add_middleware(ArtificialDelayErrorMiddleware)

# Instrument FastAPI with OpenTelemetry if configured
if OTEL_EXPORTER_OTLP_ENDPOINT:
    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:
        logger.warning("Failed to instrument FastAPI with OpenTelemetry: %s", e)


# ---------------------------------------------------------------------------
# Helper: fetch a quiz with its options as a Quiz model
# ---------------------------------------------------------------------------
def _get_quiz_with_options(quiz_id: int) -> Quiz | None:
    """Return a Quiz with nested options, or None if not found."""
    rows = execute_query("SELECT * FROM quizzes WHERE id = %s", (quiz_id,))
    if not rows:
        return None
    q = rows[0]
    opt_rows = execute_query(
        "SELECT * FROM options WHERE quiz_id = %s ORDER BY id",
        (quiz_id,),
    )
    options = [Option(**o) for o in opt_rows]
    return Quiz(
        id=q["id"],
        title=q["title"],
        question=q["question"],
        options=options,
        created_at=q.get("created_at"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check endpoint (excluded from artificial delay/error)."""
    return {"status": "ok", "service": "vote-api"}


@app.get("/api/quizzes")
async def list_quizzes():
    """Return all quizzes with their options."""
    rows = execute_query("SELECT * FROM quizzes ORDER BY id")
    quizzes = []
    for q in rows:
        opt_rows = execute_query(
            "SELECT * FROM options WHERE quiz_id = %s ORDER BY id",
            (q["id"],),
        )
        quizzes.append(
            Quiz(
                id=q["id"],
                title=q["title"],
                question=q["question"],
                options=[Option(**o) for o in opt_rows],
                created_at=q.get("created_at"),
            )
        )
    return quizzes


@app.get("/api/quizzes/{quiz_id}")
async def get_quiz(quiz_id: int):
    """Return a specific quiz with its options."""
    quiz = _get_quiz_with_options(quiz_id)
    if quiz is None:
        raise HTTPException(status_code=404, detail="Quiz not found")
    return quiz


@app.post("/api/quizzes")
async def create_quiz():
    """
    Create and return a default seeded quiz.

    Seeds a "General Knowledge" quiz with 4 options.
    This endpoint always creates a new quiz regardless of existing data.
    """
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
    return _get_quiz_with_options(quiz_id)


@app.post("/api/quizzes/{quiz_id}/vote")
async def submit_vote(quiz_id: int, vote: VoteRequest):
    """
    Submit a vote for a specific option in a quiz.

    The request body must contain `option_id` — the id of the option to vote for.
    Validates that both the quiz and the option exist.
    """
    # Verify quiz exists
    quiz = execute_query("SELECT id FROM quizzes WHERE id = %s", (quiz_id,))
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")

    # Verify option exists and belongs to this quiz
    opt = execute_query(
        "SELECT id FROM options WHERE id = %s AND quiz_id = %s",
        (vote.option_id, quiz_id),
    )
    if not opt:
        raise HTTPException(status_code=404, detail="Option not found for this quiz")

    # Record the vote
    vote_id = execute_insert(
        "INSERT INTO votes (quiz_id, option_id) VALUES (%s, %s) RETURNING id",
        (quiz_id, vote.option_id),
    )

    # Record metric
    METRIC_VOTES.labels(quiz_id=str(quiz_id), option_id=str(vote.option_id)).inc()

    return VoteResponse(status="voted", vote_id=vote_id)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)