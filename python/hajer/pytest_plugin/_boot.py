"""Bootstrap before customer imports: the replay child's start (`hajer/replay/_boot.py`), handing over to `_child`.

Standard library only until the path is set, and the venv's site-packages from `site.getsitepackages` rather
than `sysconfig`, for the reason `replay/_boot.py` gives: a distribution-patched `sysconfig` names a directory
no venv fills, and the child then cannot import the SDK's own dependencies.
"""

import site
import sys
from pathlib import Path


def site_directories() -> tuple[str, ...]:
    """The site-packages of `sys.prefix` as `main` set it: the existing ones, in `site`'s own order."""
    found = site.getsitepackages([sys.prefix, sys.exec_prefix])
    return tuple(dict.fromkeys(path for path in found if Path(path).is_dir()))


def main() -> None:
    venv = Path(sys.executable).parent.parent
    if (venv / "pyvenv.cfg").is_file():
        sys.prefix = sys.exec_prefix = str(venv)
    sys.path.extend(site_directories())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hajer.pytest_plugin._child import run  # noqa: PLC0415 - dependencies now importable

    run(sys.argv[1:])


if __name__ == "__main__":
    main()
