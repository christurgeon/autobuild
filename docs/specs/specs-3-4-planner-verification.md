# Hardening specs: #3 Planner + #4 Acceptance-level verification

> Wave 2 of the autonomy hardening specs (see the prior issue for #1 permissions + #2
> timeout/budget). Each was drafted, then adversarially reviewed against the actual
> code; 🛡️ marks where the review changed the design. The two are tightly coupled: the
> verifier's leverage is at **plan time**, so #3 must emit frozen, machine-checkable
> oracles that #4 runs deterministically.

---

## Spec #3 — Planner (GOAL + SPEC → reviewable backlog)

**Goal:** turn a spec into a well-formed backlog where the harness validates *form* and
the human owns *substance*. The planner never decides what it cannot validate.

1. 🛡️ **Planner emits symbolic handles, not IDs.** `next_task_id` is collision-safe only
   under the lock with write-then-allocate; an LLM emitting N files at once can't honor
   that, `create_task_file` clobbers, and duplicate frontmatter IDs silently shadow in
   `task_index`. The planner outputs a structured plan — `handle`, `title`, `goal`,
   `acceptance[]`, `owns[]`, `depends_on[]`-over-handles — and the **harness** assigns
   `task-NNN` under `backlog_lock` (the `file_followups` pattern), rewriting edges
   handle→id.
2. 🛡️ **Hierarchical, just-in-time — not one monolithic planning session.**
   `autobuild plan` (epic pass: GOAL+SPEC → 5–10 epics) → `autobuild plan --expand`
   (one session per epic, reads only its slice → tasks). Bounds context, isolates a bad
   expansion, avoids stale late tasks. Drafts to `.autobuild/plan/`; `--accept` moves
   them into `tasks/` (append-only; never rewrites human-edited files).
3. 🛡️ **`owns:` file-globs → a contention graph (the real merge-hell fix).** Missing dep
   edges don't fail loudly — in `pr` mode two "independent" tasks both touching one file
   open two clean PRs and conflict off-harness. File contention is NOT a logical
   dependency. Each task declares `owns:`; the harness auto-serializes overlapping owners
   with no DAG path (a file-mutex), distinct from logical deps. Build agents update
   `owns:`; harness warns when `git diff --name-only` exceeds it.
4. 🛡️ **Every acceptance criterion is machine-checkable or explicitly human-only.** Each
   is `auto: <command>` (a frozen oracle) OR tagged `human-review:`. A pure-prose
   criterion with neither is rejected at plan time. Meaningfulness/coverage stay human.
5. 🛡️ **Harness validates the draft (hard reject):** frontmatter parses; handles unique;
   deps resolve; no cycle/missing (`stuck_tasks` on the draft after resolving handles→ids);
   each criterion has an `auto:`/`human-review` token; `owns:` ⊆ GOAL "MAY change" and
   ∩ "MUST NOT" = ∅; near-duplicate detection.
6. 🛡️ **Plan report for review at the right altitude:** the dep DAG, the contention graph,
   prose-only/vacuous criteria, longest-chain depth, leaf tasks. The human reviews graph
   properties + epics, not 60 prose files.
7. 🛡️ **Decompose-on-block feedback loop:** a task BLOCKED with a "too large" reason
   triggers `autobuild plan --split <tid>` (cheap single-task re-decomposition),
   continuing IDs and re-pointing dependents. Requires the #2 session timeout.
8. Priority defaults to topological depth (not LLM-invented); each `depends_on` edge
   carries a one-line rationale; `plan` refuses while a `run` holds the lock.

**Stays human:** true task sizing, criteria meaningfulness, SPEC coverage, whether a
logical edge is real, final go/no-go on a generated backlog.

**Tests:** self-collision (dup handle → reject); re-plan id continuity (no clobber,
existing files byte-unchanged); cycle in draft → reject; dangling handle → reject; file
contention without logical dep → harness serializes; vacuous criterion → flagged,
`auto:` command → accepted; GOAL-boundary violation (`owns:` ∩ MUST-NOT) → reject;
over-serialization depth flagged; oversize task → `--split` continues ids; `plan` during
a live `run` refused; validator deterministic even though planner isn't.

---

## Spec #4 — Acceptance-level verification

**Goal:** confirm a task meets its acceptance criteria before integrating — mostly
deterministically. **It cannot verify correctness**; it catches honest-but-wrong, not
malicious; a human stays mandatory for rejects, inconclusive, subjective, boundary tasks.

1. 🛡️ **Independence is enforced, not prompted.** The oracle is planner-authored and
   frozen; the harness rejects any COMPLETE whose diff touches the oracle files
   (`git diff --name-only` ∩ oracle glob ≠ ∅). The implementer may add its own tests
   elsewhere; it may not edit the oracle.
2. 🛡️ **Deterministic acceptance gate, no LLM.** After `verify_checks`, the harness
   re-creates a clean worktree from the branch tip (not the build agent's lingering tree),
   runs a declared `setup:` step, then runs each `auto:` command itself and records exit
   codes. All green + oracle untouched → eligible to integrate.
3. 🛡️ **Verdict is harness-owned, computed from exit codes** — never the model's free text
   — written to a verifier-only dir the build agent never had access to. Defeats planted
   `verdict.json` / malicious `conftest.py`; a prompt-injected "emit PASS" can't flip an
   exit-code-driven decision.
4. 🛡️ **LLM judgment is a downgrade-only residual**, only for criteria with no `auto:`
   command, spawned as a tracked session (supervised by `_harvest`), NOT a blocking call
   in `reap_session` (which would stall all `max_parallel` slots and double wall-clock).
   It can only downgrade PASS→FAIL; a model "PASS" the harness can't deterministically
   confirm → NEEDS_HUMAN, never auto-integrate. Verifier limited to read + declared test
   commands; egress denied at the VM level (prerequisite; until then, not a boundary).
   Untrusted inputs (diff, criteria) delimited as data.
5. 🛡️ **Three outcomes, not two:** PASS → integrate/`done`; REJECT (a deterministic
   acceptance command failed *with reproducing evidence*) → new terminal status
   `rejected`, branch kept, bounded re-spawn (attempts counter) or human; INCONCLUSIVE
   (setup failed / verifier crashed / subjective criterion) → `NEEDS_HUMAN`. "Couldn't
   run" must never read as "feature is wrong."
6. 🛡️ **Idempotency:** extend the reaped/meta record with
   `verify: pending|pass|reject|inconclusive` so a crashed verifier resumes
   deterministically (same philosophy as `_classify_sentinel`).
7. 🛡️ **Env tension resolved by a declared `setup:` step** run by the harness in the
   verifier's tree; its failure is INCONCLUSIVE, never REJECT.
8. **Composition:** `verify_checks` (global green) → acceptance gate (per-task `auto:` +
   oracle-untouched) → optional LLM residual (downgrade-only) → integrate. Each layer can
   only block relative to the prior; none can force an integrate. LLM residual opt-in
   (`verify_acceptance: bool`, mirroring `verify_checks`).

**Fundamental limit (plainly):** at best this confirms a planner-authored,
implementer-frozen set of commands exits zero in a clean rebuild of the branch, and the
diff didn't touch them. It does NOT cover oracle coverage gaps, subjective criteria, or a
malicious implementer on the shared VM (no boundary until #1). An LLM "PASS" not backed by
an exit code is "looks good to me" → must abstain to a human. Much of this arguably
belongs in CI/PR review; the loop's job is to produce the branch + the frozen oracle.

**Tests:** tautological test (frozen oracle catches buggy code + buggy test → rejected);
oracle tampering (diff edits oracle → rejected pre-run); planted verdict / fake conftest
ignored (verdict harness-computed in re-created tree); false FAIL / missing dep →
NEEDS_HUMAN not rejected; subjective criterion → rejected at plan time or verifier
abstains; verifier crash/timeout resumes from `pending`, no hang/silent integrate;
prompt-injection can't flip exit-code verdict; same branch twice → same decision; long
verifier doesn't block `claim_tasks`.

---

_Foundation note: #4's deterministic gate and #3's frozen oracle depend on the #1
sentinel/permission work and the #2 session-timeout + tracked-session machinery landing
first._

🤖 Generated with [Claude Code](https://claude.com/claude-code)
