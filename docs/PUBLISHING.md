# Publishing airouter-py to PyPI

This guide walks you through publishing the package to [PyPI](https://pypi.org)
so anyone can `pip install airouter-py`.

> **Name note:** the name `airouter` is **already taken on PyPI** (by an
> unrelated package by Laurentiu Piciu, v0.1.45). This port publishes under
> **`airouter-py`**, which is available. The installed CLI script is still
> `airouter` (the script name and the package name are independent — see
> `[project.scripts]` in `pyproject.toml`).

## TL;DR — the one-shot script

The repo ships with [`publish.sh`](../publish.sh) which does everything for
you: installs `build` + `twine` into the current Python env if missing,
cleans previous artifacts, builds the sdist + wheel, runs `twine check`,
verifies the wheel installs + runs in a fresh venv, checks PyPI for an
existing same-version upload, and asks which target to upload to.

```bash
cd /path/to/airouter-py
./publish.sh                  # interactive: ask TestPyPI vs PyPI
./publish.sh --test           # TestPyPI, no prompt
./publish.sh --prod           # PyPI, no prompt
./publish.sh --dry-run        # do everything EXCEPT the final upload
./publish.sh --skip-fresh-venv   # skip the wheel-install-in-fresh-venv check
```

You still need to do the **one-time setup** below (PyPI account + API token +
`~/.pypirc`) before the first upload.

## Prerequisites (one-time)

1. **Create PyPI accounts**: <https://pypi.org/account/register/> and
   <https://test.pypi.org/account/register/>.
2. **Enable 2FA** on both (PyPI requires it for uploads).
3. **Create an API token** on each:
   - PyPI: <https://pypi.org/manage/account/token/>
   - TestPyPI: <https://test.pypi.org/manage/account/token/>
   - Scope: "Entire account" (you can narrow it later once the project exists).
   - Save the tokens somewhere safe — PyPI shows them only once.
4. **Create `~/.pypirc`** (mode 0600) so `twine` knows where to upload:

   ```ini
   [distutils]
   index-servers =
       pypi
       testpypi

   [pypi]
   username = __token__
   password = pypi-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

   [testpypi]
   repository = https://test.pypi.org/legacy/
   username = __token__
   password = pypi-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   ```

## Pre-publish checklist

Run this every time, before every release:

```bash
cd /path/to/airouter-py

# 1. Bump the version in pyproject.toml (PyPI rejects re-uploads of the same
#    version, even if you deleted the file). Use semver:
#    0.1.0 -> 0.1.1 (patch), 0.2.0 (minor), 1.0.0 (major).
# Edit the line:  version = "0.1.0"

# 2. Update the changelog (keep one in CHANGELOG.md if you want).

# 3. Clean previous build artifacts.
rm -rf build dist airouter.egg-info

# 4. Build the source distribution (sdist) and the wheel.
python -m build

# 5. Lint the metadata (catches missing README, bad classifiers, etc.).
python -m twine check dist/*

# 6. Verify the wheel installs in a fresh venv and the CLI runs.
python -m venv /tmp/venv_test
/tmp/venv_test/bin/pip install dist/airouter_py-*.whl
/tmp/venv_test/bin/airouter --help
/tmp/venv_test/bin/python -c "import airouter; print(airouter.__version__)"
```

If all six steps pass, you're ready to upload.

## Publish to TestPyPI first (recommended)

Always upload to TestPyPI first — it lets you verify the install flow without
polluting the real PyPI namespace.

```bash
python -m twine upload --repository testpypi dist/*
```

Then verify the install from TestPyPI in a fresh venv:

```bash
python -m venv /tmp/test_install
/tmp/test_install/bin/pip install \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    airouter-py
/tmp/test_install/bin/airouter --help
```

(The `--extra-index-url https://pypi.org/simple/` is needed because
TestPyPI only has the airouter-py package — its dependencies like `requests`
and `cryptography` are pulled from the real PyPI.)

## Publish to PyPI

Once TestPyPI looks good:

```bash
python -m twine upload dist/*
```

Within a minute or two:

```bash
pip install airouter-py        # the library + CLI
pip install airouter-py[server] # adds FastAPI / uvicorn / starlette
```

View your package at <https://pypi.org/project/airouter-py/>.

## Automated publishing with GitHub Actions (recommended)

Once you've done the first manual upload (which creates the project on PyPI),
switch to automatic publishing on git tag push. Create
`.github/workflows/publish.yml` in your repo:

```yaml
name: publish

on:
  push:
    tags: ["v*"]

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write      # trusted publishing (no API token needed)
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build twine
      - run: python -m build
      - run: python -m twine check dist/*
      - uses: pypa/gh-action-pypi-publish@release/v1
        # with:
        #   repository-url: https://test.pypi.org/legacy/   # uncomment for TestPyPI
```

Then to release a new version:

```bash
# Bump version in pyproject.toml first.
git commit -am "Release v0.1.1"
git tag v0.1.1
git push origin v0.1.1
```

The workflow builds, lints, and uploads to PyPI automatically. With
**trusted publishing** (the `id-token: write` permission above) you don't
even need an API token — configure it once at
<https://pypi.org/manage/project/airouter-py/settings/publishing/> by adding
your GitHub repo as a trusted publisher.

## Version bumping rules

PyPI does not let you re-upload the same version of a file. To release a new
version:

1. Bump `version = "..."` in `pyproject.toml`.
2. (Optional) Update `CHANGELOG.md`.
3. Clean, build, check, upload.

Use [Semantic Versioning](https://semver.org/):

- `0.1.0` → `0.1.1`: bug fixes only
- `0.1.x` → `0.2.0`: new backwards-compatible features
- `0.x.y` → `1.0.0`: first stable release

While the major version is `0`, breaking changes are allowed in minor bumps
(`0.1.0` → `0.2.0` may break the API).

## What gets published

The build produces two files in `dist/`:

- `airouter_py-0.1.0-py3-none-any.whl` — the wheel (what `pip` installs by
  default; pre-built, fast install, no build step needed on the user side).
- `airouter_py-0.1.0.tar.gz` — the source distribution (sdist; the tarball
  of the source tree, used as a fallback and by other packaging tools).

Both are uploaded on every publish — `twine upload dist/*` uploads everything
in the directory.

## Files PyPI displays

PyPI's project page pulls from these files in your repo:

- `README.md` → the long description on the project page.
- `LICENSE` → shown in the "Meta" sidebar.
- `pyproject.toml` `[project]` table → name, version, description, classifiers,
  keywords, requires-python, project URLs, dependencies.

Keep these tidy — they're the public face of your package.

## Yanking a release (if something is broken)

If a release is broken but you don't want to delete it (people may have
already pinned to it):

```bash
pip install twine
twine upload --repository pypi --skip-existing dist/airouter_py-0.1.0-py3-none-any.whl  # no-op if already uploaded
python -m pip install pymaybe  # not needed
# Yank via the web UI: https://pypi.org/manage/project/airouter-py/releases/
```

Yanking hides the release from `pip install airouter-py` (without a version
specifier) but leaves it downloadable by exact version
(`pip install airouter-py==0.1.0`).

## Deleting a release (only for the first 24h after upload)

PyPI lets you delete a file only within the first 24 hours after upload, and
only via the web UI at <https://pypi.org/manage/project/airouter-py/releases/>.
Use this only if you uploaded the wrong file. After 24h, you must yank and
release a new version instead.

## Common mistakes

- **Re-uploading the same version**: PyPI rejects it with
  `File already exists`. Bump the version in `pyproject.toml`.
- **Forgetting the README**: `twine check` will warn that the long
  description is missing or invalid.
- **Forgetting `license-files = ["LICENSE"]`**: the project page shows no
  license.
- **Publishing from a dirty git tree**: the sdist includes whatever is in
  the working directory. Run from a clean checkout (or better: from a git
  tag) so you don't accidentally include local files.
- **Not running tests before publishing**: at minimum, run
  `python tests/test_e2e_generate.py` and `python tests/test_server_smoke.py`
  against the built wheel, not the source tree.
