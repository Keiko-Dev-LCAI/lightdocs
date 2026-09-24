#!/usr/bin/env bash
# pii-guard.sh — blocks commits containing personal/private info
#
# Scans STAGED content for:
#   • personal email and username
#   • excluded personal wallet addresses
#   • hardcoded secret key values (not placeholders)
#
# Exit 1 and print file:line if anything matches. Exit 0 if clean.
#
# Usage (standalone test):
#   bash tools/pii-guard.sh
#
# Installed as pre-commit hook via install-pii-guard.sh

set -uo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
# If you have a real name to block, set it here (case-insensitive grep).
# Leave blank to skip the real-name check.
REAL_NAME=""

# Personal email and username handle
EMAIL_PATTERNS=(
  'REDACTED@gmail\.com'
  '\bREDACTED\b'
)

# Personal/excluded wallet addresses (matched case-insensitively)
WALLET_PATTERNS=(
  '69DEd8cFe0a9b5e8c00cAb7EEb058959A23D7156'
  'a3a653a8cba0710ff57ac34e2278c603b4259fd3'
  '1F899FaD2C8BD70b6eF356ae6cC3c0abDbB15EB5'
  '6518fD26a7aD2Fe1bA80De5f279Ee59F55C0A9bA'
)

# Hardcoded secret patterns: assignment to a real-looking value (not a placeholder).
# Matches lines like: ETHERSCAN_KEY = "ABC123..." or PRIVATE_KEY='0x...'
# Skips obvious placeholders: YOUR_KEY, PASTE_HERE, <...>, example, test, dummy, xxx
SECRET_PATTERNS=(
  '(PRIVATE_KEY|SECRET_KEY|API_KEY|ETHERSCAN_KEY|APIKEY)\s*[=:]\s*["\047][A-Za-z0-9+/]{20,}'
)
SECRET_SKIP='(YOUR_|PASTE|PLACEHOLDER|EXAMPLE|TEST|DUMMY|<|xxx|\.\.\.|__)'

# ── Helpers ────────────────────────────────────────────────────────────────
FAIL=0
RED='\033[0;31m'
NC='\033[0m'

flag() {
  echo -e "${RED}[PII-GUARD BLOCKED]${NC} $1"
  FAIL=1
}

# Get staged diff as text with file:line context
# We use `git diff --cached -U0` and parse it to map to file:line.
# For simplicity, use git diff --cached --name-only + git show :file for each.
staged_files() {
  git diff --cached --name-only --diff-filter=ACMR 2>/dev/null
}

check_file() {
  local path="$1"
  # Read staged content (not working tree) via git show
  local content
  content=$(git show ":${path}" 2>/dev/null) || return 0

  # Email patterns
  for pat in "${EMAIL_PATTERNS[@]}"; do
    local hits
    hits=$(echo "$content" | grep -nEi "$pat" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r line; do
        flag "${path}:${line}  ← personal email/username"
      done <<< "$hits"
    fi
  done

  # Real name (only if configured)
  if [[ -n "$REAL_NAME" ]]; then
    local hits
    hits=$(echo "$content" | grep -nFi "$REAL_NAME" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r line; do
        flag "${path}:${line}  ← real name"
      done <<< "$hits"
    fi
  fi

  # Wallet addresses
  for wallet in "${WALLET_PATTERNS[@]}"; do
    local hits
    hits=$(echo "$content" | grep -ni "$wallet" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r line; do
        flag "${path}:${line}  ← excluded wallet address"
      done <<< "$hits"
    fi
  done

  # Hardcoded secrets
  for pat in "${SECRET_PATTERNS[@]}"; do
    local hits
    hits=$(echo "$content" | grep -nEi "$pat" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      while IFS= read -r line; do
        # Skip if line looks like a placeholder
        if echo "$line" | grep -qEi "$SECRET_SKIP"; then
          continue
        fi
        flag "${path}:${line}  ← hardcoded secret value"
      done <<< "$hits"
    fi
  done
}

# ── Main ───────────────────────────────────────────────────────────────────
# Check we're inside a git repo
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[pii-guard] Not a git repo — nothing to check." >&2
  exit 0
fi

files=$(staged_files)
if [[ -z "$files" ]]; then
  exit 0
fi

while IFS= read -r f; do
  check_file "$f"
done <<< "$files"

if [[ $FAIL -ne 0 ]]; then
  echo ""
  echo "  Commit blocked. Remove the flagged content and try again."
  echo "  If this is a known-safe value, see tools/pii-guard.sh to adjust patterns."
  exit 1
fi

exit 0
