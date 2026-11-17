"""
Brenducate SDK — Non-Blocking Background Queue, Policy Sync, & Serverless Flush

Guarantees zero network latency on LLM calls by queuing traces in memory
and flushing via a background daemon thread + explicit flush() / atexit hooks
(required for AWS Lambda, Modal, and short-lived serverless functions).
"""

import atexit
import queue
import threading
import time
import weakref
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .pricing import resolve_step_down_model


class PolicyStore:
    """Thread-safe in-memory store for synced guardrails and optimistic per-tenant spend."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.policy: Dict[str, Any] = {}
        self.tenants: Dict[str, Dict[str, Any]] = {}
        self.local_spend_delta: Dict[str, float] = {}
        self.last_sync_at: float = 0.0

    def apply_sync_payload(self, data: Dict[str, Any]) -> None:
        with self._lock:
            if isinstance(data.get("policy"), dict):
                self.policy = dict(data["policy"])
            if isinstance(data.get("tenants"), dict):
                self.tenants = dict(data["tenants"])
                # Server state now reflects prior flushed spend; reset optimistic delta
                self.local_spend_delta.clear()
            self.last_sync_at = time.monotonic()

    def record_local_spend(self, tenant_id: str, cost_usd: float) -> None:
        if not tenant_id or cost_usd <= 0:
            return
        with self._lock:
            self.local_spend_delta[tenant_id] = (
                self.local_spend_delta.get(tenant_id, 0.0) + cost_usd
            )

    def check_tenant(
        self,
        tenant_id: str,
        requested_model: str,
        custom_fallback_map: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, str, Optional[str]]:
        """
        Sub-millisecond decision engine evaluated before an LLM request fires.
        Returns: (action: "allow" | "degrade" | "block", target_model, reason)
        """
        with self._lock:
            tenant_info = self.tenants.get(tenant_id, {})
            override = tenant_info.get("override_mode", "auto")
            spent_today = float(tenant_info.get("spent_today_usd", 0.0)) + float(
                self.local_spend_delta.get(tenant_id, 0.0)
            )
            policy = self.policy
            daily_cap = float(policy.get("tenant_daily_cap_usd", 0.0) or 0.0)
            daily_degrade = float(policy.get("tenant_daily_degrade_usd", 0.0) or 0.0)
            synced_map = policy.get("model_fallback_map") or {}

        if override == "blocked":
            return (
                "block",
                requested_model,
                f"Tenant '{tenant_id}' manually blocked via Brenducate dashboard.",
            )

        if daily_cap > 0 and spent_today >= daily_cap:
            return (
                "block",
                requested_model,
                f"Tenant '{tenant_id}' reached daily hard cap (${spent_today:.2f} / ${daily_cap:.2f}).",
            )

        merged_map = {**synced_map, **(custom_fallback_map or {})}
        should_degrade = (override == "force_degrade") or (
            daily_degrade > 0 and spent_today >= daily_degrade
        )
        if should_degrade:
            fallback = resolve_step_down_model(requested_model, merged_map)
            if fallback and fallback.lower() != requested_model.lower():
                reason = (
                    f"Tenant '{tenant_id}' auto-degraded ({requested_model} -> {fallback}; "
                    f"daily spend ${spent_today:.2f})."
                )
                return ("degrade", fallback, reason)

        return ("allow", requested_model, None)


def _atexit_flush(client_ref: "weakref.ref[BrenducateClient]") -> None:
    client = client_ref()
    if client is not None:
        try:
            client.flush(timeout=2.0)
            client.stop()
        except Exception:
            pass


class BrenducateClient:
    """
    Non-blocking HTTP telemetry client & background policy syncer.
    Fail-open by design: network errors never disrupt the caller's application.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        enable_background_sync: bool = True,
        sync_interval_s: float = 60.0,
        register_atexit: bool = True,
        max_queue_size: int = 2000,
    ) -> None:
        self.api_key = api_key
        cleaned_url = base_url.rstrip("/")
        if cleaned_url == "https://brenducate.com":
            cleaned_url = "https://www.brenducate.com"
        self.base_url = cleaned_url
        self.sync_interval_s = max(5.0, sync_interval_s)
        self.enable_background_sync = enable_background_sync
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "brenducate-sdk/0.2.0",
        }
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._flush_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self.policy_store = PolicyStore()

        self._http_client: Optional[httpx.Client] = None
        self._start_worker()

        if register_atexit:
            atexit.register(_atexit_flush, weakref.ref(self))

    def _get_client(self, timeout: float = 3.0) -> httpx.Client:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.Client(timeout=timeout, follow_redirects=True)
        return self._http_client

    def _start_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="brenducate-telemetry-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def send_trace(self, payload: Dict[str, Any]) -> None:
        """
        Enqueue trace for background delivery in <0.1ms.
        Drops oldest trace safely if queue reaches cap (fail-open).
        """
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            except Exception:
                pass

    def _drain_batch(self, max_items: int = 50) -> List[Dict[str, Any]]:
        batch: List[Dict[str, Any]] = []
        while len(batch) < max_items:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _post_batch_sync(self, batch: List[Dict[str, Any]], timeout: float = 3.0) -> None:
        if not batch:
            return
        try:
            client = self._get_client(timeout=timeout)
            if len(batch) == 1:
                client.post(
                    f"{self.base_url}/v1/telemetry/traces",
                    json=batch[0],
                    headers=self._headers,
                    timeout=timeout,
                )
            else:
                client.post(
                    f"{self.base_url}/v1/telemetry/traces/batch",
                    json={"traces": batch},
                    headers=self._headers,
                    timeout=timeout,
                )
        except Exception:
            pass  # Fail-open: never crash customer process on telemetry error

    async def _post_batch_async(self, batch: List[Dict[str, Any]], timeout: float = 3.0) -> None:
        if not batch:
            return
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                if len(batch) == 1:
                    await client.post(
                        f"{self.base_url}/v1/telemetry/traces",
                        json=batch[0],
                        headers=self._headers,
                    )
                else:
                    await client.post(
                        f"{self.base_url}/v1/telemetry/traces/batch",
                        json={"traces": batch},
                        headers=self._headers,
                    )
        except Exception:
            pass

    def sync_policies_now(self, timeout: float = 2.0) -> bool:
        """Fetch latest guardrail limits and per-tenant spend states synchronously."""
        try:
            client = self._get_client(timeout=timeout)
            res = client.get(
                f"{self.base_url}/v1/policies/sync",
                headers=self._headers,
                timeout=timeout,
            )
            if res.status_code == 200:
                self.policy_store.apply_sync_payload(res.json())
                return True
        except Exception:
            pass
        return False

    def _worker_loop(self) -> None:
        next_sync = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if self.enable_background_sync and now >= next_sync:
                self.sync_policies_now(timeout=2.0)
                next_sync = now + self.sync_interval_s

            batch = self._drain_batch(max_items=50)
            if batch:
                with self._flush_lock:
                    self._post_batch_sync(batch, timeout=3.0)
                continue

            # Sleep briefly without spinning CPU
            self._stop_event.wait(timeout=0.25)

    def flush(self, timeout: float = 2.0) -> int:
        """
        Synchronously flush all queued traces. Call at the end of AWS Lambda /
        Modal invocations to ensure zero telemetry loss before container freeze.
        """
        flushed = 0
        deadline = time.monotonic() + max(0.1, timeout)
        with self._flush_lock:
            while time.monotonic() < deadline:
                batch = self._drain_batch(max_items=50)
                if not batch:
                    break
                remaining = max(0.2, deadline - time.monotonic())
                self._post_batch_sync(batch, timeout=remaining)
                flushed += len(batch)
        return flushed

    async def aflush(self, timeout: float = 2.0) -> int:
        """Async version of flush() for async serverless handlers (e.g. Modal/FastAPI)."""
        flushed = 0
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            batch = self._drain_batch(max_items=50)
            if not batch:
                break
            remaining = max(0.2, deadline - time.monotonic())
            await self._post_batch_async(batch, timeout=remaining)
            flushed += len(batch)
        return flushed

    def stop(self) -> None:
        self._stop_event.set()
        if self._http_client is not None:
            try:
                self._http_client.close()
            except Exception:
                pass
