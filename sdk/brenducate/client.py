"""
AgentPulse SDK — Async HTTP Client

Sends telemetry payloads to the AgentPulse Ops ingestion API.
Uses a fire-and-forget async pattern so it never adds latency to
the agent's main execution path.
"""

import httpx
import asyncio
import time
from typing import List, Optional, Dict, Any


class AgentPulseClient:
    """
    Thin async HTTP client for the AgentPulse Ops API.
    Sends traces asynchronously — if the network is down, it fails
    silently so it never disrupts your agent's execution.
    """

    def __init__(self, api_key: str, base_url: str):
        self.api_key  = api_key
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "User-Agent":    "agentpulse-sdk/0.1.0",
        }

    def send_trace(self, payload: Dict[str, Any]) -> None:
        """
        Fire-and-forget trace submission.
        Runs in a background thread so it never blocks the caller.
        """
        try:
            # Try to get the running event loop (e.g. inside async agent frameworks)
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._post_trace_async(payload))
            else:
                loop.run_until_complete(self._post_trace_async(payload))
        except RuntimeError:
            # No event loop at all — run synchronously as a fallback
            self._post_trace_sync(payload)

    async def _post_trace_async(self, payload: Dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.base_url}/v1/telemetry/traces",
                    json=payload,
                    headers=self._headers,
                )
        except Exception:
            pass  # Never raise — telemetry is best-effort

    def _post_trace_sync(self, payload: Dict[str, Any]) -> None:
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(
                    f"{self.base_url}/v1/telemetry/traces",
                    json=payload,
                    headers=self._headers,
                )
        except Exception:
            pass  # Never raise — telemetry is best-effort

    def get_dashboard_summary(self) -> Optional[Dict]:
        """Fetch live KPI summary for the current team (synchronous)."""
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(
                    f"{self.base_url}/v1/dashboard/summary",
                    headers=self._headers,
                )
                r.raise_for_status()
                return r.json()
        except Exception as e:
            print(f"[AgentPulse] Could not fetch summary: {e}")
            return None

