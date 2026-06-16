#!/usr/bin/env bash
# scheduler.sh — choose the next runnable tasks and claim them atomically.

# A task is runnable if status==todo AND every id in depends_on is `done`.
task_is_done() {
  local id="$1" f
  for f in "$TASKS_DIR"/*.md; do
    [ -e "$f" ] || continue
    if [ "$(fm "$f" id)" = "$id" ]; then
      [ "$(fm "$f" status)" = "done" ]; return
    fi
  done
  return 1
}

deps_satisfied() {
  local file="$1" deps dep
  deps="$(fm "$file" depends_on | tr -d '[]' | tr ',' ' ')"
  for dep in $deps; do
    [ -z "$dep" ] && continue
    task_is_done "$dep" || return 1
  done
  return 0
}

# print runnable task files, highest priority first (lower number wins)
runnable_tasks() {
  local f
  for f in "$TASKS_DIR"/*.md; do
    [ -e "$f" ] || continue
    [ "$(fm "$f" status)" = "todo" ] || continue
    deps_satisfied "$f" || continue
    printf '%s\t%s\n' "$(fm "$f" priority)" "$f"
  done | sort -n | cut -f2-
}

# claim_tasks N — atomically flip up to N runnable tasks todo->claimed.
# Echoes the claimed file paths. Uses a lock dir so parallel `run`s don't double-claim.
claim_tasks() {
  local want="$1" claimed=0 f
  # mkdir is atomic across processes; spin briefly if another claimer holds it.
  local tries=0
  until mkdir "$LOCK_FILE" 2>/dev/null; do
    tries=$((tries+1)); [ "$tries" -gt 50 ] && { warn "could not acquire backlog.lock"; return 1; }
    sleep 0.1
  done
  trap 'rmdir "$LOCK_FILE" 2>/dev/null || true' RETURN

  while IFS= read -r f; do
    [ -z "$f" ] && continue
    [ "$claimed" -ge "$want" ] && break
    set_status "$f" "claimed"
    printf '%s\n' "$f"
    claimed=$((claimed+1))
  done < <(runnable_tasks)
}
