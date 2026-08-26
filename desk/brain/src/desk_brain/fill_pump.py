"""Fill auto-pull: Tradovate fill events -> Supabase fills -> trade rebuild.

Idempotent end to end: fills upsert on (account, order_id, exec_id) with
duplicates ignored — snapshot replays after a reconnect are no-ops — and the
rebuild endpoint regenerates trades deterministically. The CSV importer
remains the backfill path for history predating API access.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from supabase import AsyncClient

from .config import Settings
from .contracts_meta import product_root
from .tradovate import UserSync

log = logging.getLogger(__name__)

REBUILD_DEBOUNCE_S = 2.0


class FillPump:
    def __init__(self, s: Settings, sync: UserSync, db: AsyncClient, http: aiohttp.ClientSession, web_url: str):
        self._s = s
        self._sync = sync
        self._db = db
        self._http = http
        self._web_url = web_url.rstrip("/")
        self._dirty: set[tuple[str, str]] = set()  # (account, contract) awaiting rebuild
        self._rebuild_task: asyncio.Task | None = None
        sync.on_fill = self.handle_fill
        sync.on_fill_fee = self.handle_fill_fee

    async def handle_fill(self, fill: dict[str, Any]) -> None:
        try:
            order = await self._sync.order(fill["orderId"])
            account = await self._sync.account_name(order["accountId"])
            contract = (await self._sync.contract(fill["contractId"])).get("name", "").upper()
            row = {
                "account": account,
                "order_id": str(fill["orderId"]),
                "exec_id": str(fill["id"]),
                "contract": contract,
                "product": product_root(contract),
                "side": "buy" if fill.get("action") == "Buy" else "sell",
                "qty": int(fill["qty"]),
                "price": float(fill["price"]),
                "fees": 0,
                "filled_at": fill["timestamp"],
                "raw_json": {"source": "tradovate_api", **{k: v for k, v in fill.items()}},
            }
            await (
                self._db.table("fills")
                .upsert(row, on_conflict="account,order_id,exec_id", ignore_duplicates=True)
                .execute()
            )
            self._mark_dirty(account, contract)
        except Exception:  # noqa: BLE001 — one bad fill must not kill the pump
            log.exception("fill pump: failed on fill %s", fill.get("id"))

    async def handle_fill_fee(self, fee: dict[str, Any]) -> None:
        """fillFee arrives separately (id == fill id); fold it into the row."""
        try:
            total = sum(
                float(fee.get(k) or 0)
                for k in ("commission", "clearingFee", "exchangeFee", "nfaFee", "brokerageFee", "ipFee", "orderRoutingFee")
            )
            if total == 0:
                return
            res = (
                await self._db.table("fills")
                .update({"fees": total})
                .eq("exec_id", str(fee["id"]))
                .execute()
            )
            for row in res.data or []:
                self._mark_dirty(row["account"], row["contract"])
        except Exception:  # noqa: BLE001
            log.exception("fill pump: failed on fillFee %s", fee.get("id"))

    def _mark_dirty(self, account: str, contract: str) -> None:
        self._dirty.add((account, contract))
        if self._rebuild_task is None or self._rebuild_task.done():
            self._rebuild_task = asyncio.create_task(self._rebuild_soon())

    async def _rebuild_soon(self) -> None:
        await asyncio.sleep(REBUILD_DEBOUNCE_S)
        groups, self._dirty = self._dirty, set()
        if not groups:
            return
        try:
            async with self._http.post(
                f"{self._web_url}/api/rebuild",
                json={"groups": [{"account": a, "contract": c} for a, c in groups]},
                headers={"x-brain-secret": self._s.brain_shared_secret},
            ) as resp:
                if resp.status != 200:
                    log.warning("rebuild endpoint returned %s: %s", resp.status, await resp.text())
                else:
                    log.info("rebuilt trades for %s", sorted(groups))
        except aiohttp.ClientError as e:
            # Not fatal: fills are stored; the next import/rebuild catches up.
            log.warning("rebuild call failed (%s); fills are stored, trades lag", e)
            self._dirty |= groups
