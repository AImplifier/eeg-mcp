"""Server entrypoint.

Tools register themselves as a side effect of being imported, so this module's
only real job is to import them in a stable order and hand control to FastMCP.
"""

from __future__ import annotations

import atexit

from .app import mcp
from .monitor import monitors
from .session import store

# Importing these registers their @mcp.tool() functions on `mcp`.
from . import tools_stream  # noqa: E402,F401
from . import tools_replay  # noqa: E402,F401
from . import tools_dsp  # noqa: E402,F401
from . import tools_events  # noqa: E402,F401
from . import tools_stim  # noqa: E402,F401
from . import tools_viz  # noqa: E402,F401
from . import tools_record  # noqa: E402,F401
from . import tools_processing  # noqa: E402,F401


def _shutdown() -> None:
    """Release boards, serial ports, LSL outlets and monitor ports on the way out.

    Matters more here than in an offline server: leaving a board session open
    can block the next process from acquiring the device at all, and a monitor
    left listening keeps a port bound.
    """
    try:
        monitors.stop_all()
    except Exception:
        pass
    try:
        store.stop_all()
    except Exception:
        pass


def main() -> None:
    atexit.register(_shutdown)
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
