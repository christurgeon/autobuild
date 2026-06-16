#!/usr/bin/env bash
# common.sh — shared paths, logging, config + frontmatter parsing, and `init`.

# --- logging -----------------------------------------------------------------
_c() { printf '\033[%sm' "$1"; }
log()  { printf '%s %s\n' "$(_c '1;34')[autobuild]$(_c 0)" "$*"; }
ok()   { printf '%s %s\n' "$(_c '1;32')[ ok ]$(_c 0)" "$*"; }
warn() { printf '%s %s\n' "$(_c '1;33')[warn]$(_c 0)" "$*" >&2; }
err()  { printf '%s %s\n' "$(_c '1;31')[fail]$(_c 0)" "$*" >&2; }

# --- project paths -----------------------------------------------------------
# All commands run from the target project root. These resolve relative to $PWD.
PROJECT_ROOT="$(pwd)"
AB_DIR="$PROJECT_ROOT/.autobuild"
TASKS_DIR="$PROJECT_ROOT/tasks"
SESSIONS_DIR="$AB_DIR/sessions"
WORKTREES_DIR="$AB_DIR/worktrees"
LOCK_FILE="$AB_DIR/backlog.lock"
CONFIG_FILE="$AB_DIR/config.yml"

# --- tiny YAML / frontmatter readers -----------------------------------------
# Deliberately minimal: scalar `key: value` lookups only. Good enough for the
# flat config and task frontmatter we control. TODO: swap for `yq` if available.

# cfg KEY [default] — read a top-level scalar from config.yml
cfg() {
  local key="$1" default="${2:-}" val
  val="$(grep -E "^${key}:" "$CONFIG_FILE" 2>/dev/null | head -n1 \
        | sed -E "s/^${key}:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^\"//; s/\"$//")"
  [ -n "$val" ] && printf '%s' "$val" || printf '%s' "$default"
}

# cfg_list KEY — read a YAML block list (lines like `  - item`) under KEY
cfg_list() {
  awk -v key="$1" '
    $0 ~ "^"key":" { grab=1; next }
    grab && /^[[:space:]]+-/ { sub(/^[[:space:]]*-[[:space:]]*/,""); gsub(/^"|"$/,""); print; next }
    grab && /^[^[:space:]]/ { grab=0 }
  ' "$CONFIG_FILE"
}

# fm FILE KEY — read a frontmatter scalar from a task file's `--- ... ---` block
fm() {
  local file="$1" key="$2"
  awk -v key="$key" '
    NR==1 && $0=="---" { infm=1; next }
    infm && $0=="---" { exit }
    infm && $0 ~ "^"key":" {
      sub("^"key":[[:space:]]*",""); sub(/[[:space:]]*#.*$/,""); gsub(/^"|"$/,""); print; exit
    }
  ' "$file"
}

# set_status FILE STATUS — rewrite the `status:` frontmatter line in place
set_status() {
  local file="$1" status="$2" tmp
  tmp="$(mktemp)"
  awk -v s="$status" '
    NR==1 && $0=="---" { infm=1; print; next }
    infm && $0=="---" { infm=0; print; next }
    infm && /^status:/ { print "status: " s "          # set by autobuild"; next }
    { print }
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

# --- init --------------------------------------------------------------------
ab_init() {
  local src="${AUTOBUILD_HOME}/templates"
  log "initializing autobuild in $PROJECT_ROOT"
  [ -f "$PROJECT_ROOT/GOAL.md" ]  || cp "$src/GOAL.md"  "$PROJECT_ROOT/GOAL.md"
  [ -f "$PROJECT_ROOT/CLAUDE.md" ]|| cp "$src/CLAUDE.md" "$PROJECT_ROOT/CLAUDE.md"
  mkdir -p "$TASKS_DIR"
  [ -n "$(ls -A "$TASKS_DIR" 2>/dev/null)" ] || cp "$src/tasks/"*.md "$TASKS_DIR/"
  mkdir -p "$AB_DIR" "$SESSIONS_DIR" "$WORKTREES_DIR"
  [ -f "$CONFIG_FILE" ] || cp "$src/.autobuild/config.yml" "$CONFIG_FILE"
  ok "ready. Edit GOAL.md and tasks/, then run: autobuild run"
}

# require we're inside an initialized project
require_init() {
  [ -f "$CONFIG_FILE" ] || { err "no .autobuild/config.yml — run 'autobuild init' first"; exit 1; }
  [ -d "$TASKS_DIR" ]   || { err "no tasks/ directory — run 'autobuild init' first"; exit 1; }
  mkdir -p "$SESSIONS_DIR" "$WORKTREES_DIR"
}

new_session_id() { printf 'sess-%s-%s' "$(date +%Y%m%d-%H%M%S)" "$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"; }
