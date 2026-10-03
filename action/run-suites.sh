#!/usr/bin/env bash
# Customer CI owns app dependencies and provider credentials. Replay needs neither provider nor Hajer keys.
set -euo pipefail
if ! python -m pip install "$GITHUB_ACTION_PATH/../python[ci]"; then
  echo "HAJER_CI_INSTALL_FAILED"
  exit 2
fi
cd "$GITHUB_WORKSPACE"
suites="${HAJER_SUITES_DIR:-.hajer/suites}"
# Hajer's suites run apart from the customer's pytest setup, which could mask an outcome or spend attempts: no
# addopts (PYTEST_ADDOPTS or the customer's ini), no auto-loaded plugins (rerunfailures, xdist, flaky, ...), no
# conftest.py, an empty ini of our own, and only the suite directory collected. The plugin itself also refuses
# re-runs and ignores xfail. Model judgments are advisory; deterministic and infrastructure failures fail.
unset PYTEST_ADDOPTS PYTEST_PLUGINS
# Traffic from a suite run is tagged `ci`, so it is never selected as a test input.
export HAJER_ENVIRONMENT=ci
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
ini="${RUNNER_TEMP}/hajer-pytest.ini"
printf '[pytest]\n' >"$ini"
args=(-c "$ini" --rootdir "$GITHUB_WORKSPACE" --noconftest -p no:cacheprovider -p no:rerunfailures -p no:xdist)
args+=(-p hajer.pytest_plugin --hajer-suites "$suites" --hajer-results "${RUNNER_TEMP}/hajer-suite-results.json" --hajer-summary "$GITHUB_STEP_SUMMARY")
# The command produces a standalone adapter report. The plugin verifies execution bindings itself in this session:
# a persisted report cannot authorize changed code. A case whose adapter misses its site stays neutral, never green.
checks="${RUNNER_TEMP}/hajer-adapter-checks.json"
if [[ -f .hajer/replay.toml && "${HAJER_SUITES_PRIVATE_INPUTS:-false}" != true ]]; then
  python -m hajer verify-adapters --root "$GITHUB_WORKSPACE" --results "$checks" || echo "The adapter check could not run; each adapter is checked by the plugin instead."
fi
if [[ -f "$checks" ]]; then
  args+=(--hajer-adapter-checks "$checks")
fi
if [[ "${HAJER_SUITES_LIVE:-false}" == true ]]; then
  args+=(--hajer-live)
fi
if [[ "${HAJER_SUITES_PRIVATE_INPUTS:-false}" == true ]]; then
  [[ "${HAJER_SUITES_LIVE:-false}" == true ]] || { echo "Private inputs require live execution"; exit 2; }
  : "${HAJER_API_KEY:?Private inputs need a team API key}"
  : "${HAJER_TEAM_ID:?Private inputs need a team id}"
  : "${HAJER_PROJECT_ID:?Private inputs need a project id}"
  args+=(--hajer-private-inputs --hajer-project-id "$HAJER_PROJECT_ID")
fi
if [[ "${HAJER_SUITES_UPLOAD:-false}" == true ]]; then
  : "${HAJER_API_KEY:?Suite upload needs a team API key}"
  : "${HAJER_TEAM_ID:?Suite upload needs a team id}"
  : "${HAJER_PROJECT_ID:?Suite upload needs a project id}"
  args+=(--hajer-upload --hajer-project-id "$HAJER_PROJECT_ID")
fi
python -m pytest "${args[@]}" "$suites"
