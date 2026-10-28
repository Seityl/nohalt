"""A new nohalt installed under a running supervisor is switched to, never mixed in (LIV-14)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from nohalt import upgrade
from nohalt.state import RUNNING
from nohalt.util import pid_alive
from test_reload import Exec, on_disk  # noqa: F401  (a fixture)


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A copy of the nohalt package, standing in for the installed one."""
    dst = tmp_path / "site" / "nohalt"
    shutil.copytree(upgrade.PACKAGE, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def _install(pkg: Path, text: str = "# a new release\n") -> None:
    """What an upgrade does to the package directory: a file replaced, one added."""
    target = pkg / "agents" / "claude_code.py"
    data = target.read_text()
    target.unlink()
    target.write_text(data + text)
    (pkg / "brand_new.py").write_text("X = 1\n")


def _watch(pkg: Path, clock: Clock, versions: list[str], checks: list[str] | None = None) -> upgrade.Watch:
    def check(root: Path) -> str:
        assert root == pkg
        return checks.pop(0) if checks else ""

    return upgrade.Watch(pkg, clock=clock, settle=5, installed=lambda: versions[-1], check=check)


def test_an_unchanged_package_is_not_an_upgrade(package):
    clock = Clock()
    w = _watch(package, clock, ["0.2.0"])
    (package / "__pycache__").mkdir(exist_ok=True)
    (package / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"x")  # written by whoever imports it
    clock.t += 3600
    assert not w.settled()


def test_an_upgrade_is_switched_to_only_once_it_has_settled(package):
    clock = Clock()
    versions = ["0.2.0"]
    w = _watch(package, clock, versions)
    _install(package)
    versions.append("0.3.0")
    assert not w.settled()  # just seen: an install may still be writing files
    clock.t += 4
    assert not w.settled()
    (package / "another.py").write_text("")  # still installing: the wait starts again
    clock.t += 4
    assert not w.settled()
    clock.t += 5
    assert w.settled()
    assert w.broken() == ""


def test_a_version_change_alone_is_an_upgrade(package):
    clock = Clock()
    versions = ["0.2.0"]
    w = _watch(package, clock, versions)
    versions.append("0.2.1")
    w.settled()
    clock.t += 5
    assert w.settled()


def test_a_simulated_upgrade_re_executes_the_supervisor_and_agents_carry_on(on_disk, package):  # noqa: F811
    w = on_disk()
    w.issue(1, "sleep 3", "close")
    w.sup.tick()
    pid = w.rec(1).pid
    clock = Clock()
    versions = ["0.2.0"]
    w.sup.upgrade = _watch(package, clock, versions)
    _install(package)
    versions.append("0.3.0")
    assert not w.sup._restart_pending()  # seen, not yet settled
    clock.t += 5
    assert w.sup._restart_pending()  # ends the wait between ticks

    with pytest.raises(Exec):
        w.sup.run()
    argv = w.execs[-1]
    assert argv[:3] == [sys.executable, "-m", "nohalt"] and argv[-1] == "run"
    assert any(" -> 0.3.0)" in m for m in w.messages)
    assert any("nohalt upgraded on disk: re-executing" in m for m in w.messages)
    assert pid_alive(pid) and w.rec(1).status == RUNNING


def test_an_upgrade_that_does_not_import_keeps_the_loaded_code(on_disk, package):  # noqa: F811
    w = on_disk()
    clock = Clock()
    versions = ["0.2.0"]
    w.sup.upgrade = _watch(package, clock, versions, checks=["ImportError: cannot import name 'UsageSignal'"])
    _install(package)
    versions.append("0.3.0")
    w.sup.upgrade.settled()
    clock.t += 5
    assert w.sup.run(once=True) == 0  # no re-exec
    assert not w.execs
    said = [m for m in w.messages if "does not import" in m]
    assert len(said) == 1 and "UsageSignal" in said[0] and "0.3.0" in said[0]
    clock.t += 60
    assert not w.sup._restart_pending()  # reported once, not every tick

    _install(package, "# fixed\n")  # the next install is tried again
    w.sup.upgrade.settled()
    clock.t += 5
    with pytest.raises(Exec):
        w.sup.run()


def test_imports_cleanly_runs_the_package_on_disk(package):
    assert upgrade.imports_cleanly(package) == ""
    agents = package / "agents" / "__init__.py"
    agents.write_text(agents.read_text().replace("class UsageSignal", "class Renamed"))
    why = upgrade.imports_cleanly(package)
    assert "UsageSignal" in why


def test_every_agent_and_integrator_module_is_imported_at_startup(on_disk, monkeypatch):  # noqa: F811
    loaded = upgrade.preload()
    for sub in ("agents", "integrate", "forge", "stacks"):
        for f in (upgrade.PACKAGE / sub).glob("*.py"):
            name = f"nohalt.{sub}" if f.stem == "__init__" else f"nohalt.{sub}.{f.stem}"
            assert name in loaded and name in sys.modules, name
    for f in upgrade.PACKAGE.glob("*.py"):
        if f.stem not in ("__init__", "__main__"):
            assert f"nohalt.{f.stem}" in loaded

    calls: list[int] = []
    monkeypatch.setattr(upgrade, "preload", lambda: calls.append(1) or [])
    w = on_disk()
    w.sup.run(once=True)
    assert calls == [1]
