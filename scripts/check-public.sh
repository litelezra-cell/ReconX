#!/usr/bin/env bash
# Public-release gate: fails if any tracked file contains a string from the local deny-list.
# The deny-list lives in the machine-local Recon config (public_denylist), never in this repo —
# a list of private names inside a public repo would itself be the leak.
# Usage: scripts/check-public.sh [path …]   (default: every tracked file; exit 0 clean · 1 leaks · 2 no deny-list)
# Matching is case-sensitive and fixed-string, so list each spelling you mean ("Acme" and "acme").
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${RECON_CONFIG:-$HOME/.claude/recon-local/config.json}"
PATTERNS="$(mktemp)"
trap 'rm -f "$PATTERNS"' EXIT

python3 - "$CONFIG" > "$PATTERNS" <<'PY'
import json, sys
try:
    words = json.load(open(sys.argv[1], encoding="utf-8")).get("public_denylist", [])
except FileNotFoundError:
    words = []
print("\n".join(w for w in words if w.strip()))
PY
if [ ! -s "$PATTERNS" ]; then
  echo "no public_denylist in $CONFIG — cannot check (refusing to call the repo clean)" >&2
  exit 2
fi

cd "$ROOT"
hits="$(git ls-files -z -- "$@" | xargs -0 grep -I -n -F -f "$PATTERNS" -- 2>/dev/null || true)"
if [ -z "$hits" ]; then
  echo "clean: no deny-listed string in $(git ls-files -- "$@" | wc -l | tr -d ' ') tracked files"
  echo "note: git history still holds earlier versions; publish from a fresh history, not by flipping this repo public."
  exit 0
fi
echo "LEAKS by folder (files · lines):"
printf '%s\n' "$hits" | awk -F: '{split($1,p,"/"); d=((p[1]=="toolbox"||p[1]=="skills") && p[2]!="")?p[1]"/"p[2]:p[1]; if(!((d,$1) in seen)){seen[d,$1]=1; files[d]++} n[d]++} END{for(d in n) printf "  %-32s %4d · %d\n", d, files[d], n[d]}' | sort
echo "first lines:"; printf '%s\n' "$hits" | cut -c1-160 | head -15
exit 1
