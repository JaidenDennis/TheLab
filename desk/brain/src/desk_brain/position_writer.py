"""Writes desk:position to Redis from the user-sync entity caches.

Refreshes on every entity change and on a 1s tick (spec §7) so unrealized
P&L tracks the live last price from desk:market_state.
"""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis

from . import redis_keys as rk
from .contracts_meta import point_value, product_root
from .tradovate import UserSync

log = logging.getLogger(__name__)


class PositionWriter:
    def __init__(self, sync: UserSync, redis: Redis):
        self._sync = sync
        self._redis = redis
        sync.on_change = self.write

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.write()
            except Exception:  # noqa: BLE001
                log.exception("position writer tick failed")

    async def write(self) -> None:
        st = self._sync.state
        market = await rk.read_json(self._redis, rk.MARKET_STATE)
        last = market.get("last") if market else None

        positions = []
        for p in st.positions.values():
            net = p.get("netPos", 0)
            if not net:
                continue
            contract = st.contracts.get(p.get("contractId", 0), {})
            name = (contract.get("name") or "?").upper()
            pv = point_value(product_root(name)) or 1
            avg = p.get("netPrice")
            upl = None
            if last is not None and avg is not None:
                upl = (float(last) - float(avg)) * net * pv
            positions.append(
                {
                    "account_id": p.get("accountId"),
                    "contract": name,
                    "side": "long" if net > 0 else "short",
                    "size": abs(net),
                    "avg_price": avg,
                    "unrealized": upl,
                    "entered_at": p.get("timestamp"),  # Tradovate last-change ts; best effort
                }
            )

        orders = [
            {
                "id": o.get("id"),
                "contract": (st.contracts.get(o.get("contractId", 0), {}).get("name") or "?").upper(),
                "action": o.get("action"),
                "type": o.get("orderType"),
                "qty": o.get("orderQty") or o.get("qty"),
                "price": o.get("price") or o.get("stopPrice"),
                "status": o.get("ordStatus"),
            }
            for o in st.working_orders()
        ]

        await rk.write_json(
            self._redis,
            rk.POSITION,
            {
                "connected": self._sync.connected.is_set(),
                "positions": positions,
                "working_orders": orders,
                "auto_liq": next(iter(st.auto_liq.values()), None),
                "cash": {str(k): {"amount": v.get("amount")} for k, v in st.cash.items()},
            },
        )
