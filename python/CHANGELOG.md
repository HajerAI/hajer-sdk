# Changelog

All notable changes to the `hajer` Python package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the package uses [semantic versioning](https://semver.org/);
while the version is `0.x`, a minor release may change the public API.

## [Unreleased]

### Removed

- The CI suites runner: the `hajer.pytest_plugin` pytest plugin and its `hajer[ci]` extra, the guarded
  replay child (`hajer.replay`), the `upload-results` and `verify-adapters` commands of `python -m hajer`,
  the `HAJER_CI_*` and `HAJER_ADAPTER_CHECK_ANSWERS` settings, and the composite GitHub Action at
  `action/`. The application-side SDK (`verify`, `observe`, `wrap`, `scope`, `attach`, `instrument`) is
  unchanged.
- The unpublished TypeScript client (`typescript/`, `@hajer/sdk`). This repository ships one package.

## [0.1.0] - 2026-10-04

First public release. Python 3.11, 3.12 and 3.13.
