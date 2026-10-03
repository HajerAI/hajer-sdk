"""The import-time hook, where the interpreter finds it on its own: `sitecustomize`.

Python imports a module named `sitecustomize` at interpreter start-up from anywhere on `sys.path`, before
the application's first line. Putting **this directory** on `PYTHONPATH` therefore attaches a process
whose source nobody touched:

    PYTHONPATH="$(python -m hajer attach-path)" HAJER_ATTACH=1 python -m your_app

The body is one import, and `hajer.autoattach` is what reads `HAJER_ATTACH`: with the variable off this
file costs one module import and does nothing else.

**Two things it is honest about.** It is not chained: if the environment already has a `sitecustomize` of
its own and this directory comes first on `PYTHONPATH`, that one is shadowed — put this directory last, or
use the one-line `import hajer.autoattach` in the entry point instead. And an exception here would break
interpreter start-up for the whole process, so it catches everything and gives up quietly: a process that
cannot attach must still run.
"""

from __future__ import annotations


def _attach_this_interpreter() -> object:
    """The whole file: one import, and what it decided. Never an exception out of start-up."""
    try:
        import hajer.autoattach  # noqa: PLC0415 - a top-level import here would raise out of start-up
    except Exception:  # noqa: BLE001 - failing to attach may never stop an interpreter from starting
        return None
    return hajer.autoattach.ATTACHED


#: The attachment this start-up made, or `None` when `HAJER_ATTACH` was off or `hajer` is not installed.
#: Assigned so that nothing has to be told the import above is for its effect.
ATTACHED = _attach_this_interpreter()
