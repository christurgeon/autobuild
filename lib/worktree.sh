#!/usr/bin/env bash
# worktree.sh — give each session its own isolated git checkout + branch.

# make_worktree SESSION_ID TASK_ID -> echoes the worktree path
make_worktree() {
  local sid="$1" tid="$2" base path branch
  base="$(cfg base_branch main)"
  branch="autobuild/${tid}"
  path="$WORKTREES_DIR/$sid"

  # Branch from the current tip of base. If the branch already exists (retry),
  # reuse it rather than failing.
  # `if` suppresses set -e, so check git's exit code explicitly and fail loudly.
  if git -C "$PROJECT_ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$PROJECT_ROOT" worktree add -q "$path" "$branch" || return 1
  else
    git -C "$PROJECT_ROOT" worktree add -q -b "$branch" "$path" "$base" || return 1
  fi
  printf '%s' "$path"
}

# remove_worktree SESSION_ID — detach the worktree (keeps the branch + commits)
remove_worktree() {
  local sid="$1" path="$WORKTREES_DIR/$1"
  [ -d "$path" ] || return 0
  git -C "$PROJECT_ROOT" worktree remove --force "$path" 2>/dev/null \
    || rm -rf "$path"
}
