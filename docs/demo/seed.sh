#!/usr/bin/env bash
# Lay down a throwaway autobuild demo project for the README GIF.
# Token-free: drives the real harness against the repo's stub `claude`.
# Usage: seed.sh <dest-dir> <path-to-stub-claude>
set -euo pipefail
DEST="$1"; STUB="$2"

mkdir -p "$DEST"
cd "$DEST"
git init -q -b main
git config user.name  "autobuild demo"
git config user.email "demo@autobuild.invalid"
git commit -q --allow-empty -m "init"

autobuild init >/dev/null

# A stub agent on PATH (real orchestration, canned edits — no tokens spent).
mkdir -p "$DEST/bin"
cp "$STUB" "$DEST/bin/claude"
chmod +x "$DEST/bin/claude"

# Config tuned for a tidy, network-free demo.
cat > .autobuild/config.yml <<'YAML'
model: claude-opus-4-8
max_parallel: 3
base_branch: main
max_iterations: 50
integration: branch
checks:
  - "echo checks-ok"
verify_checks: true
claude_cmd: claude
dangerously_bypass_permissions: true
require_sandbox_for_bypass: false
session_max_turns: 40
task_timeout_seconds: 1800
kill_grace_seconds: 10
YAML

# Four tasks: a root, two parallel dependents, and one gated behind a dependent.
rm -f tasks/*.md
write_task() { # id title priority depends_on
  cat > "tasks/$1.md" <<EOF
---
id: $1
title: $2
status: todo
priority: $3
depends_on: $4
---

## Goal
$2

## Acceptance criteria
- [ ] checks pass
EOF
}
write_task task-001 "db-schema"  1 "[]"
write_task task-002 "api-layer"  2 "[task-001]"
write_task task-003 "cli-flags"  2 "[task-001]"
write_task task-004 "docs"       3 "[task-002]"
