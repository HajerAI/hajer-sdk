"""The `-I -S` children find the venv's own packages on any interpreter, however its distribution patched `sysconfig`.

Three scripts start a child before `hajer` is importable (`replay/_boot.py`, `replay/_reach_boot.py`,
`pytest_plugin/_boot.py`), and each has to put the venv's site-packages on the path itself. Asking `sysconfig` for them
answered `<venv>/local/lib/pythonX.Y/dist-packages` on Ubuntu's `python3.12` and Homebrew's own prefix on macOS — a
directory no venv fills — so the child died on `import httpx` and the plugin said only `CHILD_FAILED`. The venv here is
a real one, built from the interpreter running the tests, and the probe runs under the child's own `-I -S -B`.
"""

import subprocess
import sys
import venv
from pathlib import Path

import pytest

from hajer.pytest_plugin import _boot as plugin_boot
from hajer.replay import _boot as replay_boot
from hajer.replay import _reach_boot as reach_boot

BOOTS: dict[str, Path] = {
    name: Path(str(module.__file__))
    for name, module in (("replay", replay_boot), ("reach", reach_boot), ("plugin", plugin_boot))
}
_WINDOWS = sys.platform == "win32"


@pytest.fixture(scope="module")
def fresh_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A venv of the running interpreter, without pip: `pyvenv.cfg`, a `python`, and an empty site-packages."""
    directory = tmp_path_factory.mktemp("child-boot") / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=not _WINDOWS).create(directory)
    return directory


def _site_packages(venv_dir: Path) -> Path:
    """Where `venv` put the environment's packages, spelled without consulting `site` or `sysconfig`."""
    if _WINDOWS:
        return venv_dir / "Lib" / "site-packages"
    return venv_dir / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"


@pytest.mark.parametrize("boot", BOOTS.values(), ids=list(BOOTS))
def test_a_child_puts_the_venvs_own_site_packages_on_its_path(fresh_venv: Path, boot: Path) -> None:
    python = fresh_venv / ("Scripts/python.exe" if _WINDOWS else "bin/python")
    # The script's `site_directories`, run as the script runs it: `-I -S -B`, `sys.prefix` set to the venv, and nothing
    # of `hajer` imported (`run_path` under another name leaves the `__main__` guard closed).
    probe = (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "venv = Path(sys.executable).parent.parent\n"
        "assert (venv / 'pyvenv.cfg').is_file(), sys.executable\n"
        "sys.prefix = sys.exec_prefix = str(venv)\n"
        f"found = runpy.run_path({str(boot)!r}, run_name='probe')['site_directories']()\n"
        "print('\\n'.join(found))\n"
    )
    result = subprocess.run(  # noqa: S603 - the venv's interpreter on code this test wrote
        [str(python), "-I", "-S", "-B", "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    found = [Path(line).resolve() for line in result.stdout.splitlines() if line]
    assert found, result.stdout
    assert all(fresh_venv.resolve() in path.parents for path in found), found
    assert _site_packages(fresh_venv).resolve() in found, found


@pytest.mark.parametrize("boot", BOOTS.values(), ids=list(BOOTS))
def test_a_child_boot_asks_site_and_never_sysconfig_where_the_venv_keeps_its_packages(boot: Path) -> None:
    source = boot.read_text(encoding="utf-8")
    assert "import sysconfig" not in source
    assert "site.getsitepackages(" in source
