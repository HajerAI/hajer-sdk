"""The replay child's first code: `python -I -S -B …/hajer/replay/_boot.py SPEC RECEIPT EGRESS_LOG`.

`-I` ignores `PYTHONPATH` and every other `PYTHON*` variable and keeps the working directory and this
script's directory off `sys.path`; `-S` skips `site`, so neither a `.pth` hook nor a `sitecustomize.py` runs
before the guard. Without `site` nobody reads the venv's `pyvenv.cfg` either, so this file does the one
part of that it needs — the venv becomes `sys.prefix` — then puts only the venv's site-packages (without
processing their `.pth` files) and the SDK on the path and hands over to `hajer.replay.__main__`, which
installs the guard before anything of the revision is importable. Standard library only until then.
"""

import sys
import sysconfig
from pathlib import Path


def site_directories() -> tuple[str, ...]:
    """The venv's site-packages, from `sys.prefix` as `_start` set it."""
    paths = sysconfig.get_paths(vars={"base": sys.prefix, "platbase": sys.exec_prefix})
    return tuple(dict.fromkeys((paths["purelib"], paths["platlib"])))


def _start() -> int:
    venv = Path(sys.executable).parent.parent
    if (venv / "pyvenv.cfg").is_file():
        sys.prefix = sys.exec_prefix = str(venv)
    sys.path.extend(site_directories())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hajer.replay.__main__ import main  # noqa: PLC0415 - importable only once the path above is set

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_start())
