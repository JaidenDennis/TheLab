from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import date

from nq_agent.execution.base import Executor, NotifyExecutor
from nq_agent.journal import Journal
from nq_agent.models import OrderOutcome, OrderResult, Signal

logger = logging.getLogger(__name__)


class Router:
    """Fans one Signal out to N executors.

    Notify executors run first and are awaited to completion or timeout — the
    human on the manual account is the slow leg and gets the head start. The
    remaining executors then run concurrently, each with an independent
    timeout, so one failing never blocks another.

    asyncio.TaskGroup is deliberately not used: it cancels siblings on failure,
    which is the exact behaviour this must avoid.
    """

    def __init__(
        self,
        executors: list[Executor],
        journal: Journal,
        executor_timeout: float,
        notify_timeout: float,
        partial_fan: str,
        enabled_accounts: Callable[[], set[str] | None] | None = None,
    ) -> None:
        # Executor declares name/account_id/enabled as bare class-level
        # annotations, which Python does not enforce -- a subclass whose
        # __init__ forgets one is a perfectly valid class that raises
        # AttributeError the first time the router touches the missing
        # attribute. That first touch is inside dispatch, i.e. while a signal
        # is being sent to a broker. Wiring is where this should fail, so it
        # fails here, before anything can be dispatched at all. Checked before
        # the duplicate-name scan below, which itself reads `.name`.
        for executor in executors:
            missing = [
                attribute
                for attribute in ("name", "account_id", "enabled")
                if not hasattr(executor, attribute)
            ]
            if missing:
                raise ValueError(
                    f"{type(executor).__name__} does not set {', '.join(missing)}; "
                    "Executor's class-level annotations are a contract its "
                    "__init__ has to satisfy, not defaults it inherits"
                )

        # Router hands out the whole instance list, and OrderResult.executor_name
        # is the only field identifying which destination produced a result.
        # DryRunExecutor(account_id=None) and DryRunNotifier(name) both reduce to
        # the bare name, so two same-named instances would otherwise be silently
        # indistinguishable in every OrderResult and journal line they produce.
        counts = Counter(executor.name for executor in executors)
        duplicates = sorted(name for name, count in counts.items() if count > 1)
        if duplicates:
            raise ValueError(
                f"duplicate executor name(s) {duplicates}: OrderResult.executor_name "
                "would not uniquely identify which destination produced a result"
            )
        self._executors = executors
        self._journal = journal
        self._executor_timeout = executor_timeout
        self._notify_timeout = notify_timeout
        self._partial_fan = partial_fan
        self._enabled_accounts = enabled_accounts

    @property
    def executors(self) -> list[Executor]:
        """Every wired destination, enabled or not. `enabled` is the dispatch
        list; this is the ownership list, which is what teardown needs."""
        return list(self._executors)

    @property
    def enabled(self) -> list[Executor]:
        """Live destinations, recomputed on every read.

        The account allow-list is queried fresh each time so an account can be
        disabled mid-session without restarting the process.
        """
        allowed = self._enabled_accounts() if self._enabled_accounts else None
        return [
            executor
            for executor in self._executors
            if executor.enabled
            and (
                allowed is None
                or executor.account_id is None
                or executor.account_id in allowed
            )
        ]

    def _notifiers(self) -> list[NotifyExecutor]:
        return [e for e in self.enabled if isinstance(e, NotifyExecutor)]

    def _brokers(self) -> list[Executor]:
        return [e for e in self.enabled if not isinstance(e, NotifyExecutor)]

    async def _run_one(self, executor: Executor, signal: Signal, timeout: float) -> OrderResult:
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(executor.execute(signal), timeout)
        except asyncio.TimeoutError:
            # UNKNOWN, not REJECTED: the broker has not answered, which is not
            # the same as answering no. It may be filling the order right now.
            elapsed = int((time.perf_counter() - started) * 1000)
            return OrderResult(
                signal_id=signal.id,
                executor_name=executor.name,
                outcome=OrderOutcome.UNKNOWN,
                account_id=executor.account_id,
                latency_ms=elapsed,
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - an executor must never break the fan-out
            # Also UNKNOWN. The router cannot tell from here whether the
            # exception came before the request left the process or after the
            # broker had already accepted it. Only the executor knows that,
            # and an executor certain of a rejection returns REJECTED itself
            # rather than raising.
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.exception("executor %s raised", executor.name)
            return OrderResult(
                signal_id=signal.id,
                executor_name=executor.name,
                outcome=OrderOutcome.UNKNOWN,
                account_id=executor.account_id,
                latency_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        if result.latency_ms:
            return result
        return result.model_copy(update={"latency_ms": elapsed})

    async def _run_group(
        self, executors: list[Executor], signal: Signal, timeout: float
    ) -> list[OrderResult]:
        if not executors:
            return []
        # return_exceptions=True is defense in depth on top of _run_one's own
        # try/except: _run_one already converts every Exception and every
        # asyncio.TimeoutError into a failed OrderResult, so nothing here
        # should ever actually be a raised exception. But asyncio.TaskGroup is
        # deliberately avoided precisely because one sibling's failure must
        # never take another down, so gather must not be allowed to propagate
        # even a BaseException (e.g. CancelledError) that slips past that
        # guard - one executor misbehaving must never sink the whole fan-out.
        outcomes = await asyncio.gather(
            *(self._run_one(executor, signal, timeout) for executor in executors),
            return_exceptions=True,
        )
        results: list[OrderResult] = []
        for executor, outcome in zip(executors, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error(
                    "executor %s raised outside its own guard: %r", executor.name, outcome
                )
                results.append(
                    OrderResult(
                        signal_id=signal.id,
                        executor_name=executor.name,
                        outcome=OrderOutcome.UNKNOWN,
                        account_id=executor.account_id,
                        latency_ms=int(timeout * 1000),
                        error=f"{type(outcome).__name__}: {outcome}",
                    )
                )
            else:
                results.append(outcome)
        return results

    async def close(self) -> None:
        """Release every executor's resources.

        Every executor, not just the enabled ones: `enabled` gates dispatch,
        but a disabled executor was still constructed and may still be holding
        whatever it opened. Failures are logged and swallowed rather than
        raised -- this runs in Engine.run's teardown, often while an exception
        is already on its way out, and one leg failing to close must not strand
        the others any more than one leg failing to dispatch does.
        """
        for executor in self._executors:
            try:
                await executor.close()
            except Exception:  # noqa: BLE001 - teardown never raises
                logger.exception("executor %s failed to close", executor.name)

    async def dispatch(self, signal: Signal, session_date: date) -> list[OrderResult]:
        notifiers = self._notifiers()
        results = await self._run_group(list(notifiers), signal, self._notify_timeout)
        results.extend(await self._run_group(self._brokers(), signal, self._executor_timeout))

        for result in results:
            await self._journal.write(
                "order_result",
                session_date,
                signal_id=result.signal_id,
                executor_name=result.executor_name,
                account_id=result.account_id,
                # Both: `outcome` is the fact, `success` is the summary an
                # operator greps for. Dropping success would break every
                # existing journal reader; dropping outcome would lose the
                # distinction this whole field exists to carry.
                outcome=result.outcome,
                success=result.success,
                latency_ms=result.latency_ms,
                error=result.error,
            )

        failed = [result for result in results if not result.success]
        if failed and self._partial_fan == "alert_only":
            names = ", ".join(result.executor_name for result in failed)
            message = f"partial fan-out on signal {signal.id}: {names} failed"
            for notifier in notifiers:
                await notifier.alert(message)
            await self._journal.write(
                "executor_alert", session_date, signal_id=signal.id, detail=message
            )

        return results
