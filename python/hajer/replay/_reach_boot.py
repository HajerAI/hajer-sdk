"""The adapter-reach child's first code: `python -I -S -B …/hajer/replay/_reach_boot.py SPEC RECEIPT EGRESS_LOG`.

The same start as the replay child (`_boot.py`, standard library only until the path is set, and the venv's
site-packages from `site.getsitepackages` for the reason given there): the venv becomes `sys.prefix`, only its
site-packages and the SDK go on the path, and `_reach` installs the guard before anything of the application
is importable.
"""

import site
import sys
from pathlib import Path


def site_directories() -> tuple[str, ...]:
    """The site-packages of `sys.prefix` as `_start` set it: the existing ones, in `site`'s own order."""
    found = site.getsitepackages([sys.prefix, sys.exec_prefix])
    return tuple(dict.fromkeys(path for path in found if Path(path).is_dir()))


def _start() -> int:
    venv = Path(sys.executable).parent.parent
    if (venv / "pyvenv.cfg").is_file():
        sys.prefix = sys.exec_prefix = str(venv)
    sys.path.extend(site_directories())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hajer.replay._reach import main  # noqa: PLC0415 - importable only once the path above is set

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_start())
