---
id: task-102
title: "#1 — Session permission posture + spawn argv (and the sentinel-location fix)"
status: todo
priority: 1
depends_on: [task-101]
---

## Goal
Make a headless session able to act unattended under a fail-safe, env-gated posture, and
fix the latent bug where a confined agent cannot write its own sentinel.

## Context
- `session.py:111` spawns `claude -p` with **no** permission/tool flags.
- The session dir is `<root>/.autobuild/sessions/<id>` — **outside** the worktree `cwd`
  (`paths.py`). Under real confinement the agent can't write `result.json` → every task
  fails closed and looks like a crash.
- `config.py` has a `VALID_INTEGRATIONS`-style validation pattern to reuse.
- A cloned target repo may ship `.claude/` hooks that execute on spawn.

## Approach (resolved)
- Config keys (validated, added to `KNOWN_KEYS`): `permission_mode` (enum
  {plan,default,acceptEdits,bypassPermissions}), `allowed_tools` (list of non-empty),
  `session_max_turns` (int≥1), `dangerously_bypass_permissions` (bool),
  `require_sandbox_for_bypass` (bool, default true).
- Build argv: `--permission-mode`; `--allowedTools` = `allowed_tools` + an auto-derived
  `Bash(<cmd>:*)` per `checks` entry + `Bash(git:*)`; **`--add-dir <session_dir>`** scoped to
  *exactly* this session's dir (never the parent `.autobuild/sessions/` or `<root>`);
  `--strict-mcp-config`; `--max-turns` when set. Deny writes to `.claude/**`.
- Bypass gating: pass `--dangerously-skip-permissions` only if
  `dangerously_bypass_permissions` AND (`not require_sandbox_for_bypass` OR
  `AUTOBUILD_SANDBOX=1`); otherwise **refuse to spawn** with a clear error. Log the chosen
  mode loudly at spawn.
- README: state plainly that the allowlist is ergonomics, **not** a security boundary (the
  sandbox VM is); and that `integration: pr` is only safe when the agent has no push
  credentials / no network.

## Acceptance criteria
- [ ] `--add-dir` target equals the session dir and is a subpath check (not parent/root).
- [ ] With `permission_mode: acceptEdits`, every `checks` command + `git commit` are covered by `--allowedTools`.
- [ ] Bypass refused without `AUTOBUILD_SANDBOX`; permitted (flag present) with it.
- [ ] `--strict-mcp-config` present; deny rule for `.claude/**`.
- [ ] New config keys validated (aggregated `ConfigError`); added to `KNOWN_KEYS`.

## Test matrix
- [ ] `--add-dir` target is exactly the session dir; assert it is NOT `.autobuild/sessions` and NOT `<root>`
- [ ] confined-agent sentinel path is a subpath of an `--add-dir`ed location (guards the latent bug)
- [ ] `acceptEdits` ⇒ each `checks` cmd + `git commit` appear in `--allowedTools`
- [ ] bypass: refused without `AUTOBUILD_SANDBOX`; argv has `--dangerously-skip-permissions` with it
- [ ] `--strict-mcp-config` present; `.claude/**` deny rule applied
- [ ] config: bad `permission_mode`, `session_max_turns: 0`, `allowed_tools: "Edit"` (string) each aggregate into one `ConfigError`
- [ ] hostile `.claude/` fixture repo: `SessionStart`/`PreToolUse` hook does not execute
- [ ] existing e2e/stub tests pass with the new flags injected (stub tolerant of extra argv)

## Out of scope
Usage parsing + nonzero-exit messaging (task-103); timeouts (task-104/105).
