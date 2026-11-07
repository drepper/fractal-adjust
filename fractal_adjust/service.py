"""Serialized hub access for long-running front ends (GUI, MQTT) and timed effects."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import itertools
import threading
from collections.abc import Callable, Sequence

from .effects import EffectSettings
from .hub import Hub, HubError, SubDevice


@dataclasses.dataclass(slots=True)
class _Temporary:
    """A timed effect shown as a non-persistent preview."""

    token: int
    settings: EffectSettings
    ids: frozenset[int] | None
    timer: threading.Timer | None = None


class HubService:
    """Runs every hub operation on one thread and keeps the stack of timed effects.

    Timed effects are shown as previews, which the hub does not store.  When one expires the
    effect below it on the stack is shown again; when the stack is empty the preview is
    cancelled and the hub returns to its stored effect.
    """

    def __init__(self, opener: Callable[[], Hub]) -> None:
        """Use @p opener to open the hub when it is first needed."""
        self._opener = opener
        self.hub: Hub | None = None
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub")
        self._temps: list[_Temporary] = []
        self._tokens = itertools.count(1)
        self._closed = False

    def submit_plain[T](self, fn: Callable[[], T]) -> concurrent.futures.Future[T]:
        """Run @p fn on the hub thread."""
        return self._pool.submit(fn)

    def submit[T](self, fn: Callable[[Hub], T]) -> concurrent.futures.Future[T]:
        """Run @p fn with the (lazily opened) hub on the hub thread."""
        return self._pool.submit(lambda: fn(self.open()))

    def open(self) -> Hub:
        """Return the hub, opening it if needed.  Call only on the hub thread."""
        if self.hub is None:
            self.hub = self._opener()
        return self.hub

    def drop(self) -> None:
        """Forget a hub that stopped responding.  Call only on the hub thread."""
        if self.hub is not None:
            try:
                self.hub.close()
            except OSError:
                pass
            self.hub = None
        self._clear_temps()

    @staticmethod
    def _targets(hub: Hub, ids: frozenset[int] | None) -> list[SubDevice]:
        devs = hub.subdevices(details=False)
        return [d for d in devs if ids is None or d.id in ids]

    def _replay(self, hub: Hub) -> None:
        """Show the remaining timed effects, bottom to top, or the stored effect if none."""
        hub.cancel_preview()
        for temp in self._temps:
            hub.upload_effect(temp.settings, preview=True, subdevices=self._targets(hub, temp.ids))

    def show_temporary(self, hub: Hub, settings: EffectSettings, ids: Sequence[int] | None,
                       timeout: float | None) -> None:
        """Show @p settings without storing it, for @p timeout seconds (None: until replaced).

        Call only on the hub thread.
        """
        if settings.info.boot:
            raise ValueError("boot effects cannot be shown temporarily")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        temp = _Temporary(next(self._tokens), settings, frozenset(ids) if ids else None)
        hub.upload_effect(settings, preview=True, subdevices=self._targets(hub, temp.ids))
        self._temps.append(temp)
        if timeout is not None:
            temp.timer = threading.Timer(timeout, self._expire_later, (temp.token,))
            temp.timer.daemon = True
            temp.timer.start()

    def stored_effect_changed(self, hub: Hub) -> None:
        """Re-show timed effects after a stored effect was written.  Call on the hub thread."""
        if self._temps:
            self._replay(hub)

    def _expire_later(self, token: int) -> None:
        """Timer thread: queue the expiry on the hub thread."""
        if not self._closed:
            self._pool.submit(self._expire, token)

    def _expire(self, token: int) -> None:
        for i, temp in enumerate(self._temps):
            if temp.token == token:
                top = i == len(self._temps) - 1
                del self._temps[i]
                if top and self.hub is not None:
                    try:
                        self._replay(self.hub)
                    except HubError:
                        self.drop()
                return

    def _clear_temps(self) -> None:
        for temp in self._temps:
            if temp.timer is not None:
                temp.timer.cancel()
        self._temps.clear()

    @property
    def temporary_count(self) -> int:
        """Number of timed effects currently active."""
        return len(self._temps)

    def shutdown(self, final: Callable[[Hub], None] | None = None) -> None:
        """End timed effects, run @p final, close the hub and stop the thread."""
        def close() -> None:
            hub = self.hub
            if hub is None:
                return
            try:
                if self._temps:
                    self._clear_temps()
                    hub.cancel_preview()
                if final is not None:
                    final(hub)
            finally:
                hub.close()
                self.hub = None
        self._closed = True
        self._pool.submit(close)
        self._pool.shutdown(wait=True)
