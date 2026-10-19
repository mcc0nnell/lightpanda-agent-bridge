#!/usr/bin/env bash
# Fetch the exact successful Lightpanda PR #4023 merge build. Requires GitHub CLI.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v gh >/dev/null || { echo 'GitHub CLI (gh) is required' >&2; exit 2; }
mkdir -p bin
if [[ -e bin/lightpanda-merge-f734e7f6 ]]; then
  echo 'Existing executable found; verifying before reuse'
else
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  gh run download 39102731049 -R lightpanda-io/browser -n lightpanda-build-release -D "$tmp"
  test -f "$tmp/lightpanda"
  mv "$tmp/lightpanda" bin/lightpanda-merge-f734e7f6
fi
printf '%s  %s\n' '861906c6493cffb94a45667648ed5c3db091ae659adb4c0532e0dc9afd9607a5' 'bin/lightpanda-merge-f734e7f6' | sha256sum -c -
chmod 0755 bin/lightpanda-merge-f734e7f6
./bin/lightpanda-merge-f734e7f6 version
