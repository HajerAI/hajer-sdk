# Support example

The smallest application that shows what `hajer eval` adds to an ordinary promptfoo suite.

```
wf_support                      examples/support/app.py   @hajer.workflow
└── cmp_refund_agent                                      @hajer.component
    └── tool_refund_status  (get_refund_status)           @hajer.tool
```

```bash
pip install "hajer[evals]"         # Node >= 22.22 must be on PATH; the engine installs itself on first run
cd examples/support
hajer eval                         # runs promptfooconfig.yaml; writes the payload under ~/.cache/hajer/runs/<run id>/
hajer eval --payload-out run.json  # the same, with the platform payload where you asked for it
hajer eval --obligation obl_refund_status_disclosed   # only the tests covering this obligation (the greeting is skipped)
hajer eval -c failing.yaml         # exits 100: one case fails on purpose
```

No model key is needed: the application is deterministic and `llm-rubric` is graded by `grader.py`, a
promptfoo provider that applies the rubric's `must not say:` rule. Swap it for a real grader by deleting the
`provider:` line under the assertion.
