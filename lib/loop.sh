#!/usr/bin/env bash
# loop.sh — the outer Ralph-style loop, the reaper, status, and clean.

# ab_run — schedule -> spawn -> reap, repeating until the backlog is drained
# or max_iterations is hit. State is files+git, so this is crash-safe: kill it
# and re-run, it picks up where it left off.
ab_run() {
  require_init
  local max_par max_iter iter=0
  max_par="$(cfg max_parallel 3)"
  max_iter="$(cfg max_iterations 50)"
  : > "$AB_DIR/.running"

  log "starting loop (max_parallel=$max_par, max_iterations=$max_iter)"

  while :; do
    iter=$((iter+1))
    [ "$iter" -gt "$max_iter" ] && { warn "hit max_iterations ($max_iter); stopping"; break; }

    reap_stalled             # turn dead-without-result sessions into BLOCKED
    ab_reap_quiet            # collect anything that finished since last pass

    local active free
    active="$(running_count)"
    free=$(( max_par - active ))

    if [ "$free" -gt 0 ]; then
      local claimed; claimed="$(claim_tasks "$free" || true)"
      if [ -n "$claimed" ]; then
        while IFS= read -r tf; do [ -n "$tf" ] && spawn_session "$tf"; done <<< "$claimed"
      fi
    fi

    # Termination: nothing running AND no task left in a non-terminal state
    # (terminal = done | blocked). A stalled in-progress task keeps us looping
    # until reap_stalled resolves it or max_iterations trips.
    active="$(running_count)"
    if [ "$active" -eq 0 ] && [ "$(pending_count)" -eq 0 ]; then
      local blocked; blocked="$(count_status blocked)"
      if [ "$blocked" -gt 0 ]; then
        warn "backlog settled with $blocked blocked task(s) — see 'autobuild status'"
      else
        ok "backlog drained — COMPLETE"
      fi
      break
    fi

    sleep 2
  done
  ab_status
}

# count tasks whose status equals $1
count_status() {
  local want="$1" f n=0
  for f in "$TASKS_DIR"/*.md; do
    [ -e "$f" ] || continue
    [ "$(fm "$f" status)" = "$want" ] && n=$((n+1))
  done
  echo "$n"
}

# count tasks NOT in a terminal state (anything but done/blocked)
pending_count() {
  local f st n=0
  for f in "$TASKS_DIR"/*.md; do
    [ -e "$f" ] || continue
    case "$(fm "$f" status)" in done|blocked) ;; *) n=$((n+1)) ;; esac
  done
  echo "$n"
}

# any session whose process is gone but left no result.json gets a BLOCKED
# sentinel, so a crashed/killed agent can't stall the loop forever.
reap_stalled() {
  [ -f "$AB_DIR/.running" ] || return 0
  local sid pid sdir tid
  while IFS=: read -r sid pid; do
    [ -z "${sid:-}" ] && continue
    sdir="$SESSIONS_DIR/$sid"
    [ -d "$sdir" ] || continue
    [ -f "$sdir/result.json" ] && continue
    if ! kill -0 "$pid" 2>/dev/null; then
      tid="$(grep -E '"task"' "$sdir/meta.json" 2>/dev/null | head -1 | sed -E 's/.*"task"[^"]*"([^"]+)".*/\1/')"
      warn "session $sid exited without a result; marking $tid BLOCKED"
      cat > "$sdir/result.json" <<JSON
{ "task": "$tid", "status": "BLOCKED",
  "summary": "session process exited without writing result.json", "commit": "", "followups": [] }
JSON
    fi
  done < "$AB_DIR/.running"
}

# reap one finished session: read its result.json sentinel and act on it
_reap_session() {
  local sdir="$1" sid res status tid tf
  sid="$(basename "$sdir")"
  res="$sdir/result.json"
  [ -f "$res" ] || return 1                      # not finished yet
  tid="$(grep -E '"task"' "$res" | head -n1 | sed -E 's/.*"task"[^"]*"([^"]+)".*/\1/')"
  status="$(grep -E '"status"' "$res" | head -n1 | sed -E 's/.*"status"[^"]*"([^"]+)".*/\1/')"
  tf="$(grep -lR "id: $tid" "$TASKS_DIR" 2>/dev/null | head -n1)"

  case "$status" in
    COMPLETE)
      [ -n "$tf" ] && set_status "$tf" "done"
      ok "$sid: $tid COMPLETE"
      _integrate "$tid"
      _file_followups "$res"
      ;;
    BLOCKED)
      [ -n "$tf" ] && set_status "$tf" "blocked"
      warn "$sid: $tid BLOCKED — $(grep -E '"summary"' "$res" | sed -E 's/.*"summary"[^"]*"([^"]+)".*/\1/')"
      ;;
    NEEDS_HUMAN)
      [ -n "$tf" ] && set_status "$tf" "blocked"
      warn "$sid: $tid NEEDS_HUMAN — see $res"
      ;;
    *) warn "$sid: unrecognized status '$status'"; return 1 ;;
  esac

  remove_worktree "$sid"
  # drop it from the running list
  if [ -f "$AB_DIR/.running" ]; then
    grep -v "^$sid:" "$AB_DIR/.running" > "$AB_DIR/.running.tmp" 2>/dev/null || true
    mv "$AB_DIR/.running.tmp" "$AB_DIR/.running" 2>/dev/null || true
  fi
}

# integrate a finished branch per config: pr | auto-merge | branch
_integrate() {
  local tid="$1" mode base branch
  mode="$(cfg integration pr)"
  base="$(cfg base_branch main)"
  branch="autobuild/$tid"
  case "$mode" in
    pr)
      if command -v gh >/dev/null 2>&1; then
        git -C "$PROJECT_ROOT" push -u origin "$branch" >/dev/null 2>&1 || true
        gh pr create --head "$branch" --base "$base" \
          --title "autobuild: $tid" --body "Automated by autobuild for $tid." >/dev/null 2>&1 \
          && ok "opened PR for $branch" || warn "PR creation skipped/failed for $branch"
      else
        warn "integration=pr but 'gh' not found; left branch $branch for manual PR"
      fi
      ;;
    auto-merge)
      git -C "$PROJECT_ROOT" merge --no-ff -m "autobuild: merge $tid" "$branch" \
        && ok "merged $branch into $base" || warn "auto-merge failed for $branch (conflict?)"
      ;;
    branch) log "left branch $branch for manual merge" ;;
    *) warn "unknown integration mode '$mode'" ;;
  esac
}

# create tasks/ files for any followups[] entries in a result.json
_file_followups() {
  local res="$1" titles n next
  # crude extraction of followup titles; TODO: use a JSON parser if available
  titles="$(grep -oE '"title"[^"]*"[^"]+"' "$res" | sed -E 's/.*"title"[^"]*"([^"]+)"/\1/')"
  [ -z "$titles" ] && return 0
  n="$(ls "$TASKS_DIR"/task-*.md 2>/dev/null | sed -E 's/.*task-0*([0-9]+).*/\1/' | sort -n | tail -n1)"
  n="${n:-0}"
  while IFS= read -r title; do
    [ -z "$title" ] && continue
    n=$((n+1)); next="$(printf 'task-%03d' "$n")"
    local slug; slug="$(echo "$title" | tr '[:upper:] ' '[:lower:]-' | tr -cd 'a-z0-9-' | cut -c1-40)"
    cat > "$TASKS_DIR/${next}-${slug}.md" <<EOF
---
id: $next
title: $title
status: todo
priority: 3
depends_on: []
---

## Goal
$title

## Acceptance criteria
- [ ] (filed automatically by autobuild as a follow-up)

## Notes
Auto-generated from a session follow-up.
EOF
    ok "filed follow-up $next: $title"
  done <<< "$titles"
}

ab_reap() { require_init; _reap_all; }
ab_reap_quiet() { _reap_all >/dev/null 2>&1 || true; }
_reap_all() {
  local sdir
  for sdir in "$SESSIONS_DIR"/*/; do
    [ -d "$sdir" ] || continue
    [ -f "$sdir/result.json" ] || continue
    # skip ones already reaped (worktree gone + task not in-progress)
    _reap_session "$sdir" || true
  done
}

ab_status() {
  require_init
  printf '\n%s\n' "$(_c '1;37')TASKS$(_c 0)"
  local f id st pr
  for f in "$TASKS_DIR"/*.md; do
    [ -e "$f" ] || continue
    id="$(fm "$f" id)"; st="$(fm "$f" status)"; pr="$(fm "$f" priority)"
    printf '  %-10s p%-2s %-12s %s\n' "$id" "$pr" "$st" "$(fm "$f" title)"
  done
  printf '\n%s\n' "$(_c '1;37')SESSIONS$(_c 0)"
  local s
  for s in "$SESSIONS_DIR"/*/; do
    [ -d "$s" ] || continue
    local sid done
    sid="$(basename "$s")"
    done="pending"; [ -f "$s/result.json" ] && done="$(grep -E '"status"' "$s/result.json" | head -n1 | sed -E 's/.*"status"[^"]*"([^"]+)".*/\1/')"
    printf '  %-32s %s\n' "$sid" "$done"
  done
  echo
}

ab_clean() {
  require_init
  log "cleaning finished worktrees and reaped sessions"
  git -C "$PROJECT_ROOT" worktree prune
  local s
  for s in "$SESSIONS_DIR"/*/; do
    [ -d "$s" ] || continue
    [ -f "$s/result.json" ] && rm -rf "$s"
  done
  : > "$AB_DIR/.running"
  ok "clean"
}
