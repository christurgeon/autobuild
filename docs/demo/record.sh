#!/usr/bin/env bash
# Render docs/demo.gif: a real, token-free autobuild run recorded against the
# repo's stub `claude`. Requires `autobuild` on PATH plus `asciinema` and `agg`.
#
#   uv tool install .            # autobuild on PATH
#   uv tool install asciinema
#   # agg: https://github.com/asciinema/agg/releases  (single static binary)
#   docs/demo/record.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$REPO/docs/demo.gif"
CAST="$(mktemp --suffix=.cast)"
DEMO="$(mktemp -d)"
DRIVER="$(mktemp --suffix=.sh)"

bash "$REPO/docs/demo/seed.sh" "$DEMO" "$REPO/tests/fixtures/claude"

# The script asciinema records: type a command, then run it.
cat > "$DRIVER" <<DRIVER
#!/usr/bin/env bash
export PATH="$DEMO/bin:\$PATH"
cd "$DEMO"
export AUTOBUILD_SANDBOX=1            # silence the un-sandboxed-bypass warning for the demo
export STUB_STATUS_task_002=BLOCKED   # api-layer blocks -> docs stays gated
export STUB_SLEEP=1.0                 # visible pacing so parallelism is legible
type_cmd() {  # echo a prompt + command with a typing animation, then run it
  printf '\033[1;32m\$\033[0m '
  for ((i=0; i<\${#1}; i++)); do printf '%s' "\${1:i:1}"; sleep 0.04; done
  printf '\n'; sleep 0.4; eval "\$1"
}
sleep 0.6
type_cmd "autobuild run"
sleep 1.0
type_cmd "autobuild status"
sleep 2.5
DRIVER
chmod +x "$DRIVER"

asciinema rec --overwrite -c "bash $DRIVER" "$CAST"

agg --theme asciinema --font-size 15 --cols 92 --rows 34 "$CAST" "$OUT"
echo "wrote $OUT"
rm -rf "$DEMO" "$DRIVER" "$CAST"
