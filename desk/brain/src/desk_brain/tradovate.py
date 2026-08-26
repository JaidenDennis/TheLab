"""Read-only Tradovate integration (spec §3, §7, plus fill auto-pull).

One websocket (user/syncrequest) pushes account entities in real time:
positions, working orders, fills, fees. A REST client backs it for auth,
token renewal, and gap-filling lookups. The API key is created with order
permissions disabled, so no code path here — or anywhere — can trade.

Fills flow into the same Supabase table and the same reconstruction as the
CSV importer: this module only *upserts* fills idempotently and then asks
desk-web to rebuild trades, so reconstruction logic lives in exactly one
place (desk-web/src/lib/ingest).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp

from .config import Settings

log = logging.getLogger(__name__)


class TradovateREST:
    """Auth + GET helper with token renewal."""

    def __init__(self, s: Settings, http: aiohttp.ClientSession):
        self._s = s
        self._http = http
        self._token: str | None = None
        self._expires: datetime | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            now = datetime.now(timezone.utc)
            if self._token and self._expires and (self._expires - now).total_seconds() > 300:
                return self._token
            if self._token:
                renewed = await self._try_renew()
                if renewed:
                    return self._token
            await self._login()
            assert self._token is not None
            return self._token

    async def _login(self) -> None:
        s = self._s
        body = {
            "name": s.tradovate_username,
            "password": s.tradovate_password,
            "appId": s.tradovate_app_id,
            "appVersion": s.tradovate_app_version,
            "cid": s.tradovate_cid,
            "sec": s.tradovate_secret,
            "deviceId": s.tradovate_device_id,
        }
        async with self._http.post(f"{s.tradovate_base}/auth/accesstokenrequest", json=body) as resp:
            data = await resp.json()
        if "accessToken" not in data:
            # p-ticket = throttled; errorText = bad credentials
            raise RuntimeError(f"Tradovate auth failed: {data.get('errorText') or data}")
        self._token = data["accessToken"]
        self._expires = datetime.fromisoformat(data["expirationTime"].replace("Z", "+00:00"))
        self.user_id: int = data.get("userId", 0)
        log.info("tradovate: authenticated, token to %s", self._expires)

    async def _try_renew(self) -> bool:
        try:
            async with self._http.get(
                f"{self._s.tradovate_base}/auth/renewaccesstoken",
                headers={"Authorization": f"Bearer {self._token}"},
            ) as resp:
                data = await resp.json()
            if "accessToken" in data:
                self._token = data["accessToken"]
                self._expires = datetime.fromisoformat(data["expirationTime"].replace("Z", "+00:00"))
                return True
        except aiohttp.ClientError:
            pass
        return False

    async def get(self, path: str, **params: Any) -> Any:
        tok = await self.token()
        async with self._http.get(
            f"{self._s.tradovate_base}/{path}",
            params={k: str(v) for k, v in params.items()},
            headers={"Authorization": f"Bearer {tok}"},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


@dataclass
class AccountState:
    """Entity caches maintained from the user-sync stream."""

    accounts: dict[int, dict] = field(default_factory=dict)  # id -> account
    contracts: dict[int, dict] = field(default_factory=dict)  # id -> contract
    orders: dict[int, dict] = field(default_factory=dict)  # id -> order
    positions: dict[int, dict] = field(default_factory=dict)  # id -> position
    auto_liq: dict[int, dict] = field(default_factory=dict)  # accountId -> userAccountAutoLiq
    cash: dict[int, dict] = field(default_factory=dict)  # accountId -> cashBalance

    def working_orders(self) -> list[dict]:
        return [o for o in self.orders.values() if o.get("ordStatus") in ("Working", "Suspended")]


class UserSync:
    """Tradovate user-data websocket with reconnect + snapshot re-sync.

    on_fill(fill_entity) and on_fill_fee(fee_entity) fire for realtime AND
    snapshot fills — the Supabase upsert makes replays harmless, and the
    snapshot on reconnect is exactly the backfill for anything missed.
    """

    def __init__(self, s: Settings, rest: TradovateREST):
        self._s = s
        self._rest = rest
        self.state = AccountState()
        self.connected = asyncio.Event()
        self.on_fill: Callable[[dict], Any] | None = None
        self.on_fill_fee: Callable[[dict], Any] | None = None
        self.on_change: Callable[[], Any] | None = None  # any position/order change
        self._req_id = 0

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — any transport failure retries
                log.warning("tradovate ws dropped: %s — reconnecting in %.0fs", e, backoff)
            self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _run_once(self) -> None:
        token = await self._rest.token()
        async with aiohttp.ClientSession() as ws_http:
            async with ws_http.ws_connect(self._s.tradovate_ws, heartbeat=None) as ws:
                self._ws = ws
                opened = await ws.receive_str()
                if not opened.startswith("o"):
                    raise RuntimeError(f"unexpected open frame {opened!r}")
                await self._send("authorize", body=token)
                await self._send("user/syncrequest", body=json.dumps({"users": [self._rest.user_id]}))
                ping = asyncio.create_task(self._heartbeat(ws))
                try:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        await self._frame(msg.data)
                finally:
                    ping.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await ping
        raise RuntimeError("websocket closed")

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(2.5)
            await ws.send_str("[]")

    async def _send(self, endpoint: str, query: str = "", body: str = "") -> None:
        self._req_id += 1
        await self._ws.send_str(f"{endpoint}\n{self._req_id}\n{query}\n{body}")

    async def _frame(self, raw: str) -> None:
        kind, payload = raw[:1], raw[1:]
        if kind in ("h", "o"):
            return
        if kind == "c":
            raise RuntimeError(f"server closed: {payload}")
        if kind != "a":
            return
        for item in json.loads(payload):
            if not isinstance(item, dict):
                continue
            if item.get("e") == "props":
                await self._entity(item.get("d") or {})
            elif item.get("e") == "shutdown":
                raise RuntimeError(f"server shutdown: {item.get('d')}")
            elif "d" in item and isinstance(item["d"], dict) and "users" in item["d"]:
                await self._snapshot(item["d"])

    async def _snapshot(self, d: dict) -> None:
        st = self.state
        for acc in d.get("accounts", []):
            st.accounts[acc["id"]] = acc
        for c in d.get("contracts", []):
            st.contracts[c["id"]] = c
        for o in d.get("orders", []):
            st.orders[o["id"]] = o
        for p in d.get("positions", []):
            st.positions[p["id"]] = p
        for a in d.get("userAccountAutoLiqs", []):
            st.auto_liq[a.get("id", 0)] = a
        for cb in d.get("cashBalances", []):
            st.cash[cb.get("accountId", 0)] = cb
        self.connected.set()
        log.info(
            "tradovate: sync snapshot — %d account(s), %d order(s), %d fill(s)",
            len(d.get("accounts", [])),
            len(d.get("orders", [])),
            len(d.get("fills", [])),
        )
        if self.on_change:
            await _maybe_await(self.on_change())
        for f in d.get("fills", []):
            if self.on_fill:
                await _maybe_await(self.on_fill(f))
        for fee in d.get("fillFees", []):
            if self.on_fill_fee:
                await _maybe_await(self.on_fill_fee(fee))

    async def _entity(self, d: dict) -> None:
        etype, entity = d.get("entityType"), d.get("entity") or {}
        st = self.state
        if etype == "position":
            st.positions[entity["id"]] = entity
        elif etype == "order":
            st.orders[entity["id"]] = entity
        elif etype == "account":
            st.accounts[entity["id"]] = entity
        elif etype == "contract":
            st.contracts[entity["id"]] = entity
        elif etype == "userAccountAutoLiq":
            st.auto_liq[entity.get("id", 0)] = entity
        elif etype == "cashBalance":
            st.cash[entity.get("accountId", 0)] = entity
        elif etype == "fill":
            if self.on_fill:
                await _maybe_await(self.on_fill(entity))
            return
        elif etype == "fillFee":
            if self.on_fill_fee:
                await _maybe_await(self.on_fill_fee(entity))
            return
        else:
            return
        if self.on_change:
            await _maybe_await(self.on_change())

    # -- lookups with cache-miss fallback to REST -------------------------------

    async def contract(self, contract_id: int) -> dict:
        c = self.state.contracts.get(contract_id)
        if c is None:
            c = await self._rest.get("contract/item", id=contract_id)
            self.state.contracts[contract_id] = c
        return c

    async def order(self, order_id: int) -> dict:
        o = self.state.orders.get(order_id)
        if o is None:
            o = await self._rest.get("order/item", id=order_id)
            self.state.orders[order_id] = o
        return o

    async def account_name(self, account_id: int) -> str:
        a = self.state.accounts.get(account_id)
        if a is None:
            a = await self._rest.get("account/item", id=account_id)
            self.state.accounts[account_id] = a
        return a.get("name", str(account_id))


async def _maybe_await(v: Any) -> None:
    if asyncio.iscoroutine(v):
        await v
