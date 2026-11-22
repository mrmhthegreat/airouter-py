#!/usr/bin/env bash
# publish.sh — one-shot build + upload for airouter-py.
#
# What it does, in order:
#   1. Verifies you're in the project root (pyproject.toml present).
#   2. Auto-installs `build` and `twine` into the current Python environment
#      if they're missing (so you never see "No module named twine" again).
#   3. Cleans previous build artifacts (build/, dist/, *.egg-info/).
#   4. Builds the sdist (.tar.gz) + wheel (.whl) into dist/.
#   5. Runs `twine check dist/*` (catches missing README, bad classifiers).
#   6. Generates dist/SHA256SUMS so downloaders can verify file integrity.
#   7. Reads the version from pyproject.toml and asks PyPI whether that
#      version already exists. If it does, aborts (PyPI rejects re-uploads).
#   8. Installs the wheel in a throwaway venv and runs `airouter --help` +
#      `python -c "import airouter"` to prove the wheel actually works.
#      (Skip with --skip-fresh-venv.)
#   9. Asks which target to upload to:
#         --test   -> TestPyPI
#         --prod   -> PyPI
#         (none)   -> interactive prompt
#   10. Uploads via `twine upload`.
#
# Prerequisites (one-time, see docs/PUBLISHING.md):
#   * PyPI account + 2FA + API token at https://pypi.org/manage/account/token/
#   * TestPyPI account + API token at https://test.pypi.org/manage/account/token/
#   * ~/.pypirc configured (mode 0600):
#
#       [distutils]
#       index-servers = pypi testpypi
#       [pypi]
#       username = __token__
#       password = pypi-XXXXXXXX
#       [testpypi]
#       repository = https://test.pypi.org/legacy/
#       username = __token__
#       password = pypi-XXXXXXXX
#
# Usage:
#   ./publish.sh                 # interactive: ask which target
#   ./publish.sh --test          # TestPyPI, no prompt
#   ./publish.sh --prod          # PyPI, no prompt
#   ./publish.sh --skip-fresh-venv   # skip the wheel-install-in-fresh-venv check
#   ./publish.sh --dry-run       # do everything except the final upload
set -euo pipefail

# --- colors -----------------------------------------------------------------
if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BLUE=$'\033[34m'; RESET=$'\033[0m'; BOLD=$'\033[1m'
else
    GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""; BOLD=""
fi

log()  { printf '%s>>>%s %s\n' "$BLUE" "$RESET" "$*"; }
ok()   { printf '%s OK %s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!! %s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%sERR%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# --- parse flags ------------------------------------------------------------
TARGET=""
SKIP_FRESH_VENV=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --test)              TARGET="testpypi" ;;
        --prod)              TARGET="pypi" ;;
        --skip-fresh-venv)   SKIP_FRESH_VENV=1 ;;
        --dry-run)           DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) die "unknown flag: $arg (try --help)" ;;
    esac
done

# --- 1. project root check --------------------------------------------------
[[ -f pyproject.toml ]] || die "run this from the airouter-py project root (no pyproject.toml found in $PWD)"
[[ -f airouter/__init__.py ]] || die "pyproject.toml found but airouter/__init__.py is missing — wrong directory?"

# Pick the Python interpreter. Prefer the venv one if VIRTUAL_ENV is set,
# otherwise fall back to python3.
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || die "python interpreter '$PY' not found"
log "using Python: $($PY --version) ($($PY -c 'import sys; print(sys.executable)'))"

# --- 2. ensure build + twine are installed ----------------------------------
ensure_pkg() {
    local pkg="$1"
    if ! "$PY" -c "import $pkg" >/dev/null 2>&1; then
        warn "$pkg not installed in this Python env — installing it now (pip install $pkg)"
        "$PY" -m pip install --quiet --upgrade "$pkg" || die "failed to install $pkg"
    fi
}
log "checking that 'build' and 'twine' are available..."
ensure_pkg build
ensure_pkg twine
ok "build + twine ready"

# --- 3. read version + name from pyproject.toml -----------------------------
read -r NAME VERSION <<<"$("$PY" -c '
import tomllib, pathlib
try:
    d = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    print(d["project"]["name"], d["project"]["version"])
except Exception:
    import re, pathlib
    s = pathlib.Path("pyproject.toml").read_text()
    n = re.search(r"^name\s*=\s*\"([^\"]+)\"", s, re.M).group(1)
    v = re.search(r"^version\s*=\s*\"([^\"]+)\"", s, re.M).group(1)
    print(n, v)
')"
[[ -n "$NAME" && -n "$VERSION" ]] || die "could not read name/version from pyproject.toml"
log "package: $BOLD$NAME$RESET version: $BOLD$VERSION$RESET"

# Normalise the name the same way PyPI does (PEP 503): lowercase, runs of
# [-_.] become a single dash.
NORM_NAME=$(echo "$NAME" | tr '[:upper:]' '[:lower:]' | sed -E 's/[-_.]+/-/g')

# --- 4. check if this version is already on PyPI ----------------------------
log "checking PyPI for existing $NORM_NAME $VERSION..."
EXISTS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://pypi.org/pypi/$NORM_NAME/$VERSION/json" || echo "000")
if [[ "$EXISTS_CODE" == "200" ]]; then
    die "version $VERSION is already on PyPI — bump the version in pyproject.toml before re-publishing."
fi
ok "version $VERSION is not yet on PyPI (good)"

# --- 5. clean + build -------------------------------------------------------
log "cleaning previous build artifacts..."
rm -rf build dist *.egg-info airouter.egg-info
ok "clean"

log "building sdist + wheel..."
"$PY" -m build --quiet
WHEEL=$(ls dist/*.whl | head -1)
SDIST=$(ls dist/*.tar.gz | head -1)
[[ -f "$WHEEL" ]] || die "no .whl produced in dist/"
[[ -f "$SDIST" ]] || die "no .tar.gz produced in dist/"
ok "built $(basename "$WHEEL") + $(basename "$SDIST")"

# --- 6b. SHA256SUMS — checksum file so downloaders can verify integrity -------
log "generating dist/SHA256SUMS ..."
( cd dist && sha256sum *.whl *.tar.gz > SHA256SUMS )
ok "wrote dist/SHA256SUMS:"
sed 's/^/    /' dist/SHA256SUMS

# --- 6. twine check ---------------------------------------------------------
# Note: pass only the actual distribution files (*.whl, *.tar.gz) — twine
# tries to validate every argument and complains about SHA256SUMS.
log "running twine check..."
"$PY" -m twine check dist/*.whl dist/*.tar.gz >/tmp/twine_check.log 2>&1 || { cat /tmp/twine_check.log; die "twine check failed"; }
ok "twine check passed"

# --- 7. fresh-venv install test ---------------------------------------------
if [[ "$SKIP_FRESH_VENV" -eq 0 ]]; then
    log "installing the wheel in a throwaway venv and running smoke tests..."
    FRESH=/tmp/airouter_publish_venv
    rm -rf "$FRESH"
    "$PY" -m venv "$FRESH"
    "$FRESH/bin/pip" install --quiet "$WHEEL" 2>/tmp/fresh_install.log || {
        cat /tmp/fresh_install.log
        die "wheel failed to install in a fresh venv"
    }
    "$FRESH/bin/airouter" --help >/dev/null 2>&1 || die "fresh-venv: 'airouter --help' failed"
    "$FRESH/bin/python" -c "import airouter; assert airouter.__version__ == '$VERSION'; print('import ok, version', airouter.__version__)" >/dev/null || die "fresh-venv: import failed"
    ok "wheel installs + runs in a fresh venv"
else
    warn "skipping fresh-venv test (--skip-fresh-venv)"
fi

# --- 8. pick the upload target ----------------------------------------------
if [[ -z "$TARGET" ]]; then
    echo
    printf '%sWhere do you want to upload?%s\n' "$BOLD" "$RESET"
    printf '  1) TestPyPI  (recommended first; less risk)\n'
    printf '  2) PyPI      (the real thing — anyone can pip install it)\n'
    printf '  3) abort\n'
    printf 'choice [1/2/3]: '
    read -r choice
    case "$choice" in
        1) TARGET="testpypi" ;;
        2) TARGET="pypi" ;;
        *) die "aborted by user" ;;
    esac
fi

case "$TARGET" in
    testpypi)
        UPLOAD_REPO="--repository testpypi"
        TARGET_LABEL="TestPyPI (https://test.pypi.org/project/$NORM_NAME/)"
        ;;
    pypi)
        UPLOAD_REPO=""
        TARGET_LABEL="PyPI (https://pypi.org/project/$NORM_NAME/)"
        ;;
    *) die "internal error: bad TARGET=$TARGET" ;;
esac

# --- 9. confirm + upload ----------------------------------------------------
echo
printf '%sAbout to upload:%s\n' "$BOLD" "$RESET"
printf '  target:    %s\n' "$TARGET_LABEL"
printf '  files:\n'
for f in dist/*; do
    printf '    - %s (%s)\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
done
if [[ "$DRY_RUN" -eq 1 ]]; then
    warn "--dry-run: stopping before the actual upload"
    exit 0
fi

# Pre-flight: warn loudly if ~/.pypirc is missing or doesn't have the target
# section — twine's error for this is confusing ("InvalidConfiguration: ...
# No repository found for ...").
PYPIRC="${HOME}/.pypirc"
if [[ ! -f "$PYPIRC" ]]; then
    warn "$PYPIRC does not exist — twine will fail. See docs/PUBLISHING.md for the format."
    die "aborted: create ~/.pypirc with your PyPI/TestPyPI API token first"
fi
if ! grep -qE "^\[$TARGET\]" "$PYPIRC"; then
    warn "$PYPIRC has no [$TARGET] section — twine will fail."
    die "aborted: add a [$TARGET] section to ~/.pypirc (see docs/PUBLISHING.md)"
fi
ok "found [$TARGET] section in $PYPIRC"

printf 'proceed with upload? [y/N]: '
read -r confirm
[[ "$confirm" =~ ^[Yy]$ ]] || die "aborted by user"

log "uploading to $TARGET..."
# Upload only the distribution files (not SHA256SUMS — PyPI rejects it).
"$PY" -m twine upload $UPLOAD_REPO dist/*.whl dist/*.tar.gz

echo
ok "$BOLD published $NAME $VERSION to $TARGET $RESET"
echo
echo "  View it at:    $TARGET_LABEL"
echo "  Install with:  pip install $NAME"
echo "                 pip install $NAME[server]"
echo
if [[ "$TARGET" == "testpypi" ]]; then
    echo "  Verify in a fresh venv:"
    echo "    python -m venv /tmp/check && /tmp/check/bin/pip install \\"
    echo "      --index-url https://test.pypi.org/simple/ \\"
    echo "      --extra-index-url https://pypi.org/simple/ \\"
    echo "      $NAME"
    echo "    /tmp/check/bin/airouter --help"
fi
