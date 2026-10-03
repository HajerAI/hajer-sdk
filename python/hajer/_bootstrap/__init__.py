"""Holds `sitecustomize.py`, the file this directory exists to put on somebody's `PYTHONPATH`.

A package rather than a bare directory so that every build backend ships it with the wheel. Importing
`hajer._bootstrap` does nothing; `python -m hajer attach-path` prints where it is.
"""
