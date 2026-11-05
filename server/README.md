# AgentPulse Ops — Backend API & Telemetry Ingestion Server

FastAPI-powered high-throughput edge telemetry ingestion, trace database, and dashboard API.

## ⚡ Running Locally

### 1. Install Dependencies
```bash
cd server
pip install -r requirements.txt
```

### 2. Start the Server
```bash
uvicorn main:app --port 8000 --reload
```

The database (`agentpulse.db`) is automatically initialized using SQLite on first run with a seeded demo API key (`ap_live_demo_key_00001`).

## 📡 API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Service health status. |
| `POST` | `/v1/auth/keys` | Provision a new API key for a team. |
| `POST` | `/v1/telemetry/traces` | High-throughput trace ingestion from the SDK. |
| `GET` | `/v1/telemetry/traces` | List recent traces for the authenticated team. |
| `GET` | `/v1/telemetry/traces/{id}` | Detailed trace inspection with full step-by-step graph. |
| `GET` | `/v1/dashboard/summary` | Aggregated 24h KPIs (active agents, tokens, spend, anomalies). |
| `GET` | `/v1/alerts` | List active circuit-breaker alerts. |
| `PATCH`| `/v1/alerts/{id}/resolve` | Mark an alert as resolved. |

## 🚀 Production Deployment (Postgres / Render / Railway)

To switch from SQLite to PostgreSQL in production:
1. Set environment variable: `DATABASE_URL=postgresql://user:password@host:5432/agentpulse`
2. Run with production workers:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
   ```
