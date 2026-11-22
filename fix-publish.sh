#!/usr/bin/env bash
# fix-publish.sh — one-shot fix for the "400 File already exists" error.
#
# Run this from your airouter-py repo root. It:
#   1. Bumps the version 0.1.0 -> 0.1.1 (in pyproject.toml + airouter/__init__.py)
#   2. Replaces .github/workflows/publish.yml with the fixed version that
#      skips the upload if the version is already on PyPI
#   3. Commits the changes
#   4. Deletes the old v0.1.0 tag (local + remote) so it can't re-trigger
#   5. Tags v0.1.1 and pushes (main + tag) — this triggers the fixed workflow
#
# After this runs, the GitHub Actions publish workflow will:
#   - Build v0.1.1
#   - Check https://pypi.org/pypi/airouter-py/0.1.1/json -> 404 -> skip=false
#   - Build -> twine check -> SHA256SUMS -> upload v0.1.1 -> DONE
#
set -euo pipefail

cd "$(dirname "$0")"

echo "=== 1. Bump version 0.1.0 -> 0.1.1 ==="
sed -i 's/^version = "0.1.0"/version = "0.1.1"/' pyproject.toml
sed -i 's/^__version__ = "0.1.0"/__version__ = "0.1.1"/' airouter/__init__.py
grep '^version' pyproject.toml
grep '__version__' airouter/__init__.py

echo
echo "=== 2. Write fixed .github/workflows/publish.yml ==="
mkdir -p .github/workflows
cat > .github/workflows/publish.yml <<'YAML'
name: publish

on:
  push:
    tags: ["v*"]

jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install --upgrade build twine

      - name: Read package name + version
        id: meta
        run: |
          python - <<'PY' > /tmp/meta.txt
          import tomllib, pathlib, re
          try:
              d = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
              name, version = d["project"]["name"], d["project"]["version"]
          except Exception:
              s = pathlib.Path("pyproject.toml").read_text()
              name = re.search(r'^name\s*=\s*"([^"]+)"', s, re.M).group(1)
              version = re.search(r'^version\s*=\s*"([^"]+)"', s, re.M).group(1)
          norm = re.sub(r"[-_.]+", "-", name.lower())
          print(f"{norm} {version}")
          PY
          read -r NORM_NAME VERSION < /tmp/meta.txt
          echo "norm_name=$NORM_NAME" >> "$GITHUB_OUTPUT"
          echo "version=$VERSION"    >> "$GITHUB_OUTPUT"

      - name: Check if version is already on PyPI
        id: exists
        run: |
          set +e
          CODE=$(curl -s -o /dev/null -w "%{http_code}" \
            "https://pypi.org/pypi/${{ steps.meta.outputs.norm_name }}/${{ steps.meta.outputs.version }}/json")
          if [ "$CODE" = "200" ]; then
            echo "::notice::Version ${{ steps.meta.outputs.version }} is already on PyPI — skipping upload."
            echo "skip=true" >> "$GITHUB_OUTPUT"
          else
            echo "skip=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Build sdist + wheel
        if: steps.exists.outputs.skip != 'true'
        run: python -m build

      - name: twine check
        if: steps.exists.outputs.skip != 'true'
        run: python -m twine check dist/*.whl dist/*.tar.gz

      - name: Generate SHA256SUMS
        if: steps.exists.outputs.skip != 'true'
        run: ( cd dist && sha256sum *.whl *.tar.gz > SHA256SUMS )

      - name: Publish to PyPI
        if: steps.exists.outputs.skip != 'true'
        uses: pypa/gh-action-pypi-publish@release/v1
        with:
          skip-existing: true
          attestations: true

      - name: Upload build artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/
YAML
echo "wrote .github/workflows/publish.yml ($(wc -l < .github/workflows/publish.yml) lines)"

echo
echo "=== 3. Commit the fix ==="
git add pyproject.toml airouter/__init__.py .github/workflows/publish.yml
git commit -m "Bump to 0.1.1 + fix publish workflow (skip if version already on PyPI)"

echo
echo "=== 4. Delete old v0.1.0 tag (local + remote) ==="
git tag -d v0.1.0 2>/dev/null || echo "(no local v0.1.0 tag)"
git push origin :refs/tags/v0.1.0 2>/dev/null || echo "(no remote v0.1.0 tag, or already deleted)"

echo
echo "=== 5. Tag v0.1.1 + push (main + tag) ==="
git tag v0.1.1
git push origin main
git push origin v0.1.1

echo
echo "=== DONE ==="
echo "Watch the workflow at:"
echo "  https://github.com/$(git remote get-url origin | sed -E 's|.*github.com[:/]([^/]+/[^/.]+).*|\1|')/actions"
echo
echo "Once it goes green, verify:"
echo "  pip install airouter-py==0.1.1"
echo "  python -c 'import airouter; print(airouter.__version__)'"
