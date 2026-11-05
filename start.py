#!/usr/bin/env python3
"""
AgentPulse Ops — Master Unified Server Launcher

Starts both:
  1. FastAPI Ingestion & Telemetry Backend (Port 8000)
  2. Production Dashboard UI Web Server (Port 3032)

Usage:
  python3 start.py
"""

import sys
import os
import subprocess
import time
import signal
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(BASE_DIR, "server")

processes = []

def cleanup(sig=None, frame=None):
    print("\n🛑 Shutting down AgentPulse Ops servers...")
    for p in processes:
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    print("👋 All services stopped.")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

def main():
    print("=" * 72)
    print("  🚀 Starting AgentPulse Ops — Autonomous Agent Observability Platform")
    print("=" * 72)

    # 1. Initialize DB
    sys.path.insert(0, SERVER_DIR)
    try:
        from database import init_db
        init_db()
        print("✅ SQLite Telemetry Database initialized.")
    except Exception as e:
        print(f"⚠️  Database setup warning: {e}")

    # 2. Start FastAPI Backend (Port 8000)
    print("Starting FastAPI Telemetry Backend on port 8000...")
    api_cmd = [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1",
        "--port", "8000",
        "--app-dir", SERVER_DIR
    ]
    p_api = subprocess.Popen(api_cmd, cwd=SERVER_DIR)
    processes.append(p_api)

    # 3. Start Frontend Dashboard Server (Port 3032)
    print("Starting Dashboard Web Server on port 3032...")
    dash_cmd = [
        sys.executable, "-m", "http.server", "3032",
        "--directory", BASE_DIR
    ]
    p_dash = subprocess.Popen(dash_cmd, cwd=BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    processes.append(p_dash)

    time.sleep(1.5)

    print("\n" + "=" * 72)
    print("  🌟 AgentPulse Ops is Live!")
    print("  ")
    print("  📊 Dashboard UI:        http://localhost:3032/index.html")
    print("  ⚡ Edge Telemetry API:  http://localhost:8000")
    print("  📚 API Docs (Swagger):  http://localhost:8000/docs")
    print("  🛡️ SDK Demo Test:       python3 examples/demo_agent_run.py")
    print("  💳 Stripe Billing:      http://localhost:8000/v1/billing/plans")
    print("  ")
    print("  Press Ctrl+C to stop all servers.")
    print("=" * 72 + "\n")

    try:
        # Keep alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
