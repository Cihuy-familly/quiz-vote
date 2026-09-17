# quiz-vote-app

A microservices-based voting/quiz application designed as a Kubernetes learning playground. Users can browse quizzes, vote on options, and see live results -- all running across independent services that can be deployed locally with Docker Compose or on Kubernetes with GitOps/FluxCD.

---

## Architecture

```
                         +-------------------+
                         |                   |
                         |    Frontend       |
                         |  (Python/Flask)   |
                         |    Port 5000      |
                         +---------+---------+
                                   |
              +--------------------+--------------------+
              |                                         |
              v                                         v
     +-------------------+                    +-------------------+
     |                   |                    |                   |
     |    Vote API       |                    |   Result API      |
     |  (Python/FastAPI) |                    |  (Python/FastAPI) |
     |    Port 8000      |                    |    Port 8001      |
     +---+-----------+---+                    +---+-----------^---+
         |           |                            |           |
         |           v                            |           |
         |   +-------------+                      |           |
         |   |   Redis     |    (pub/sub)          |           |
         |   |  (cache)    |<---------------------+           |
         |   +-------------+                      |           |
         |                                        |           |
         v                                        v           v
     +----------------------------------------------------------+
     |                                                          |
     |                   PostgreSQL (16-alpine)                  |
     |                    Port 5432                              |
     +----------------------------------------------------------+
                                ^
                                |
                     +----------+----------+
                     |                     |
            +--------v--------+   +--------v--------+
            |                  |   |                 |
            |  Load Generator  |   |   External      |
            |  (Python/CLI)    |   |  Clients / Curl |
            |  (profile:traffic|   |                 |
            +------------------+   +-----------------+
```

### Data Flow

1. **Frontend** serves the UI on port 5000. It reads quizzes and sends votes via the Vote API.
2. **Vote API** (port 8000) handles quiz listing and vote submission. Writes votes to PostgreSQL and publishes vote events to Redis pub/sub.
3. **Redis** acts as a message broker (pub/sub). Vote events flow from Vote API to Result API.
4. **Result API** (port 8001) subscribes to Redis vote events and serves aggregated results to the frontend.
5. **PostgreSQL** stores quizzes, options, and votes persistently.
6. **Load Generator** (opt-in) produces synthetic traffic at a configurable rate for testing.

---

## Service Descriptions

| Service | Language / Framework | Port | Purpose |
|---|---|---|---|
| `frontend` | Python / Flask | 5000 | Web UI for browsing quizzes and voting |
| `vote-api` | Python / FastAPI | 8000 | REST API for quizzes and vote submission |
| `result-api` | Python / FastAPI | 8001 | REST API for aggregated results |
| `redis` | Redis 7-alpine | 6379 | Pub/sub message broker for vote events |
| `postgres` | PostgreSQL 16-alpine | 5432 | Persistent storage for quizzes, options, votes |
| `load-generator` | Python / CLI | -- | Synthetic traffic generator (disabled by default) |

---

## Quick Start (Local)

### Prerequisites

- Docker Engine 24+
- Docker Compose v2+

### Start all services

```bash
cd quiz-vote-app
docker compose up --build -d
```

This starts all 5 core services (frontend, vote-api, result-api, redis, postgres). The load generator is excluded by default.

### Verify services

```bash
# List quizzes (empty initially)
curl http://localhost:8000/api/quizzes

# Create a quiz (via vote-api internal endpoint or seed script)
# Check frontend UI
open http://localhost:5000
```

### Stop everything

```bash
docker compose down -v
```

The `-v` flag removes the PostgreSQL volume (`pgdata`) so data does not persist between clean runs.

---

## Deploy to K8s

### GitOps / FluxCD

This project is designed to be deployed via GitOps with FluxCD. The deployment manifests live under `deploy/` in the repository root.

```yaml
# Example Flux Kustomization
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: quiz-vote-app
  namespace: flux-system
spec:
  interval: 5m
  path: ./deploy/overlays/dev
  prune: true
  sourceRef:
    kind: GitRepository
    name: quiz-vote-app
```

### Manual kubectl

```bash
kubectl apply -k deploy/overlays/dev
```

### Required Secrets

PostgreSQL credentials are expected as a Kubernetes secret named `quizvote-db-credentials`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: quizvote-db-credentials
type: Opaque
stringData:
  DATABASE_URL: postgresql://quizvote:quizvote@postgres:5432/quizvote
```

---

## Istio

When deployed on a Kubernetes cluster with Istio, the service mesh provides:

- **mTLS**: All inter-service communication is encrypted and authenticated.
- **Traffic management**: Weighted routing, retries, circuit breaking, and timeouts.
- **Observability**: Automatic metrics (Prometheus), distributed tracing (Jaeger/Zipkin), and access logs.
- **Resilience**: Istio retries and connection pools protect against transient failures.

### VirtualService Example

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: vote-api
spec:
  hosts:
    - vote-api
  http:
    - route:
        - destination:
            host: vote-api
      retries:
        attempts: 3
        perTryTimeout: 2s
      timeout: 10s
```

### PeerAuthentication for mTLS

```yaml
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: quiz-vote
spec:
  mtls:
    mode: STRICT
```

---

## Observability

### Metrics

Each service exposes Prometheus metrics at `/metrics`:

- `vote_api_votes_total{status="success|error"}`
- `result_api_votes_by_option{option_id, quiz_id}`
- `http_request_duration_seconds` (Histogram)
- `http_requests_total{method, path, status}`

### Structured Logging

All services emit JSON-formatted logs to stdout:

```json
{"time": "2026-09-17T12:00:00Z", "service": "vote-api", "level": "INFO", "message": {"text": "Vote recorded", "quiz_id": 1, "option_id": 3}}
```

### Distributed Tracing

When deployed with Istio, Zipkin-compatible traces are automatically generated. The APIs propagate trace context via standard `traceparent` / `x-request-id` headers.

---

## Failure Testing

The API services support environment variables to simulate failure modes for testing resilience:

| Env Var | Service | Effect |
|---|---|---|
| `ARTIFICIAL_DELAY_MS` | vote-api, result-api | Adds a fixed delay before every HTTP response (simulates slow backend) |
| `ERROR_RATE` | vote-api, result-api | Fraction of requests that return HTTP 500 (simulates intermittent failures) |
| `LATENCY_MAX` | load-generator | Max random sleep before each vote request (simulates slow clients) |

### Example: Slow + Flaky Vote API

```yaml
# docker-compose override
services:
  vote-api:
    environment:
      ARTIFICIAL_DELAY_MS: "2000"
      ERROR_RATE: "0.2"
```

With these settings, every request to vote-api takes at least 2 seconds, and 20% of requests return a 500 error. This is useful for testing:

- **Frontend** -- does it handle slow responses gracefully (loading spinners, timeouts)?
- **Istio retries** -- does the mesh retry the 500s and succeed on the next attempt?
- **Load generator** -- does it log and survive errors?
- **Monitoring** -- do alerts fire when error rates spike?

---

## Load Testing

The load generator produces synthetic traffic so you can stress-test the system without manually clicking the UI.

### Enable it

```bash
docker compose --profile traffic up -d
```

### Configuration

| Env Var | Default | Description |
|---|---|---|
| `VOTE_API_URL` | `http://vote-api:8000` | Target vote-api address |
| `VOTES_PER_SECOND` | `0.5` | Vote submission rate |
| `ERROR_RATE` | `0.0` | Fraction of votes to skip (simulated failures) |
| `LATENCY_MAX` | `0.0` | Max seconds of random delay before each vote |

### Scale it

```bash
docker compose --profile traffic up -d --scale load-generator=3
```

This runs 3 load-generator containers in parallel, each at 0.5 votes/second = 1.5 total votes/second.

---

## Directory Structure

```
quiz-vote-app/
├── README.md                       # This file
├── docker-compose.yaml             # Local development orchestration
├── Makefile                        # Common task shortcuts
├── .github/
│   └── workflows/
│       └── ci.yaml                 # CI pipeline (build, lint, test)
│
├── services/
│   ├── frontend/                   # Web UI service
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       └── ...
│   │
│   ├── vote-api/                   # Vote submission API
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py
│   │   └── ...
│   │
│   ├── result-api/                 # Result aggregation API
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py
│   │   └── ...
│   │
│   └── load-generator/             # Synthetic traffic generator
│       ├── Dockerfile
│       ├── requirements.txt
│       └── generate.py
│
└── deploy/                        # Kubernetes manifests (GitOps-ready)
    ├── base/
    │   ├── kustomization.yaml
    │   ├── namespace.yaml
    │   ├── frontend-deployment.yaml
    │   ├── vote-api-deployment.yaml
    │   ├── result-api-deployment.yaml
    │   ├── redis-deployment.yaml
    │   ├── postgres-statefulset.yaml
    │   └── services.yaml
    │
    └── overlays/
        ├── dev/
        │   └── kustomization.yaml
        └── staging/
            └── kustomization.yaml
```

---

## Versioning

This project follows **Semantic Versioning** (`vMAJOR.MINOR.PATCH`).

- **MAJOR** -- Breaking changes to the API contract or architecture.
- **MINOR** -- New features or services (backwards-compatible).
- **PATCH** -- Bug fixes, dependency updates, minor improvements.

### Tag convention

```
v1.0.0
v1.1.0
v1.1.1
```

Each release tag corresponds to a git tag pushed to the repository. FluxCD's ImageUpdateAutomation uses these tags to deploy the latest version automatically.