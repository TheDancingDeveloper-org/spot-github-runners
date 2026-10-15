@AGENTS.md
Route Claude subagent launches by task complexity and failure impact: use Opus 5.5 (`opus-5.5`) at High (`high`) effort for more complex coding tasks, use Sonnet 5.5 (`sonnet-5.5`) at Medium (`medium`) or High (`high`) effort for bounded implementation and cross-module debugging, and use Haiku 5.5 (`haiku-5.5`) at High (`high`) effort for basic code identification and grepping scenarios.

## Claude subagent model routing

These model rules apply to Claude subagent launches. Keep the human-selected main-session model unchanged. Delegate bounded, independent work when parallel execution or context isolation helps; perform small, tightly coupled edits directly in the main session. Read `AGENTS.md` before planning or editing.

| Task category | Model | Model ID | Effort | Guidance |
| --- | --- | --- | --- | --- |
| Basic code identification and grepping scenarios, read-only file or symbol lookup, log summarization, and documentation or configuration checks | Haiku 5.5 | `haiku-5.5` | High (`high`) | Use Haiku 5.5 at High effort for basic code identification and grepping scenarios where fast, accurate retrieval is needed without source mutation. |
| Bounded implementation, unit test additions, or focused code review with explicit contracts | Sonnet 5.5 | `sonnet-5.5` | Medium (`medium`) | Use Sonnet 5.5 at Medium effort for well-scoped code or test changes with clear acceptance criteria and low cross-module ambiguity. |
| Cross-module debugging, state-machine or concurrency reasoning, pricing/storage policy tracing, and multi-file refactors | Sonnet 5.5 | `sonnet-5.5` | High (`high`) | Use Sonnet 5.5 at High effort when changes span multiple modules, require lock or lease reasoning, or diagnose subtle test regressions. |
| More complex coding tasks, ambiguous architecture, security and admission boundaries, VM/disk recovery invariants, and release or runtime-update safety | Opus 5.5 | `opus-5.5` | High (`high`) | Use Opus 5.5 at High effort for more complex coding tasks, high-blast-radius infrastructure logic, and security-critical verification. |

### Subagent launch rules

- Set both `model` and `effort` explicitly when spawning a non-fork subagent, and pass a compact, self-contained brief. Command-line subagent processes must pass `--model` and `--effort` explicitly.
- Route by uncertainty, interacting state, and failure impact rather than diff line count. A small edit to admission locking, startup metadata, or recovery state requires `opus-5.5` at `high` effort.
- Escalate to the next model or effort tier with a concrete reason when a subagent hits an unresolved invariant or a failed approach.
- Provide each subagent with the exact goal, relevant file paths, constraints, owned files, validation commands, and required completion evidence. Give independent review subagents the acceptance contract and raw diff without the implementer's conclusions.
- Parallelize read-only exploration freely. Give each writing subagent exclusive file ownership or an isolated Git worktree so concurrent edits never collide.
- Verify model availability and resolved effort. Environment variables such as `CLAUDE_CODE_EFFORT_LEVEL` or forced subagent model settings can override per-launch parameters; if the active harness lacks per-invocation effort support, use an authorized command-line child with both flags or report the mismatch rather than silently claiming the requested route ran.
