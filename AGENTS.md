## Sol + Luna Max routing

- Sol owns requirements, decomposition, architecture, integration, final review, and the final answer.

- For every independent, substantial, and separately verifiable subtask, start one explicitly named luna_worker instance. Do not use unnamed default agents for these tasks.

- Keep trivial or tightly coupled work in Sol. Read-only tasks may run in parallel.

- File-writing workers must use separate worktrees or non-overlapping file scopes. If isolation is unavailable, run writing tasks sequentially.

- A Luna worker must not spawn another agent, redefine architecture, make destructive changes, or modify unrelated files.

- Every handoff must include scope, inputs, output, acceptance criteria, and relevant file boundaries.

- Sol must wait for every worker, inspect each result and diff, run or verify relevant checks, re-dispatch failures when useful, and perform the final integration and acceptance.
