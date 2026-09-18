"""
Result API — FastAPI application for reading quiz results.

Endpoints:
    GET  /health                    — Health check
    GET  /api/quizzes/{id}/results  — Get results for a specific quiz
    GET  /metrics                   — Prometheus metrics

Environment variables:
    DATABASE_URL            — Postgres connection string (default: postgresql://...)
    OTEL_SERVICE_NAME       — OpenTelemetry service name (default: "result-api")
    OTEL_EXPORTER_OTLP_ENDPOINT — OTLP exporter endpoint (optional)
    ARTIFICIAL_DELAY_MS     — Random delay in ms (default: "0")
    ERROR_RATE              — Percentage of requests to fail (default: "0.0")
"""

import os
import random
import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

from database import execute_query
from models import OptionResult, QuizResults

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
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "result-api")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
ARTIFICIAL_DELAY_MS = int(os.getenv("ARTIFICIAL_DELAY_MS", "0"))
ERROR_RATE = float(os.getenv("ERROR_RATE", "0.0"))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
METRIC_HTTP_REQUESTS = Counter(
    "result_api_http_requests_total",
    "Total HTTP requests by endpoint and status",
    ["endpoint", "method", "status"],
)
METRIC_HTTP_DURATION = Histogram(
    "result_api_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
METRIC_ERRORS = Counter(
    "result_api_errors_total",
    "Total errors (artificial or real)",
    ["endpoint", "error_type"],
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

    The /health endpoint is always excluded.
    """

    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        if ARTIFICIAL_DELAY_MS > 0:
            delay = random.uniform(0, ARTIFICIAL_DELAY_MS) / 1000.0
            time.sleep(delay)

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
# Application lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan."""
    logger.info("Starting result-api...")
    yield
    logger.info("Shutting down result-api...")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Result API",
    description="Quiz results service — read vote tallies for quizzes",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(ArtificialDelayErrorMiddleware)

if OTEL_EXPORTER_OTLP_ENDPOINT:
    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception as e:
        logger.warning("Failed to instrument FastAPI with OpenTelemetry: %s", e)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "result-api"}


@app.get("/api/quizzes/{quiz_id}/results")
async def get_quiz_results(quiz_id: int):
    """
    Return results for a specific quiz.

    The response includes:
      - quiz_id, title, question
      - per-option breakdown: label, text, count, percentage
      - total_votes across all options
    """
    # Fetch the quiz
    rows = execute_query("SELECT * FROM quizzes WHERE id = %s", (quiz_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Quiz not found")
    quiz = rows[0]

    # Fetch all options for this quiz
    opt_rows = execute_query(
        "SELECT * FROM options WHERE quiz_id = %s ORDER BY id",
        (quiz_id,),
    )
    if not opt_rows:
        raise HTTPException(status_code=404, detail="No options found for this quiz")

    # Get total votes for each option
    option_results = []
    total_votes = 0

    for opt in opt_rows:
        vote_count = execute_query(
            "SELECT COUNT(*) AS count FROM votes WHERE option_id = %s",
            (opt["id"],),
        )
        count = vote_count[0]["count"] if vote_count else 0
        total_votes += count
        option_results.append({
            "option_id": opt["id"],
            "label": opt["label"],
            "text": opt["text"],
            "votes": count,
        })

    # Calculate percentages
    final_options = []
    for opt in option_results:
        percentage = (opt["votes"] / total_votes * 100) if total_votes > 0 else 0.0
        final_options.append(
            OptionResult(
                label=opt["label"],
                text=opt["text"],
                votes=opt["votes"],
                percentage=round(percentage, 1),
            )
        )

    return QuizResults(
        quiz_id=quiz["id"],
        title=quiz["title"],
        question=quiz["question"],
        options=final_options,
        total_votes=total_votes,
        created_at=quiz.get("created_at"),
    )


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)