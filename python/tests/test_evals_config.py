"""Where a run lives and how the engine is told about it: discovery, the run directory, the overlay, the port."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from hajer._settings import HajerSettings
from hajer.evals._config import (
    HOOK_FUNCTION,
    OVERLAY_NAME,
    discover_configs,
    free_port,
    overlay_document,
    run_directory,
    runs_directory,
    write_overlay,
)


class TestDiscovery:
    def test_promptfoos_own_default_in_the_working_directory(self, tmp_path: Path) -> None:
        (tmp_path / "promptfooconfig.yaml").write_text("prompts: []\n")
        assert discover_configs([], tmp_path) == (tmp_path.resolve() / "promptfooconfig.yaml",)

    def test_yaml_outranks_json_as_promptfoo_itself_orders_them(self, tmp_path: Path) -> None:
        (tmp_path / "promptfooconfig.json").write_text("{}")
        (tmp_path / "promptfooconfig.yml").write_text("prompts: []\n")
        assert discover_configs([], tmp_path)[0].name == "promptfooconfig.yml"

    def test_nothing_found_is_an_empty_tuple_not_a_guess(self, tmp_path: Path) -> None:
        assert discover_configs([], tmp_path) == ()

    def test_explicit_paths_resolve_relative_to_the_working_directory(self, tmp_path: Path) -> None:
        absolute = tmp_path / "elsewhere.yaml"
        found = discover_configs(["suite.yaml", str(absolute)], tmp_path)
        assert found == (tmp_path.resolve() / "suite.yaml", absolute)


class TestRunDirectory:
    def test_is_created_under_the_cache_and_older_runs_past_the_bound_are_removed(self, tmp_path: Path) -> None:
        settings = HajerSettings(cache_dir=str(tmp_path), eval_runs_keep=2)
        runs = runs_directory(settings)
        first = run_directory(settings, "evalrun_1")
        os.utime(first, (time.time() - 30, time.time() - 30))
        second = run_directory(settings, "evalrun_2")
        os.utime(second, (time.time() - 20, time.time() - 20))
        third = run_directory(settings, "evalrun_3")
        assert runs == tmp_path / "runs"
        assert third.is_dir()
        assert second.is_dir()
        assert not first.exists(), "the oldest run beyond HAJER_EVAL_RUNS_KEEP is gone"

    def test_a_run_directory_that_already_exists_is_reused(self, tmp_path: Path) -> None:
        settings = HajerSettings(cache_dir=str(tmp_path))
        once = run_directory(settings, "evalrun_x")
        (once / "keep").write_text("1")
        again = run_directory(settings, "evalrun_x")
        assert again == once
        assert (again / "keep").exists()


class TestOverlay:
    def test_the_overlay_turns_tracing_on_and_attaches_the_hook_by_absolute_path(self, tmp_path: Path) -> None:
        hook = tmp_path / "pkg" / "_hook_entry.py"
        document = overlay_document(port=4319, hook_entry=hook)
        assert document["tracing"] == {
            "enabled": True,
            "otlp": {"http": {"enabled": True, "host": "127.0.0.1", "port": 4319}},
        }
        assert document["extensions"] == [f"file://{hook}:{HOOK_FUNCTION}"]
        assert document["prompts"] == [], (
            "an absent prompts key would make promptfoo add {{prompt}} and double the suite"
        )
        assert HOOK_FUNCTION == "beforeAll", "promptfoo runs a function named like a hook for that hook only"

    def test_write_overlay_puts_the_document_in_the_run_directory(self, tmp_path: Path) -> None:
        target = write_overlay(tmp_path, port=4318, hook_entry=tmp_path / "h.py")
        assert target == tmp_path / OVERLAY_NAME
        assert json.loads(target.read_text()) == overlay_document(port=4318, hook_entry=tmp_path / "h.py")


class TestFreePort:
    @pytest.mark.loopback
    def test_the_preferred_port_is_kept_when_it_is_free(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            spare = int(probe.getsockname()[1])
        assert free_port(spare) == spare

    @pytest.mark.loopback
    def test_a_busy_port_is_replaced_by_one_the_kernel_hands_out(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            busy = int(holder.getsockname()[1])
            chosen = free_port(busy)
        assert chosen != busy
        assert chosen > 0
