#!/usr/bin/env bash
# Fetch the Jigsaw English six-label training CSV and record its raw_sha256.
#
# Credentials come from `pass` or the environment. No Kaggle credential file is ever
# materialised on disk, and the API key never enters argv (visible to `ps`): it is fed to
# curl on stdin via --config -.
#
# The member-file endpoint is deliberate. The parent archive also packages a ~1.4 GB
# unintended-bias training file with a different schema that must never be trained on, and
# two multilingual single-label files. Fetching one member is 37 MB instead of ~1.5 GB.
set -euo pipefail
umask 077

DATASET="julian3833/jigsaw-multilingual-toxic-comment-classification"
MEMBER="jigsaw-toxic-comment-train.csv"
DEST_DIR="${DEST_DIR:-data/raw}"
DEST="${DEST_DIR}/${MEMBER}"

# Idempotent: a corpus already on disk that still matches its recorded digest is the
# corpus every downstream number was derived from, so there is nothing to fetch and no
# reason to touch the credential store.
if [[ -f "$DEST" && -f "${DEST}.sha256" ]]; then
  recorded="$(tr -d '[:space:]' <"${DEST}.sha256")"
  actual="$(sha256sum "$DEST" | awk '{print $1}')"
  if [[ -n "$recorded" && "$recorded" == "$actual" ]]; then
    echo "${DEST} already present, raw_sha256=${recorded}; skipping download"
    exit 0
  fi
  echo "digest mismatch for ${DEST} (recorded ${recorded}, on disk ${actual}); refetching" >&2
fi

username="${KAGGLE_USERNAME:-$(pass show kaggle/username | head -n1)}"
key="${KAGGLE_KEY:-$(pass show kaggle/api-key | head -n1)}"
: "${username:?set KAGGLE_USERNAME or seed pass kaggle/username}"
: "${key:?set KAGGLE_KEY or seed pass kaggle/api-key}"

mkdir -p "$DEST_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

printf 'user = "%s:%s"\n' "$username" "$key" \
  | curl --config - --fail --location --silent --show-error \
         --output "${tmp}/member.zip" \
         "https://www.kaggle.com/api/v1/datasets/download/${DATASET}/${MEMBER}"

unzip -o -q "${tmp}/member.zip" -d "$tmp"
mv "${tmp}/${MEMBER}" "$DEST"
sha256sum "$DEST" | awk '{print $1}' >"${DEST}.sha256"

echo "wrote ${DEST} ($(wc -c <"$DEST") bytes)"
echo "raw_sha256=$(cat "${DEST}.sha256")"
