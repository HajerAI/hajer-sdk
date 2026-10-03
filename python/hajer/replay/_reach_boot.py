"""The adapter-reach child's first code: `python -I -S -B …/hajer/replay/_reach_boot.py SPEC RECEIPT EGRESS_LOG`.

The same start as the replay child (`_boot.py`, standard library only until the path is set): the venv becomes
`sys.prefix`, only its site-packages and the SDK go on the path, and `_reach` installs the guard before anything of the
application is importable.
"""

import sys
import sysconfig
from pathlib import Path


def _start() -> int:
    venv = Path(sys.executable).parent.parent
    if (venv / "pyvenv.cfg").is_file():
        sys.prefix = sys.exec_prefix = str(venv)
    paths = sysconfig.get_paths(vars={"base": sys.prefix, "platbase": sys.exec_prefix})
    sys.path.extend(dict.fromkeys((paths["purelib"], paths["platlib"])))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hajer.replay._reach import main  # noqa: PLC0415 - importable only once the path above is set

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_start())
