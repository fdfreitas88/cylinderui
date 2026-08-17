#!/usr/bin/env bash
#
# secret-scan.sh — CylinderUI publication gate
# ---------------------------------------------------------------------------
# Regex scan for secrets, private IPs, personal paths, identity and personal
# branding leaks over all material that ships to the public repo. Designed to
# run in CI: it FAILS (exit 1) only on *real* leaks and prints, separately and
# without failing, the known-technical identifiers that are allowed to remain
# (theme id "cylinderui", the internal interface enum cylinderui/cyber/god, the JS
# mode keys godmode/cyber, storage/event keys, etc.).
#
# Usage:
#   ./secret-scan.sh [dir ...]        # defaults to the CylinderUI publish set
#
# Output per finding: <file>:<line>:<CATEGORY>:<redacted snippet ≤60 chars>
#
# FAIL categories (any hit => gate fails): SECRET IP_CRIT IP_PRIV PATH IDENT BRAND_HARD
# INFO category  (never fails, review only): BRAND_TECH
#
# Ignored: .git, __pycache__, .pytest_cache, node_modules, */tests/* (test code
# and fixtures, incl. the anti-leak guard test that intentionally lists the
# forbidden terms), *.bak*, *.pyc, secret-scan.sh and SCAN.md themselves.

set -u

# ---------------------------------------------------------------------------
# Targets (override by passing directories as arguments)
# ---------------------------------------------------------------------------
DEFAULT_TARGETS=(
  "repo"
  "../cylinderui-dist"
  "../cylinderui-scripts"
)
if [ "$#" -gt 0 ]; then TARGETS=("$@"); else TARGETS=("${DEFAULT_TARGETS[@]}"); fi

# ---------------------------------------------------------------------------
# Patterns:  TIER|CATEGORY|extended-regex
#   TIER = FAIL (counts toward gate failure) | INFO (reported, never fails)
# ---------------------------------------------------------------------------
PATTERNS=(
  # 1. Secrets
  'FAIL|SECRET|(api[_-]?key|token|secret|password|passwd|bearer)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9]'
  'FAIL|SECRET|sk-[A-Za-z0-9]{16,}'
  'FAIL|SECRET|ghp_[A-Za-z0-9]{20,}'
  'FAIL|SECRET|AKIA[0-9A-Z]{12,}'
  'FAIL|SECRET|-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'FAIL|SECRET|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.'
  # 2. Private / personal IPs (127.0.0.1, 0.0.0.0, localhost are allowed)
  'FAIL|IP_CRIT|10\.73\.254\.11'
  'FAIL|IP_PRIV|(^|[^0-9.])10\.([0-9]{1,3}\.){2}[0-9]{1,3}'
  'FAIL|IP_PRIV|(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}'
  'FAIL|IP_PRIV|(^|[^0-9.])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}'
  # 3. Personal paths
  'FAIL|PATH|/Volumes/musik'
  'FAIL|PATH|/Users/felipefreitas'
  'FAIL|PATH|/Users/musik'
  'FAIL|PATH|macpro'
  # 4. Identity
  'FAIL|IDENT|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
  'FAIL|IDENT|felipe'
  'FAIL|IDENT|freitas'
  'FAIL|IDENT|musik'
  # 5a. Personal branding / prompts — HARD (never legitimately technical)
  'FAIL|BRAND_HARD|van ?damme'
  'FAIL|BRAND_HARD|god mode'
  'FAIL|BRAND_HARD|jailbreak'
  'FAIL|BRAND_HARD|red[ -]team'
  'FAIL|BRAND_HARD|offsec'
  'FAIL|BRAND_HARD|irrestrito'
  'FAIL|BRAND_HARD|baronllm'
  'FAIL|BRAND_HARD|cl[aá]udio'
  'FAIL|BRAND_HARD|olá mestre'
  # 5b. Technical identifiers derived from the old brand — INFO (allowed to stay)
  #     theme id / storage keys / interface enum / JS mode keys.
  'INFO|BRAND_TECH|godmode'
  'INFO|BRAND_TECH|(^|[^a-z])cyber'
  'INFO|BRAND_TECH|(^|[^a-z])god([^a-z]|$)'
)

# ---------------------------------------------------------------------------
# Allowlist:  CATEGORY|file-path-regex|line-regex
#   A FAIL hit whose file AND line both match an allowlist entry of the same
#   category is downgraded to ALLOW (reported for review, never fails the gate).
#   Entries are PRECISE: they describe legitimate, non-secret constructs — not a
#   blanket relaxation of the secret patterns.
# ---------------------------------------------------------------------------
ALLOWLIST=(
  # Public repository URL required by the publishing guide. This permits only
  # the exact GitHub destination, not the account identifier elsewhere.
  'IDENT|PUBLISH\.md|https://github\.com/fdfreitas88/cylinderui(\.git)?'
  'IDENT|cylinderui-apresentacao\.html|https://github\.com/fdfreitas88/cylinderui(\.git)?'
  'IDENT|README\.md|https://github\.com/fdfreitas88/cylinderui(\.git)?'
  'IDENT|README\.md|https://fdfreitas88\.github\.io/cylinderui/'
  # (a) Auth wiring in the router: reading an environment variable whose name
  #     ends in _TOKEN. The value is supplied by the runtime environment, so the
  #     source line never contains a literal secret (matches os.environ.get /
  #     os.getenv / environ[...] with a *_TOKEN key). A real leak (token = "sk-…")
  #     has no env read and is NOT matched here.
  'SECRET|router/router\.py|(os\.environ\.get|os\.getenv|environ\[)[^)]*_TOKEN'
  # (b) Prompt-injection guard in the router: jailbreak / injection / red-team
  #     keywords listed INSIDE a detection regex (re.search|match|compile|
  #     fullmatch). These classify hostile input — a product feature — not a
  #     personal "god mode / jailbreak" branding leak. Scoped to router.py and to
  #     lines that are actually a regex, so real BRAND_HARD leaks elsewhere still fail.
  'BRAND_HARD|router/router\.py|re\.(search|match|compile|fullmatch)\(.*(JAILBREAK|INJECTION|RED[ -]?TEAM)'
)

is_allowlisted() {
  local f="$1" cat="$2" raw="$3" a acat afile aline
  for a in "${ALLOWLIST[@]}"; do
    acat="${a%%|*}"; a="${a#*|}"
    afile="${a%%|*}"; aline="${a#*|}"
    [ "$cat" = "$acat" ] || continue
    printf '%s' "$f"   | grep -qE  "$afile" || continue
    printf '%s' "$raw" | grep -qiE "$aline" || continue
    return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# Redact a matched line: strip leading ws, mask secret tails, truncate to 60.
# ---------------------------------------------------------------------------
redact() {
  local s
  s=$(printf '%s' "$1" | sed -E 's/^[[:space:]]+//')
  s=$(printf '%s' "$s" | sed -E \
    -e 's/((api[_-]?key|token|secret|password|passwd|bearer)[[:space:]]*[:=][[:space:]]*)[A-Za-z0-9._\/+-]{4,}/\1<REDACTED>/Ig' \
    -e 's/(sk-|ghp_|AKIA|eyJ)[A-Za-z0-9._-]{6,}/\1<REDACTED>/g' \
    -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----.*/-----BEGIN PRIVATE KEY----- <REDACTED>/g')
  printf '%.60s' "$s"
}

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
FAIL_TOTAL=0
INFO_TOTAL=0
ALLOW_TOTAL=0
SECRET_COUNT=0
IP_CRIT_COUNT=0
IP_PRIV_COUNT=0
PATH_COUNT=0
IDENT_COUNT=0
BRAND_HARD_COUNT=0
BRAND_TECH_COUNT=0

increment_category() {
  case "$1" in
    SECRET) SECRET_COUNT=$((SECRET_COUNT+1)) ;;
    IP_CRIT) IP_CRIT_COUNT=$((IP_CRIT_COUNT+1)) ;;
    IP_PRIV) IP_PRIV_COUNT=$((IP_PRIV_COUNT+1)) ;;
    PATH) PATH_COUNT=$((PATH_COUNT+1)) ;;
    IDENT) IDENT_COUNT=$((IDENT_COUNT+1)) ;;
    BRAND_HARD) BRAND_HARD_COUNT=$((BRAND_HARD_COUNT+1)) ;;
    BRAND_TECH) BRAND_TECH_COUNT=$((BRAND_TECH_COUNT+1)) ;;
  esac
}

scan_file() {
  local f="$1" entry tier cat rx rest line lineno snippet raw
  for entry in "${PATTERNS[@]}"; do
    tier="${entry%%|*}"; rest="${entry#*|}"
    cat="${rest%%|*}"; rx="${rest#*|}"
    while IFS= read -r line; do
      lineno="${line%%:*}"
      raw="${line#*:}"
      snippet=$(redact "$raw")
      if [ "$tier" = "FAIL" ] && is_allowlisted "$f" "$cat" "$raw"; then
        printf '%s:%s:%s(allow):%s\n' "$f" "$lineno" "$cat" "$snippet"
        ALLOW_TOTAL=$((ALLOW_TOTAL+1))
        continue
      fi
      printf '%s:%s:%s:%s\n' "$f" "$lineno" "$cat" "$snippet"
      increment_category "$cat"
      if [ "$tier" = "FAIL" ]; then FAIL_TOTAL=$((FAIL_TOTAL+1)); else INFO_TOTAL=$((INFO_TOTAL+1)); fi
    done < <(grep -niEI "$rx" "$f" 2>/dev/null)
  done
}

for t in "${TARGETS[@]}"; do
  [ -e "$t" ] || { echo "warn: target not found: $t" >&2; continue; }
  while IFS= read -r f; do
    scan_file "$f"
  done < <(find "$t" -type f \
      -not -path '*/.git/*' \
      -not -path '*/__pycache__/*' \
      -not -path '*/.pytest_cache/*' \
      -not -path '*/node_modules/*' \
      -not -path '*/tests/*' \
      -not -name '*.bak' \
      -not -name '*.bak.*' \
      -not -name '*.pyc' \
      -not -name 'secret-scan.sh' \
      -not -name 'SCAN.md' 2>/dev/null | sort)
done

echo "----------------------------------------------------------------------"
echo "FAIL categories (real leaks — must be 0):"
printf '  %-11s %s\n' "SECRET" "$SECRET_COUNT"
printf '  %-11s %s\n' "IP_CRIT" "$IP_CRIT_COUNT"
printf '  %-11s %s\n' "IP_PRIV" "$IP_PRIV_COUNT"
printf '  %-11s %s\n' "PATH" "$PATH_COUNT"
printf '  %-11s %s\n' "IDENT" "$IDENT_COUNT"
printf '  %-11s %s\n' "BRAND_HARD" "$BRAND_HARD_COUNT"
echo "INFO category (documented technical identifiers — allowed):"
printf '  %-11s %s\n' "BRAND_TECH" "$BRAND_TECH_COUNT"
echo "ALLOW (precise false-positive exceptions — reviewed, never fails):"
printf '  %-11s %s\n' "ALLOW" "$ALLOW_TOTAL"
echo "----------------------------------------------------------------------"
echo "REAL hit total: $FAIL_TOTAL   (technical/info: $INFO_TOTAL, allowlisted: $ALLOW_TOTAL)"

if [ "$FAIL_TOTAL" -eq 0 ]; then
  echo "GATE: PASS (0 real hits)"
  exit 0
else
  echo "GATE: FAIL ($FAIL_TOTAL real hits)"
  exit 1
fi
