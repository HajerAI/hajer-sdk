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
hajer eval                         # runs the suite hajer.yaml declares (promptfooconfig.yaml); payload under ~/.cache/hajer/runs/<run id>/
hajer eval --payload-out run.json  # the same, with the platform payload where you asked for it
hajer eval --obligation obl_refund_status_disclosed   # only the tests covering this obligation (the greeting is skipped)
hajer eval -c failing.yaml         # exits 100: one case fails on purpose
hajer eval --upload                # with HAJER_API_KEY and HAJER_TEAM_ID set: the run lands on the platform
```

`hajer.yaml` is the repository's declaration: the suite, and the one obligation the tests may name
(`obl_refund_status_disclosed`, about `wf_support`). The platform reads the same file through the repository's
GitHub connection.

No model key is needed: the application is deterministic and `llm-rubric` is graded by `grader.py`, a
promptfoo provider that applies the rubric's `must not say:` rule. Swap it for a real grader by deleting the
`provider:` line under the assertion.
