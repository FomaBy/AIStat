#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
gitleaks_bin="${GITLEAKS_BIN:-gitleaks}"
probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/aistat-gitleaks.XXXXXX")"
probe_repo="$probe_dir/repository"

cleanup() {
  rm -rf "$probe_dir"
}
trap cleanup EXIT

fail() {
  printf '%s\n' "gitleaks history self-test failed: $1" >&2
  exit 1
}

scan() {
  "$gitleaks_bin" detect \
    --source "$probe_repo" \
    --config "$repo_root/.gitleaks.toml" \
    --redact \
    --exit-code 1 \
    --no-banner \
    >/dev/null 2>&1
}

commit() {
  git -C "$probe_repo" add --all
  git -C "$probe_repo" commit --quiet -m "$1"
}

"$gitleaks_bin" version >/dev/null
git init --quiet "$probe_repo"
git -C "$probe_repo" config user.email "gitleaks-test@example.invalid"
git -C "$probe_repo" config user.name "AIStat gitleaks test"

printf '%s\n' "clean history" > "$probe_repo/README.md"
commit "clean history"
scan || fail "clean history must pass"

cp "$repo_root/.env.example" "$probe_repo/.env.example"
commit "documented placeholder"
scan || fail "the documented .env.example placeholder must pass"

synthetic_token="ghp_$(python3 -c 'import hashlib; print(hashlib.sha256(b"AIStat gitleaks self-test").hexdigest()[:36])')"
printf 'token=%s\n' "$synthetic_token" > "$probe_repo/current.py"
commit "synthetic current finding"
if scan; then
  fail "an out-of-allowlist current file must fail"
fi

git -C "$probe_repo" rm --quiet current.py
commit "synthetic deleted finding"
if scan; then
  fail "an out-of-allowlist deleted file must fail"
fi

printf '%s\n' "gitleaks history self-test: pass"
