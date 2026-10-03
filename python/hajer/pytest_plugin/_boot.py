"""Bootstrap before customer imports; reuse the replay child's venv path discovery."""

import sys
import sysconfig
from pathlib import Path


def main() -> None:
    venv = Path(sys.executable).parent.parent
    if (venv / "pyvenv.cfg").is_file():
        sys.prefix = sys.exec_prefix = str(venv)
    paths = sysconfig.get_paths(vars={"base": sys.prefix, "platbase": sys.exec_prefix})
    sys.path.extend(dict.fromkeys((paths["purelib"], paths["platlib"])))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hajer.pytest_plugin._child import run  # noqa: PLC0415 - dependencies now importable

    run(sys.argv[1:])


if __name__ == "__main__":
    main()
