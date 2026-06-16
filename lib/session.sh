#!/usr/bin/env bash
# session.sh — spawn one fresh Claude session in a worktree, in the background.

# spawn_session TASK_FILE
# Creates a session dir + worktree, writes meta.json, and launches `claude -p`
# in the background. The session writes plan.md / progress.log / result.json
# into its session dir per the CLAUDE.md contract.
spawn_session() {
  local task_file="$1"
  local tid sid sdir wt model claude_bin prompt
  tid="$(fm "$task_file" id)"
  sid="$(new_session_id)"
  sdir="$SESSIONS_DIR/$sid"
  mkdir -p "$sdir"

  model="$(cfg model claude-opus-4-8)"
  claude_bin="$(cfg claude_cmd claude)"

  if ! wt="$(make_worktree "$sid" "$tid")"; then
    err "could not create worktree for $tid (does base_branch '$(cfg base_branch main)' exist?)"
    set_status "$task_file" "blocked"
    cat > "$sdir/result.json" <<JSON
{ "task": "$tid", "status": "BLOCKED",
  "summary": "worktree creation failed; check base_branch in config.yml", "commit": "", "followups": [] }
JSON
    return 0
  fi

  cat > "$sdir/meta.json" <<EOF
{
  "session": "$sid",
  "task": "$tid",
  "task_file": "$task_file",
  "worktree": "$wt",
  "branch": "autobuild/$tid",
  "status": "in-progress",
  "started": "$(date -u +%FT%TZ)"
}
EOF
  set_status "$task_file" "in-progress"

  prompt="$(cat <<EOF
You are an autobuild session. Your session directory is: $sdir
Your assigned task file is: $task_file
You are working inside an isolated git worktree at: $wt

Read GOAL.md and CLAUDE.md in this worktree for your contract, then follow
plan -> review -> implement and finish by writing $sdir/result.json.
Work ONLY on task $tid. Do everything from within $wt.
EOF
)"

  log "spawn $sid -> $tid (worktree: ${wt#$PROJECT_ROOT/})"

  # Fresh context every call — this is the Ralph property. Background it so the
  # outer loop can run up to max_parallel of these at once.
  # TODO: wire real flags/permission mode for your environment.
  (
    cd "$wt" || exit 1
    if command -v "$claude_bin" >/dev/null 2>&1; then
      "$claude_bin" -p "$prompt" --model "$model" \
        > "$sdir/session.out" 2> "$sdir/session.err" || true
    else
      # No Claude CLI on PATH (e.g. CI/dev box): emit a NEEDS_HUMAN sentinel so
      # the reaper has something deterministic to act on instead of hanging.
      warn "claude binary '$claude_bin' not found; writing NEEDS_HUMAN sentinel"
      cat > "$sdir/result.json" <<JSON
{ "task": "$tid", "status": "NEEDS_HUMAN",
  "summary": "claude CLI not found on PATH; cannot run session", "commit": "", "followups": [] }
JSON
    fi
  ) &

  echo "$sid:$!" >> "$AB_DIR/.running"
}

# count currently-running background sessions
running_count() {
  [ -f "$AB_DIR/.running" ] || { echo 0; return; }
  local n=0 line pid
  while IFS=: read -r _ pid; do
    [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null && n=$((n+1))
  done < "$AB_DIR/.running"
  echo "$n"
}
