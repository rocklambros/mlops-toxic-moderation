#!/usr/bin/env bash
# Install gitleaks from a release tarball whose checksum is committed to this repository.
#
# The alternative -- a third-party Docker action -- adds a program that reads the entire
# working tree to the set of things that can mint an OIDC token in some other job (premortem
# H35). A scanner should be under the same integrity rule as the code it scans.
#
# The checksum file is `scripts/gitleaks.sha256`, produced by `make gitleaks-checksums` from
# the release's own published checksums. Both linux architectures are recorded so this script
# is not silently arch-specific; the runner is aarch64 (see .github/workflows/ci.yml), and the
# build box is too.
set -euo pipefail

VERSION="8.21.2"
CHECKSUMS="scripts/gitleaks.sha256"

case "$(uname -m)" in
  aarch64|arm64) ARCH="arm64" ;;
  x86_64)        ARCH="x64" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="gitleaks_${VERSION}_linux_${ARCH}.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${TARBALL}"

EXPECTED="$(awk -v want="${TARBALL}" '$2 == want || $2 == "*" want { print $1 }' "${CHECKSUMS}")"
if [ -z "${EXPECTED}" ]; then
  echo "no committed checksum for ${TARBALL} in ${CHECKSUMS}" >&2
  exit 1
fi

mkdir -p bin
curl --fail --silent --show-error --location --output "/tmp/${TARBALL}" "${URL}"
if ! echo "${EXPECTED}  /tmp/${TARBALL}" | sha256sum --check --status; then
  echo "checksum mismatch for ${TARBALL}; refusing to run it" >&2
  exit 1
fi

tar -xzf "/tmp/${TARBALL}" -C bin gitleaks
chmod +x bin/gitleaks
bin/gitleaks version
