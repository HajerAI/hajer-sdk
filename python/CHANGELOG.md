# Changelog

All notable changes to the `hajer` Python package. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the package uses [semantic versioning](https://semver.org/);
while the version is `0.x`, a minor release may change the public API.

## [Unreleased]

## [0.1.0]

First public release.

### Known issues

- Python 3.12 and 3.13: the CI suite plugin's replay child process fails to start (`CHILD_FAILED`), and
  call-site capture inside asyncio tasks records `BaseEventLoop.create_task` instead of the application
  frame. `verify`, `observe` and `wrap` are unaffected. Python 3.11 is fully supported.
