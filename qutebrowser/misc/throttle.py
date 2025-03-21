# SPDX-FileCopyrightText: Jay Kamat <jaygkamat@gmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""A throttle for throttling function calls."""

import dataclasses
import time
from typing import Any, Optional
from collections.abc import Mapping, Sequence, Callable

from qutebrowser.qt.core import QObject

from qutebrowser.utils import usertypes


@dataclasses.dataclass
class _CallArgs:

    args: Sequence[Any]
    kwargs: Mapping[str, Any]


class Throttle(QObject):

    """A throttle to throttle calls.

    If a request comes in, it will be processed immediately. If another request
    comes in too soon, it is ignored, but will be processed when a timeout
    ends. If another request comes in, it will update the pending request.
    """

    def __init__(self,
                 func: Callable[..., None],
                 delay_ms: int,
                 parent: QObject = None) -> None:
        """Constructor.

        Args:
            delay_ms: The time to wait before allowing another call of the
                         function. -1 disables the wrapper.
            func: The function/method to call on __call__.
            parent: The parent object.
        """
        super().__init__(parent)
        self._delay_ms = delay_ms
        self._func = func
        self._pending_call: Optional[_CallArgs] = None
        self._last_call_ms: Optional[int] = None
        self._timer = usertypes.Timer(self, 'throttle-timer')
        self._timer.setSingleShot(True)

    def _call_pending(self) -> None:
        """Start a pending call."""
        assert self._pending_call is not None
        self._func(*self._pending_call.args, **self._pending_call.kwargs)
        self._pending_call = None
        self._last_call_ms = int(time.monotonic() * 1000)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        cur_time_ms = int(time.monotonic() * 1000)
        if self._pending_call is None:
            if (self._last_call_ms is None or
                    cur_time_ms - self._last_call_ms > self._delay_ms):
                # Call right now
                self._last_call_ms = cur_time_ms
                self._func(*args, **kwargs)
                return

            self._timer.setInterval(self._delay_ms -
                                    (cur_time_ms - self._last_call_ms))
            # Disconnect any existing calls, continue if no connections.
            try:
                self._timer.timeout.disconnect()
            except TypeError:
                pass
            self._timer.timeout.connect(self._call_pending)
            self._timer.start()

        # Update arguments for an existing pending call
        self._pending_call = _CallArgs(args=args, kwargs=kwargs)

    def set_delay(self, delay_ms: int) -> None:
        """Set the delay to wait between invocation of this function."""
        self._delay_ms = delay_ms

    def cancel(self) -> None:
        """Cancel any pending instance of this timer."""
        self._timer.stop()
