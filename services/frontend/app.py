import os
import random
import time
from functools import wraps

import requests
from flask import Flask, jsonify, render_template, redirect, request, url_for
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ---------------------------------------------------------------------------
# OpenTelemetry instrumentation
# ---------------------------------------------------------------------------
try:
    from opentelemetry import trace
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    _otel_enabled = True
except ImportError:
    _otel_enabled = False

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)

VOTE_API_URL = os.environ.get("VOTE_API_URL", "http://vote-api:8000")
RESULT_API_URL = os.environ.get("RESULT_API_URL", "http://result-api:8001")
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "frontend")
OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
ARTIFICIAL_DELAY_MS = int(os.environ.get("ARTIFICIAL_DELAY_MS", "0"))
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.0"))

# ---------------------------------------------------------------------------
# OpenTelemetry initialisation
# ---------------------------------------------------------------------------
if _otel_enabled and OTEL_EXPORTER_OTLP_ENDPOINT:
    resource = Resource.create({SERVICE_NAME: OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    FlaskInstrumentor().instrument_app(app)
    RequestsInstrumentor().instrument()
    tracer = trace.get_tracer(__name__)
else:
    tracer = None

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
METRIC_REQUESTS = Counter(
    "frontend_requests_total",
    "Total requests by endpoint and status",
    ["endpoint", "status"],
)
METRIC_DURATION = Histogram(
    "frontend_request_duration_seconds",
    "Request duration in seconds",
    ["endpoint"],
)
METRIC_ERRORS = Counter(
    "frontend_errors_total",
    "Total errors by endpoint and error type",
    ["endpoint", "error_type"],
)

# ---------------------------------------------------------------------------
# Middleware: artificial delay and random errors
# ---------------------------------------------------------------------------
def _should_skip_middleware(path: str) -> bool:
    return path in ("/health", "/metrics")


def _inject_faults():
    """Add artificial delay and/or return True when a simulated error should be raised."""
    if ARTIFICIAL_DELAY_MS > 0:
        delay = random.uniform(0, ARTIFICIAL_DELAY_MS) / 1000.0
        time.sleep(delay)
    if ERROR_RATE > 0 and random.random() < ERROR_RATE:
        return True
    return False


@app.before_request
def before_request():
    if _should_skip_middleware(request.path):
        return
    request._start_time = time.time()
    if _inject_faults():
        METRIC_ERRORS.labels(endpoint=request.path, error_type="simulated").inc()
        return jsonify({"error": "Simulated internal server error"}), 500


@app.after_request
def after_request(response):
    if hasattr(request, "_start_time") and request._start_time is not None:
        duration = time.time() - request._start_time
        METRIC_DURATION.labels(endpoint=request.path).observe(duration)
    METRIC_REQUESTS.labels(endpoint=request.path, status=response.status_code).inc()
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fetch_json(url: str, timeout: int = 5):
    """GET a URL and return the parsed JSON, or None on failure."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        METRIC_ERRORS.labels(endpoint=request.path, error_type="timeout").inc()
        return None
    except requests.exceptions.ConnectionError:
        METRIC_ERRORS.labels(endpoint=request.path, error_type="connection_error").inc()
        return None
    except requests.exceptions.RequestException:
        METRIC_ERRORS.labels(endpoint=request.path, error_type="request_error").inc()
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "frontend"})


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/")
def index():
    quizzes = _fetch_json(f"{VOTE_API_URL}/api/quizzes")
    if quizzes is None:
        return render_template("error.html", message="Service Unavailable"), 503
    return render_template("index.html", quizzes=quizzes)


@app.route("/quiz/<int:quiz_id>")
def quiz(quiz_id: int):
    data = _fetch_json(f"{VOTE_API_URL}/api/quizzes/{quiz_id}")
    if data is None:
        return render_template("error.html", message="Service Unavailable"), 503
    return render_template("quiz.html", quiz=data)


@app.route("/quiz/<int:quiz_id>/vote", methods=["POST"])
def submit_vote(quiz_id: int):
    option_id = request.form.get("option_id", type=int)
    if option_id is None:
        return render_template("error.html", message="No option selected"), 400
    try:
        resp = requests.post(
            f"{VOTE_API_URL}/api/quizzes/{quiz_id}/vote",
            json={"option_id": option_id},
            timeout=5,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        METRIC_ERRORS.labels(endpoint=request.path, error_type="timeout").inc()
        return render_template("error.html", message="Service Unavailable"), 503
    except requests.exceptions.ConnectionError:
        METRIC_ERRORS.labels(endpoint=request.path, error_type="connection_error").inc()
        return render_template("error.html", message="Service Unavailable"), 503
    except requests.exceptions.RequestException:
        METRIC_ERRORS.labels(endpoint=request.path, error_type="request_error").inc()
        # We still redirect on non-2xx so the user sees the current state
    return redirect(url_for("results", quiz_id=quiz_id))


@app.route("/quiz/<int:quiz_id>/results")
def results(quiz_id: int):
    result_data = _fetch_json(f"{RESULT_API_URL}/api/quizzes/{quiz_id}/results")
    quiz_data = _fetch_json(f"{VOTE_API_URL}/api/quizzes/{quiz_id}")
    if result_data is None and quiz_data is None:
        return render_template("error.html", message="Service Unavailable"), 503
    # Build a combined view: quiz info (title, question) plus results
    if quiz_data:
        quiz = {"id": quiz_data["id"], "title": quiz_data["title"], "question": quiz_data["question"]}
    else:
        quiz = {"id": quiz_id, "title": "Unknown Quiz", "question": ""}
    return render_template("results.html", quiz=quiz, results=result_data)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)