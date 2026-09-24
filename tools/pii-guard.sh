#!/usr/bin/env bash
# pii-guard.sh — blocks commits containing personal/private info
#
# IMPORTANT: This script contains NO personal data. The patterns it matches
# are loaded at runtime from a LOCAL, git-ignored file that never leaves the
# machine:
#     $PII_GUARD_PATTERNS   (default: ~/.config/pii-guard/patterns)
#
# That file holds one regex per line (blank lines and #comments ignored):
#     <personal email>
#     <username handle>
#     <wallet hex without 0x>
#     <real name>
# Create it once with mode 600. If it is missing, the guard prints a warning
# and does NOT block — so the guard can never itself become the leak.
#
# Also blocks hardcoded secret assignments (generic patterns, not personal),
# e.g.  PRIVATE_KEY = "long-real-looking-value".
#
# Exit 1 + print file:line on a match. Exit 0 if clean.

set -uo pipefail

PATTERNS_FILE="${PII_GUARD_PATTERNS:-$HOME/.config/pii-guard/patterns}"

# Generic secret-assignment detector (contains NO personal data).
SECRET_PATTERNS=(
  '(PRIVATE_KEY|SECRET_KEY|API_KEY|ETHERSCAN_KEY|APIKEY)\s*[=:]\s*["\047][A-Za-z0-9+/]{20,}'
)
SECRET_SKIP='(YOUR_|PASTE|PLACEHOLDER|EXAMPLE|TEST|DUMMY|<|xxx|\.\.\.|__|REPLACE_WITH)'

FAIL=0
RED='\033[0;31m'; YEL='\033[0;33m'; NC='\033[0m'
flag() { echo -e "${RED}[PII-GUARD BLOCKED]${NC} $1"; FAIL=1; }

# Load personal patterns from the local, git-ignored file.
PERSONAL_PATTERNS=()
if [[ -f "$PATTERNS_FILE" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    PERSONAL_PATTERNS+=("$line")
  done < "$PATTERNS_FILE"
else
  echo -e "${YEL}[pii-guard] No local patterns file at ${PATTERNS_FILE} — personal-data check skipped.${NC}" >&2
  echo -e "${YEL}[pii-guard] Create it (mode 600, one regex per line) to enable name/email/wallet blocking.${NC}" >&2
fi

staged_files() { git diff --cached --name-only --diff-filter=ACMR 2>/dev/null; }

check_file() {
  local path="$1" content
  content=$(git show ":${path}" 2>/dev/null) || return 0

  for pat in "${PERSONAL_PATTERNS[@]:-}"; do
    [[ -z "$pat" ]] && continue
    local hits; hits=$(echo "$content" | grep -nEi "$pat" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r l; do flag "${path}:${l}  <- personal info (local pattern)"; done <<< "$hits"
    fi
  done

  for pat in "${SECRET_PATTERNS[@]}"; do
    local hits; hits=$(echo "$content" | grep -nEi "$pat" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r l; do
        echo "$l" | grep -qEi "$SECRET_SKIP" && continue
        flag "${path}:${l}  <- hardcoded secret value"
      done <<< "$hits"
    fi
  done
}

git rev-parse --git-dir >/dev/null 2>&1 || { echo "[pii-guard] Not a git repo." >&2; exit 0; }
files=$(staged_files); [[ -z "$files" ]] && exit 0
while IFS= read -r f; do check_file "$f"; done <<< "$files"

if [[ $FAIL -ne 0 ]]; then
  echo ""; echo "  Commit blocked. Remove the flagged content and try again."; exit 1
fi
exit 0
