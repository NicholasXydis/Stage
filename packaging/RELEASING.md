# Releasing

## 1. Prepare

Bump `__version__` in `src/stage/__init__.py`. That is the only place a version
is written; `pyproject.toml` reads it dynamically and the release workflow
refuses to publish when the tag and the packaged version disagree.

Confirm every gate is green. The serial tests matter: `conftest.py` skips them
under xdist, so `pytest -n auto` alone never runs them.

```bash
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy
uv run pytest -q -n auto --cov --cov-branch --cov-report=
uv run pytest -q -m serial --cov --cov-append --cov-branch --cov-fail-under=87
uv run --with pip-audit pip-audit --strict --locked
```

The release workflow runs all of these again before it builds, so a red gate
stops the publish rather than shipping. Real PyPI publishes only from a
`v*` tag; a manual dispatch can reach TestPyPI only.

## 2. One-time PyPI setup

Both indexes use Trusted Publishing, so no API token is ever stored.

On [TestPyPI](https://test.pypi.org/manage/account/publishing/) and then
[PyPI](https://pypi.org/manage/account/publishing/), add a pending publisher:

| Field | Value |
| --- | --- |
| PyPI project name | `stage-cli` |
| Owner | `NicholasXydis` |
| Repository name | `Stage` |
| Workflow name | `release.yml` |
| Environment name | `testpypi` or `pypi` |

Then create matching environments in
[repository settings](https://github.com/NicholasXydis/Stage/settings/environments):
`testpypi` and `pypi`. Add a required reviewer on `pypi` so a real release
cannot happen without an approval click.

## 3. Dry run on TestPyPI

Actions -> Release -> Run workflow -> target `testpypi`.

Verify the result installs:

```bash
uv tool install --index https://test.pypi.org/simple/ \
  --index-strategy unsafe-best-match stage-cli
stage --version
uv tool uninstall stage-cli
```

## 4. Publish

```bash
git tag v1.0.0
git push origin v1.0.0
```

The tag triggers the workflow, which verifies the tag matches `__version__`,
builds the wheel and sdist, smoke-tests the sdist, and publishes to PyPI after
the environment approval.

Confirm:

```bash
uv tool install stage-cli
stage --version
```

A published version number can never be reused, even after deletion.

## 5. Homebrew

Create `NicholasXydis/homebrew-tap` with a `Formula/` directory.

Fill in the sdist URL and checksum from the PyPI release:

```bash
python packaging/update_formula.py 1.0.0
```

Generate the dependency resources, which requires the formula to be in a tap:

```bash
cp packaging/stage.rb "$(brew --repository)/Library/Taps/nicholasxydis/homebrew-tap/Formula/stage.rb"
brew update-python-resources stage
brew install --build-from-source stage
brew test stage
```

Commit the filled formula to the tap, then verify from a clean state:

```bash
brew uninstall stage
brew install NicholasXydis/tap/stage
stage --version
```
