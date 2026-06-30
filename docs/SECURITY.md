# Security posture

**Read this before running autobuild unattended.**

A spawned session is a headless `claude -p` acting on your repo with little supervision.
Be honest about where the boundary is:

- **The default is full bypass (`--dangerously-skip-permissions`), no sandbox gate.** Out
  of the box a session does whatever it needs without prompts — and inherits **this
  machine's git credentials and network**. A prompt-injected `GOAL.md`/task could push to
  your remote or exfiltrate. This is the right default *only* when those are disposable
  (a sandbox VM, or no-push credentials). autobuild prints a loud warning on every
  un-sandboxed bypass spawn. To run fenced, set `dangerously_bypass_permissions: false`.
- **To fence it, re-arm the gate.** Set `require_sandbox_for_bypass: true` and autobuild
  will **refuse to spawn** a bypass session unless `AUTOBUILD_SANDBOX=1` is set — the
  sandbox-only posture. With bypass off entirely, `permission_mode`/`allowed_tools` apply.
- **The allowlist is ergonomics, not a security boundary.** `--allowedTools` with
  `Bash(git:*)` is approximately a full shell (`git config core.pager=…`, command
  chaining, etc. all escape it). **The only real isolation is running autobuild inside a
  disposable sandbox VM.** `permission_mode`/`allowed_tools` keep an *honest* agent on the
  rails; they do not contain a hostile or prompt-injected one.
- **`integration: pr` is only safe when the agent has no push credentials and no network.**
  An autonomous (or prompt-injected via `GOAL.md`/task files) agent with ambient git creds
  could `git push origin HEAD:main` and bypass the verify-before-integrate gate. Pushing
  stays the *harness's* job, after verification — so withhold credentials/egress from the
  session environment.
- **The harness already strips env-based push credentials (defense-in-depth).** Before a
  session launches, its environment has git push tokens and transport helpers removed
  (`GH_TOKEN`/`GITHUB_TOKEN`/`GITLAB_TOKEN`, `SSH_AUTH_SOCK`, `GIT_ASKPASS`/`SSH_ASKPASS`,
  `GIT_SSH_COMMAND`, and inline `GIT_CONFIG_*` injection) — the agent keeps its commit
  identity and its own `ANTHROPIC_*` auth but loses the easy push primitive. This is **not**
  a secret scrubber: file-based credentials (`~/.git-credentials`, OS keychains, `~/.ssh`
  keys) and the network are still inherited, so a disposable VM remains the only real boundary.
- **A cloned target repo's `.claude/` is hostile input.** Its hooks would run with the
  agent's privileges; autobuild passes `--strict-mcp-config` and denies writes to
  `.claude/**`, but the real containment is still the VM.
- **A session is kept on its worktree, and an escape is caught.** The prompt anchors the
  agent only to its worktree and session dir — the contract, `GOAL.md`, and the task are
  *staged into the session dir*, so the agent is never handed a main-checkout path to
  resolve work against (the original escape vector). Belt-and-suspenders for the cases an
  honest anchor can't prevent: `run` snapshots `base_branch` at spawn and the reaper
  **refuses to integrate** if a session left a non-merge commit on base — the signature
  of an agent that committed onto the live base instead of its branch. In `auto-merge`
  (deliverables merge onto base) it **halts the whole run loudly** (`BaseBranchLeak`, exit
  2); in `pr`/`branch` (base is never integrated onto) it just blocks that one task and
  keeps going, so a concurrent commit to base can't stall unrelated work. `run` also
  **refuses to start with a dirty base tree** (override: `AUTOBUILD_ALLOW_DIRTY_BASE=1`),
  since a stray `git add -A` could otherwise sweep uncommitted work into a task commit.
  These keep an *honest* agent contained and make a dishonest one's mess detectable; they
  are not a substitute for the VM.

## TL;DR

The permission allowlist, credential stripping, and worktree containment keep an **honest**
agent on the rails and make a **dishonest** one's mess detectable — but the only real
isolation against a hostile or prompt-injected agent is **a disposable sandbox VM with no
push credentials and no network egress**. Run it there.
