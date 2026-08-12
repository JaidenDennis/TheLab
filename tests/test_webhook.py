"""Webhook executor, tested against a real local HTTP server.

Not a mocked session: the thing under test is how real aiohttp responses,
timeouts and transport failures map onto OrderOutcome, and a mock would only
prove the mapping agrees with itself.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from aiohttp import web

from nq_agent.execution.webhook import WebhookExecutor, classify
from nq_agent.models import Direction, OrderOutcome, Signal, SignalIntent

TS = datetime(2026, 7, 15, 13, 31, tzinfo=timezone.utc)


def entry_signal() -> Signal:
    return Signal(
        timestamp=TS,
        symbol="NQ",
        intent=SignalIntent.ENTRY,
        direction=Direction.LONG,
        entry_price=Decimal("20100.25"),
        stop_price=Decimal("20090.25"),
        target_price=Decimal("20120.25"),
        quantity=2,
        reason="test",
    )


def flatten_signal() -> Signal:
    return Signal(
        timestamp=TS,
        symbol="NQ",
        intent=SignalIntent.FLATTEN,
        direction=Direction.LONG,
        quantity=2,
        reason="cutoff",
    )


class Server:
    """A real HTTP server on a real port."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.status = 200
        self.delay = 0.0

    async def handler(self, request: web.Request) -> web.Response:
        self.received.append(await request.json())
        if self.delay:
            await asyncio.sleep(self.delay)
        return web.json_response({"ok": self.status < 400}, status=self.status)

    async def ping(self, request: web.Request) -> web.Response:
        return web.json_response({})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/hook", self.handler)
        app.router.add_get("/hook", self.ping)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"http://127.0.0.1:{port}/hook"

    async def stop(self) -> None:
        await self._runner.cleanup()


@pytest.fixture
async def server():
    srv = Server()
    url = await srv.start()
    yield srv, url
    await srv.stop()


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_2xx_is_filled(status: int) -> None:
    assert classify(status) is OrderOutcome.FILLED


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_most_4xx_is_a_real_rejection(status: int) -> None:
    """The broker understood the request and declined it. That is knowledge."""
    assert classify(status) is OrderOutcome.REJECTED


@pytest.mark.parametrize("status", [408, 429])
def test_408_and_429_are_unknown_not_rejections(status: int) -> None:
    """Both are 4xx but neither says the order was not actioned."""
    assert classify(status) is OrderOutcome.UNKNOWN


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_unknown_not_rejected(status: int) -> None:
    """The order may well have been accepted before whatever failed
    downstream. Calling it a rejection tells the operator a position does not
    exist when it might."""
    assert classify(status) is OrderOutcome.UNKNOWN


# --- against the real server ------------------------------------------------


async def test_a_successful_post_is_filled(server) -> None:
    srv, url = server
    executor = WebhookExecutor("broker", url=url, account_id="tradeify")

    result = await executor.execute(entry_signal())
    await executor.close()

    assert result.outcome is OrderOutcome.FILLED
    assert result.success is True
    assert result.account_id == "tradeify"


async def test_a_rejection_is_not_widened_to_unknown(server) -> None:
    srv, url = server
    srv.status = 422
    executor = WebhookExecutor("broker", url=url)

    result = await executor.execute(entry_signal())
    await executor.close()

    assert result.outcome is OrderOutcome.REJECTED
    assert result.needs_reconciliation is False


async def test_a_server_error_is_unknown(server) -> None:
    srv, url = server
    srv.status = 503
    executor = WebhookExecutor("broker", url=url)

    result = await executor.execute(entry_signal())
    await executor.close()

    assert result.outcome is OrderOutcome.UNKNOWN
    assert result.needs_reconciliation is True


async def test_a_timeout_is_unknown(server) -> None:
    """The request went out. Whether the broker acted on it is unknowable
    from here."""
    srv, url = server
    srv.delay = 2.0
    executor = WebhookExecutor("broker", url=url, timeout=0.05)

    result = await executor.execute(entry_signal())
    await executor.close()

    assert result.outcome is OrderOutcome.UNKNOWN
    assert result.error == "timeout"


async def test_an_unreachable_host_is_unknown_and_does_not_raise() -> None:
    """execute() must never raise -- the router's guard is a backstop, not
    the contract."""
    executor = WebhookExecutor("broker", url="http://127.0.0.1:1/hook", timeout=0.5)

    result = await executor.execute(entry_signal())
    await executor.close()

    assert result.outcome is OrderOutcome.UNKNOWN


# --- payload ----------------------------------------------------------------


async def test_prices_are_sent_as_strings_not_floats(server) -> None:
    """A price that survived Decimal from the strategy must not become a
    float in the last three lines before it leaves the process."""
    srv, url = server
    executor = WebhookExecutor("broker", url=url)

    await executor.execute(entry_signal())
    await executor.close()

    payload = srv.received[0]
    assert payload["entry"] == "20100.25"
    assert isinstance(payload["entry"], str)


async def test_a_flatten_inverts_the_side(server) -> None:
    """Signal.direction on a FLATTEN is the direction of the position being
    closed, so closing a LONG has to send a sell."""
    srv, url = server
    executor = WebhookExecutor("broker", url=url)

    await executor.execute(flatten_signal())
    await executor.close()

    payload = srv.received[0]
    assert payload["action"] == "close"
    assert payload["side"] == "sell"


async def test_a_flatten_carries_no_prices(server) -> None:
    srv, url = server
    executor = WebhookExecutor("broker", url=url)

    await executor.execute(flatten_signal())
    await executor.close()

    assert "entry" not in srv.received[0]


# --- lifecycle --------------------------------------------------------------


async def test_the_session_is_reused_across_orders(server) -> None:
    """A session per request puts a TLS handshake between the signal and the
    fill."""
    srv, url = server
    executor = WebhookExecutor("broker", url=url)

    await executor.execute(entry_signal())
    first = executor._session
    await executor.execute(entry_signal())
    second = executor._session
    await executor.close()

    assert first is second


async def test_close_releases_the_session(server) -> None:
    """This is the resource Executor.close() was added for."""
    srv, url = server
    executor = WebhookExecutor("broker", url=url)
    await executor.execute(entry_signal())
    session = executor._session

    await executor.close()

    assert session is not None and session.closed


async def test_close_is_safe_when_nothing_was_ever_sent() -> None:
    await WebhookExecutor("broker", url="http://example.invalid/hook").close()


async def test_a_missing_url_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="webhook URL"):
        WebhookExecutor("broker", url="")


async def test_health_check_survives_an_unreachable_host() -> None:
    executor = WebhookExecutor("broker", url="http://127.0.0.1:1/hook", timeout=0.5)

    assert await executor.health_check() is False
    await executor.close()
