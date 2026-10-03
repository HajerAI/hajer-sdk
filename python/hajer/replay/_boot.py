"""The replay child's first code: `python -I -S -B …/hajer/replay/_boot.py SPEC RECEIPT EGRESS_LOG`.

`-I` ignores `PYTHONPATH` and every other `PYTHON*` variable and keeps the working directory and this
script's directory off `sys.path`; `-S` skips `site`, so neither a `.pth` hook nor a `sitecustomize.py` runs
before the guard. Without `site` nobody reads the venv's `pyvenv.cfg` either, so this file does the one
part of that it needs — the venv becomes `sys.prefix` — then puts only the venv's site-packages (without
processing their `.pth` files) and the SDK on the path and hands over to `hajer.replay.__main__`, which
installs the guard before anything of the revision is importable. Standard library only until then.

Where the venv keeps its packages is asked of `site.getsitepackages`, the same computation `site.venv()`
runs at an ordinary start, and never of `sysconfig`. A distribution that patches `sysconfig`'s install
scheme answers a venv-relative `get_paths()` with its own layout — Debian and Ubuntu say
`<venv>/local/lib/pythonX.Y/dist-packages`, Homebrew its own prefix — where no venv has ever put a package,
and a child started that way could not import the SDK's own dependencies (`CHILD_FAILED`, nothing more said).
Importing `site` under `-S` runs nothing: its `main()` is skipped while the flag is set.
`_reach_boot.py` and `pytest_plugin/_boot.py` start the same way and repeat these lines rather than import
them: `hajer` is not importable until they have run.
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
    from hajer.replay.__main__ import main  # noqa: PLC0415 - importable only once the path above is set

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(_start())
