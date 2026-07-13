"""TxLINE API client: REST snapshots + SSE streams with auto JWT renewal."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from httpx_sse import aconnect_sse

log = logging.getLogger("txline")

DEFAULT_BASE = "https://txline-dev.txodds.com"
WORLD_CUP_COMPETITION_ID = 72


@dataclass
class TxLineAuth:
    base_url: str
    api_token: str
    jwt: str = ""

    async def renew_jwt(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(f"{self.base_url}/auth/guest/start", json={})
        resp.raise_for_status()
        self.jwt = resp.json()["token"]
        log.info("guest JWT renewed")
        return self.jwt

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.jwt}",
            "X-Api-Token": self.api_token,
        }


@dataclass
class TxLineClient:
    auth: TxLineAuth
    client: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=30))

    @classmethod
    def from_env(cls) -> "TxLineClient":
        base = os.environ.get("TXLINE_API_BASE", DEFAULT_BASE).rstrip("/")
        auth = TxLineAuth(
            base_url=base,
            api_token=os.environ["TXLINE_API_TOKEN"],
            jwt=os.environ.get("TXLINE_JWT", ""),
        )
        return cls(auth=auth)

    @property
    def api(self) -> str:
        return f"{self.auth.base_url}/api"

    async def _get(self, path: str, params: dict | None = None) -> Any:
        if not self.auth.jwt:
            await self.auth.renew_jwt(self.client)
        for attempt in (1, 2):
            resp = await self.client.get(
                f"{self.api}{path}", params=params, headers=self.auth.headers()
            )
            if resp.status_code in (401, 403) and attempt == 1:
                await self.auth.renew_jwt(self.client)
                continue
            resp.raise_for_status()
            return resp.json()

    # --- REST snapshots -----------------------------------------------------

    async def fixtures(self, start_epoch_day: int,
                       competition_id: int = WORLD_CUP_COMPETITION_ID) -> list[dict]:
        return await self._get(
            "/fixtures/snapshot",
            {"competitionId": competition_id, "startEpochDay": start_epoch_day},
        )

    async def odds_snapshot(self, fixture_id: int) -> list[dict]:
        return await self._get(f"/odds/snapshot/{fixture_id}")

    async def scores_snapshot(self, fixture_id: int) -> Any:
        return await self._get(f"/scores/snapshot/{fixture_id}")

    # --- SSE streams ----------------------------------------------------------

    async def stream(self, kind: str) -> AsyncIterator[dict]:
        """Yield events from /odds/stream or /scores/stream forever, reconnecting."""
        assert kind in ("odds", "scores")
        backoff = 1.0
        while True:
            if not self.auth.jwt:
                await self.auth.renew_jwt(self.client)
            try:
                async with aconnect_sse(
                    self.client, "GET", f"{self.api}/{kind}/stream",
                    headers=self.auth.headers(),
                    timeout=httpx.Timeout(30, read=None),
                ) as source:
                    log.info("SSE %s stream connected", kind)
                    backoff = 1.0
                    async for ev in source.aiter_sse():
                        if not ev.data:
                            continue
                        try:
                            data = json.loads(ev.data)
                        except json.JSONDecodeError:
                            data = {"raw": ev.data}
                        yield {"kind": kind, "recv_ts": time.time(), "data": data}
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    await self.auth.renew_jwt(self.client)
                    continue
                log.warning("SSE %s HTTP error: %s", kind, exc)
            except (httpx.TransportError, asyncio.TimeoutError) as exc:
                log.warning("SSE %s dropped: %s", kind, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
