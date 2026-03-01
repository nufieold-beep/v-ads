# LiteAds – Installation & Setup Guide

> **CPM CTV & In-App Video Ad Server** with OpenRTB 2.6 and VAST 2.0–4.2 support.

---

## URL Dashboard

All service URLs at a glance once the stack is running:

| Service              | URL                                        | Notes                          |
|----------------------|--------------------------------------------|--------------------------------|
| **Ad Server**        | http://localhost:8000                      | FastAPI application            |
| **Health Check**     | http://localhost:8000/health               | Server status                  |
| **Swagger UI**       | http://localhost:8000/docs                 | Interactive API docs           |
| **ReDoc**            | http://localhost:8000/redoc                | Alternative API docs           |
| **OpenRTB Bid**      | http://localhost:8000/api/v1/openrtb/bid   | `POST` – OpenRTB 2.6 endpoint |
| **VAST Tag**         | http://localhost:8000/api/vast             | `GET` – VAST XML endpoint      |
| **Ad Request**       | http://localhost:8000/api/v1/ad/request    | `POST` – Internal ad request   |
| **Event Tracking**   | http://localhost:8000/api/v1/event/*       | `POST` – VAST event callbacks  |
| **Nginx**            | http://localhost:80                        | Reverse proxy (production)     |
| **Prometheus**       | http://localhost:9090                      | Metrics (monitoring profile)   |
| **Grafana**          | http://localhost:3000                      | Dashboards – `admin` / `admin` |
| **PostgreSQL**       | localhost:5432                             | DB: `liteads` / User: `liteads`|
| **Redis**            | localhost:6379                             | Cache & frequency caps         |

> **Profiles:** Nginx requires `--profile production`. Prometheus & Grafana require `--profile monitoring`.

---

## Table of Contents

1. [URL Dashboard](#url-dashboard)
2. [Prerequisites](#prerequisites)
3. [Quick Start (Docker)](#quick-start-docker)
4. [Local Development Setup](#local-development-setup)
6. [Configuration](#configuration)
7. [Database Setup](#database-setup)
8. [Running the Server](#running-the-server)
9. [API Endpoints](#api-endpoints)
10. [Production Deployment](#production-deployment)
11. [Monitoring (Prometheus & Grafana)](#monitoring-prometheus--grafana)
12. [Testing](#testing)
13. [Makefile Reference](#makefile-reference)
14. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Dependency       | Version   | Required |
|------------------|-----------|----------|
| Python           | ≥ 3.10    | Yes      |
| PostgreSQL       | 15+       | Yes      |
| Redis            | 7+        | Yes      |
| Docker & Compose | Latest    | Optional |
| pip              | Latest    | Yes      |

> **Note:** If you use Docker Compose, PostgreSQL and Redis are included automatically.

---

## Quick Start (Docker)

The fastest way to get LiteAds running:

```bash
# 1. Clone the repository
git clone https://github.com/your-org/openadserver.git
cd openadserver

# 2. Start all core services (ad-server + PostgreSQL + Redis)
docker compose up -d

# 3. Verify the server is running
curl http://localhost:8000/health
```

This starts three services:

| Service    | Port  | Description                     |
|------------|-------|---------------------------------|
| ad-server  | 8000  | FastAPI ad server               |
| postgres   | 5432  | PostgreSQL 15 with seed data    |
| redis      | 6379  | Redis 7 (caching, freq caps)   |

The database is automatically initialized with schema tables and seed campaigns when the PostgreSQL container starts for the first time (via `scripts/init_db.sql`).

### Start with Nginx (Production Profile)

```bash
docker compose --profile production up -d
```

This adds an Nginx reverse proxy on port **80** in front of the ad server.

### Start with Full Monitoring

```bash
docker compose --profile production --profile monitoring up -d
```

This adds:

| Service     | Port | Description              |
|-------------|------|--------------------------|
| nginx       | 80   | Reverse proxy            |
| prometheus  | 9090 | Metrics collection       |
| grafana     | 3000 | Dashboards (admin/admin) |

---

## Local Development Setup

### 1. Install Python Dependencies

```bash
# Create and activate a virtual environment (recommended)
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# Install the package in editable mode with dev dependencies
pip install -e ".[dev]"
```

Or use Make:

```bash
make install
```

### 2. Start PostgreSQL and Redis

If you're developing locally without Docker for the app but still need the databases:

```bash
# Start only postgres and redis via Docker
docker compose up -d postgres redis
```

Or use Make:

```bash
make db-init
```

If running PostgreSQL natively, create the database and run the init script:

```bash
createdb liteads
psql -d liteads -f scripts/init_db.sql
```

### 3. Configure Environment

Set the environment to `dev`:

```bash
# Windows (PowerShell)
$env:LITEADS_ENV = "dev"

# macOS / Linux
export LITEADS_ENV=dev
```

The `dev` config (in `configs/dev.yaml`) enables debug mode, hot-reload, and console logging.

### 4. Run the Server

```bash
# Using Make
make dev

# Or directly
python -m liteads.ad_server.main

# Or using the CLI entry point
liteads-server
```

The server starts at **http://localhost:8000**.

---

## Configuration

LiteAds uses a layered YAML configuration system:

```
configs/
├── base.yaml       # Base defaults (always loaded)
├── dev.yaml        # Development overrides
└── prod.yaml       # Production overrides
```

The active config is selected by the `LITEADS_ENV` environment variable:

| `LITEADS_ENV` | Config files loaded            |
|---------------|--------------------------------|
| `dev`         | `base.yaml` + `dev.yaml`      |
| `prod`        | `base.yaml` + `prod.yaml`     |
| *(unset)*     | `base.yaml` only              |

### Environment Variable Overrides

All config values can be overridden via environment variables using the `LITEADS_` prefix with double-underscore nesting:

```bash
# Database
LITEADS_DATABASE__HOST=db.example.com
LITEADS_DATABASE__PORT=5432
LITEADS_DATABASE__NAME=liteads
LITEADS_DATABASE__USER=liteads
LITEADS_DATABASE__PASSWORD=secret

# Redis
LITEADS_REDIS__HOST=redis.example.com
LITEADS_REDIS__PORT=6379

# Server
LITEADS_SERVER__PORT=8000
LITEADS_SERVER__WORKERS=4
```

### Key Configuration Sections

| Section       | Description                                                    |
|---------------|----------------------------------------------------------------|
| `server`      | Host, port, workers, reload                                    |
| `database`    | PostgreSQL connection + pool settings                          |
| `redis`       | Redis connection + pool settings                               |
| `ad_serving`  | Default ads count, timeout, bid floor, ML model toggle         |
| `video`       | Supported MIME types, duration range, bitrate, VAST protocols  |
| `vast`        | VAST versions (2.0–4.2), tracking URL, skip offset, companion |
| `openrtb`     | Auction type, tmax, currency, bid floor, price macro           |
| `frequency`   | Daily/hourly frequency caps and TTL                            |
| `ml`          | Model directory, model names, embedding dim, batch size        |
| `logging`     | Log level and format (json / console)                          |
| `monitoring`  | Prometheus toggle and port                                     |

---

## Database Setup

### Schema

The database schema is defined in `scripts/init_db.sql` and includes:

| Table             | Purpose                                           |
|-------------------|---------------------------------------------------|
| `advertisers`     | Advertiser accounts with balance and credit        |
| `campaigns`       | CPM campaigns (CTV or InApp), budgets, bid amounts |
| `creatives`       | Video creatives with VAST/companion support        |
| `targeting_rules` | JSON-based targeting (device, geo, content, etc.)  |
| `ad_events`       | VAST event tracking (impression through error)     |
| `hourly_stats`    | Aggregated video metrics for VTR optimisation      |

### Seed Data

The init script includes seed data:

- **2 advertisers** (CTV Demo + InApp Video)
- **4 campaigns** (2 CTV at $8–$12 CPM, 2 InApp at $6–$7.50 CPM)
- **4 video creatives** (1920×1080 CTV + 1280×720 InApp)
- **Targeting rules** (environment, device OS, geo/DMA, content genre)

### Generate Additional Mock Data

```bash
make db-mock
# Runs: python scripts/generate_mock_data.py --advertisers 10 --campaigns 5 --creatives 3
```

### Database Migrations (Alembic)

```bash
make db-migrate
# Runs: alembic upgrade head
```

### Direct Database Access

```bash
make db-shell
# Opens psql connected to the liteads database
```

---

## Running the Server

### Development

```bash
make dev
# Starts with: LITEADS_ENV=dev, hot-reload enabled, debug logging
```

### Production (Docker)

```bash
# Build and start
docker compose up -d --build

# Scale horizontally (3 instances behind Nginx)
docker compose --profile production up -d --scale ad-server=3

# View logs
docker compose logs -f ad-server
```

### Production (Native)

```bash
export LITEADS_ENV=prod
python -m liteads.ad_server.main
# Starts with: 4 workers, JSON logging, ML prediction enabled
```

---

## API Endpoints

Once the server is running, the following endpoints are available:

### Health Check

```
GET /health
```

### Ad Request (Internal)

```
POST /api/v1/ad/request
Content-Type: application/json

{
  "placement_id": "ctv-preroll-1",
  "environment": "ctv",
  "num_ads": 1,
  "device": {
    "device_type": "ctv",
    "os": "roku",
    "make": "Roku",
    "ip": "76.83.21.242",
    "ua": "Roku/DVP-12.0"
  },
  "video_placement": {
    "width": 1920,
    "height": 1080,
    "min_duration": 5,
    "max_duration": 30,
    "placement_type": "pre_roll"
  }
}
```

### OpenRTB 2.6 Bid Request

```
POST /api/v1/openrtb/bid
Content-Type: application/json

{
  "id": "bid-req-001",
  "imp": [{
    "id": "1",
    "video": {
      "mimes": ["video/mp4"],
      "protocols": [2, 3, 6, 7],
      "w": 1920,
      "h": 1080,
      "minduration": 5,
      "maxduration": 30,
      "linearity": 1,
      "placement": 1
    },
    "bidfloor": 2.0,
    "bidfloorcur": "USD"
  }],
  "app": {
    "bundle": "com.roku.channel",
    "name": "Roku Channel",
    "cat": ["IAB1"]
  },
  "device": {
    "ua": "Roku/DVP-12.0 (12.0.0.4183)",
    "ip": "76.83.21.242",
    "devicetype": 3,
    "make": "Roku",
    "model": "Express 4K",
    "os": "Roku",
    "osv": "12.0",
    "ifa": "fa73d67d-9a53-123a-456b-789c01234de",
    "connectiontype": 2
  },
  "at": 2,
  "tmax": 200,
  "cur": ["USD"]
}
```

### VAST Tag (GET)

For VAST-compatible players (e.g., LG webOS, Samsung Tizen, SSAI):

```
GET /api/vast?sid=vast-session-001&w=1920&h=1080&min_dur=5&max_dur=30&ip=76.83.21.242&ua=Mozilla/5.0&device_make=LG&os=webos&app_bundle=com.lgwebos.app
```

Key parameters: `sid`, `imp`, `w`, `h`, `min_dur`, `max_dur`, `ip`, `ua`, `ifa`, `dnt`, `os`, `device_make`, `device_model`, `app_bundle`, `app_name`, `bid_floor`, `content_id`, `content_genre`, `content_rating`, `lat`, `lon`, `country`, `region`, `gdpr`, `gdpr_consent`, `us_privacy`, `coppa`.

### Event Tracking

```
POST /api/v1/event/impression
POST /api/v1/event/click
POST /api/v1/event/video     # start, firstQuartile, midpoint, thirdQuartile, complete, skip
POST /api/v1/event/error
```

### API Documentation (Auto-generated)

```
Swagger UI: http://localhost:8000/docs
ReDoc:      http://localhost:8000/redoc
```

---

## Production Deployment

### Docker Compose (Recommended)

```bash
# Full production stack with Nginx + monitoring
make prod-deploy
# Equivalent to: docker compose --profile production up -d --build

# Check status
make prod-status

# Scale ad-server instances
make docker-scale
# Equivalent to: docker compose up -d --scale ad-server=3
```

### Dockerfile Details

The Dockerfile uses a **multi-stage build** (Python 3.11-slim):

1. **Builder stage** – Installs build deps, creates wheel
2. **Runtime stage** – Installs only the wheel, runs as non-root `liteads` user
3. **Health check** – `curl http://localhost:8000/health` every 30s

### Nginx

The Nginx configuration (`deployment/nginx/nginx.conf`) provides:

- Reverse proxy to ad-server (port 8000)
- Load balancing when scaling to multiple instances
- Static file serving

### Environment Variables for Production

```bash
# Required
LITEADS_ENV=prod
LITEADS_DATABASE__HOST=your-db-host
LITEADS_DATABASE__PORT=5432
LITEADS_DATABASE__NAME=liteads
LITEADS_DATABASE__USER=liteads
LITEADS_DATABASE__PASSWORD=your-secure-password

LITEADS_REDIS__HOST=your-redis-host
LITEADS_REDIS__PORT=6379
```

---

## Monitoring (Prometheus & Grafana)

```bash
# Start monitoring stack
docker compose --profile monitoring up -d
```

| Service    | URL                        | Credentials   |
|------------|----------------------------|---------------|
| Prometheus | http://localhost:9090       | –             |
| Grafana    | http://localhost:3000       | admin / admin |

Pre-built Grafana dashboards are provisioned automatically from `deployment/grafana/dashboards/liteads.json`.

Prometheus scrape config is at `deployment/prometheus/prometheus.yml`.

### Key Metrics

The ad server exposes Prometheus metrics via middleware:

- Request latency histograms
- Request counts by endpoint and status
- Ad fill rate
- VAST response generation time
- Error rates

---

## Testing

```bash
# Run all tests with coverage
make test
# Runs: pytest tests/ -v --cov=liteads --cov-report=term-missing

# Run specific test suites
pytest tests/test_ad_server/ -v
pytest tests/test_ml_engine/ -v
pytest tests/test_rec_engine/ -v

# Lint and type check
make lint
# Runs: ruff check liteads/ tests/ && mypy liteads/

# Auto-format
make format
# Runs: ruff format liteads/ tests/ && ruff check --fix liteads/ tests/
```

### End-to-End Tests

```bash
# Full flow test (requires running server)
python scripts/test_full_flow.py

# LR model end-to-end test
python scripts/e2e_test_lr.py
```

### Load Testing

```bash
# Benchmark with wrk (requires wrk installed)
make benchmark

# Criteo data stress test with Locust
cd scripts/criteo
python locustfile.py
```

---

## Makefile Reference

| Command              | Description                                     |
|----------------------|-------------------------------------------------|
| `make install`       | Install package in editable mode with dev deps  |
| `make dev`           | Start dev server (debug, hot-reload)            |
| `make test`          | Run pytest with coverage                        |
| `make lint`          | Run ruff + mypy                                 |
| `make format`        | Auto-format with ruff                           |
| `make docker-up`     | Start core Docker services                      |
| `make docker-down`   | Stop all Docker services                        |
| `make docker-build`  | Build Docker images                             |
| `make docker-logs`   | Tail Docker logs                                |
| `make docker-scale`  | Scale to 3 ad-server instances                  |
| `make db-init`       | Initialize database (start postgres + run SQL)  |
| `make db-migrate`    | Run Alembic migrations                          |
| `make db-mock`       | Generate mock data                              |
| `make db-shell`      | Open psql shell                                 |
| `make redis-cli`     | Open Redis CLI                                  |
| `make redis-flush`   | Flush all Redis data                            |
| `make health`        | Check server health                             |
| `make api-docs`      | Show API documentation URLs                     |
| `make benchmark`     | Run wrk benchmark                               |
| `make prod-deploy`   | Deploy production stack                         |
| `make prod-status`   | Show production service status                  |
| `make clean`         | Remove build artifacts and caches               |

---

## Troubleshooting

### Server won't start

```bash
# Check if PostgreSQL and Redis are running
docker compose ps

# Check database connectivity
make db-shell

# Check Redis connectivity
make redis-cli
# > PING   (should return PONG)
```

### Port already in use

```bash
# Check what's using port 8000
# Windows
netstat -ano | findstr :8000

# Linux/macOS
lsof -i :8000
```

### Database not initialized

```bash
# Re-run init script
docker compose exec postgres psql -U liteads -d liteads -f /docker-entrypoint-initdb.d/init.sql

# Or from host
psql -h localhost -U liteads -d liteads -f scripts/init_db.sql
```

### Docker volumes stale

```bash
# Remove volumes and restart fresh
docker compose down -v
docker compose up -d
```

### Python import errors

```bash
# Ensure the package is installed in editable mode
pip install -e ".[dev]"

# Verify installation
python -c "import liteads; print('OK')"
```

---

## Project Structure

```
openadserver/
├── configs/                  # YAML configuration files
├── deployment/
│   ├── docker/              # Dockerfile
│   ├── grafana/             # Grafana dashboards & provisioning
│   ├── nginx/               # Nginx reverse proxy config
│   └── prometheus/          # Prometheus scrape config
├── liteads/
│   ├── ad_server/           # FastAPI application
│   │   ├── main.py          # App entry point & lifespan
│   │   ├── middleware/       # Prometheus metrics middleware
│   │   ├── routers/         # API route handlers
│   │   └── services/        # Business logic (ad, event, OpenRTB)
│   ├── common/              # Shared utilities (cache, config, DB, logging)
│   ├── ml_engine/           # ML pipeline (features, models, training, serving)
│   ├── models/              # SQLAlchemy ORM models
│   ├── rec_engine/          # Recommendation engine (retrieval, ranking, filters)
│   ├── schemas/             # Pydantic request/response schemas
│   └── trainer/             # Model training CLI
├── models/                   # Trained model checkpoints
├── scripts/                  # Utility scripts (DB init, mock data, benchmarks)
├── tests/                    # Test suites
├── docker-compose.yml
├── Makefile
└── pyproject.toml
```

---

## License

See [LICENSE](LICENSE) for details.
