"""Hajer's replay sandbox: the child-process half of a replay (the runner lives in Hajer's backend).

Hajer runs one case of a customer's workflow with the revision's own interpreter as
`python -I -S -B …/hajer/replay/_boot.py SPEC RECEIPT EGRESS_LOG`, inside an operating-system sandbox with
no network but the parent's egress tunnels. Inside, `_guard.py` sends bytes only to the declared model
provider through its tunnel, answers recorded non-model HTTP boundaries from the case's evidence and
refuses every other send; `_hooks.py` refuses every other socket, name lookup and process in an audit
hook; `_database.py` refuses database drivers; every event is logged as it happens. `_entry.py` turns the
frozen adapter into one call; `_case.py` classifies the attempt fail-closed and sends a REPLAYED output to
`verify`. Nothing here is imported by `import hajer`.
"""
