"""`import hajer.autoattach` — attach this process when `HAJER_ATTACH` says so, and otherwise do nothing.

One line, first thing in an entry point, safe to leave in a repository forever:

    import hajer.autoattach    # noqa: F401 - imported for its effect

With `HAJER_ATTACH=1` in the environment it calls `hajer.attach()`: every provider client the process
builds records its model calls, and with a key each becomes a span of its own (`hajer/_attach.py` says
exactly what leaves the process). With the variable unset or off it does nothing at all — no
instrumentation, no socket — so the import is the hook and the variable is the switch.

`hajer/_bootstrap/sitecustomize.py` is this same import, placed where the interpreter finds it at
start-up, for a process whose source nobody may touch:

    PYTHONPATH="$(python -m hajer attach-path)" HAJER_ATTACH=1 python -m your_app

This module is public and has no underscore because it is meant to be written in somebody's code; it is
the only module in the package besides `hajer` itself that is.
"""

from __future__ import annotations

from hajer._attach import attach
from hajer._settings import HajerSettings

#: What this import decided, so a developer can assert on it: the attachment, or `None` when the switch
#: was off. Reading it is how a test says "the hook ran and chose not to attach".
ATTACHED = attach() if HajerSettings.from_env().attach else None
