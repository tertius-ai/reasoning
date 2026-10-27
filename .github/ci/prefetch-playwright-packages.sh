#!/usr/bin/env bash
# Copyright Benjamin Antieau and Deven Manam.
# SPDX-License-Identifier: Apache-2.0
#
# Usage: prefetch-playwright-packages.sh CACHE_DIR
#
# Puts the packages that `playwright install --with-deps chromium` will install into apt's archive directory first.
# apt fetches them one at a time from one mirror and continues at any nonzero speed, and the runner's Azure mirror
# sometimes serves 40-60 kB/s, which turns the 26 MB install into ten minutes. Here the packages download in
# parallel, a mirror that stays below 200 kB/s for 15 s is abandoned for the next one, and every file must match the
# hash in apt's signed index. Packages that this step does not fetch are left to apt, so it can only save time.
# CACHE_DIR keeps verified packages between runs; "fetched=true" in $GITHUB_OUTPUT means it gained new ones.
set -uo pipefail
shopt -s nullglob

MIRRORS="http://azure.archive.ubuntu.com/ubuntu http://archive.ubuntu.com/ubuntu"

if [ "${1:-}" = fetch ]; then
  path=$2 file=$3 sum=$4 digest=$5
  for mirror in $MIRRORS; do
    if curl -fsS --retry 1 --speed-limit 200000 --speed-time 15 -o "$PREFETCH_WORK/$file" "$mirror/$path" &&
      echo "$digest  $PREFETCH_WORK/$file" | "$sum" -c --status; then
      echo "fetched $file from $mirror"
      exit 0
    fi
    rm -f "$PREFETCH_WORK/$file"
  done
  echo "left $file to apt"
  exit 0
fi

cache=$1
mkdir -p "$cache"
export PREFETCH_WORK
PREFETCH_WORK=$(mktemp -d)

sudo apt-get update -qq || exit 0
packages=$(npm --prefix web exec -- playwright install-deps --dry-run chromium 2>/dev/null | sed -n 's/^  \([a-z0-9][a-z0-9.+:-]*\)$/\1/p')
if [ -z "$packages" ]; then
  echo "Playwright reports no missing system packages"
  exit 0
fi
# --print-uris names each download and the file apt expects in its archive directory; the index records of the same
# candidate versions give each pool path's SHA-512.
# shellcheck disable=SC2086
sudo apt-get install --print-uris -qq -y --no-install-recommends $packages | grep "^'" > "$PREFETCH_WORK/uris"
# shellcheck disable=SC2086
apt-cache show --no-all-versions $packages |
  awk 'function emit() { if (f && s512) print f, "sha512sum", s512; else if (f && s256) print f, "sha256sum", s256 }
       /^Filename: /{f=$2} /^SHA256: /{s256=$2} /^SHA512: /{s512=$2}
       /^$/{emit(); f = s256 = s512 = ""} END{emit()}' > "$PREFETCH_WORK/index"

: > "$PREFETCH_WORK/queue"
while read -r uri file _; do
  uri=${uri//\'/}
  # The URI is percent-encoded (a "+" in a version is "%2b"); the index's Filename is not.
  path=pool/${uri#*/pool/}
  path=$(printf '%b' "${path//%/\\x}")
  read -r sum digest < <(awk -v path="$path" '$1 == path {print $2, $3; exit}' "$PREFETCH_WORK/index")
  if [ -z "${digest:-}" ]; then
    echo "left $file to apt: no SHA-256 or SHA-512 in the index for $uri"
  elif [ -f "$cache/$file" ] && echo "$digest  $cache/$file" | "$sum" -c --status; then
    cp "$cache/$file" "$PREFETCH_WORK/$file"
  else
    echo "$path $file $sum $digest" >> "$PREFETCH_WORK/queue"
  fi
  unset sum digest
done < "$PREFETCH_WORK/uris"

echo "$(wc -l < "$PREFETCH_WORK/uris") packages to install, $(wc -l < "$PREFETCH_WORK/queue") not in the cache"
xargs -r -P 8 -n 4 "$0" fetch < "$PREFETCH_WORK/queue" | tee "$PREFETCH_WORK/log"

debs=("$PREFETCH_WORK"/*.deb)
if [ ${#debs[@]} -gt 0 ]; then
  sudo cp -t /var/cache/apt/archives "${debs[@]}"
fi
if grep -q '^fetched ' "$PREFETCH_WORK/log"; then
  rm -f "$cache"/*.deb
  cp -t "$cache" "${debs[@]}"
  echo "fetched=true" >> "${GITHUB_OUTPUT:-/dev/null}"
fi
