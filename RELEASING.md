# Releasing

Everything below is prepared and validated locally. Nothing has been published —
these are the steps to take when you decide to.

## Current state

| Item | Status |
|---|---|
| `pyproject.toml` | Ready, version `0.1.0` |
| `server.json` | Ready, MCP registry schema `2025-12-11` |
| `README.md` | Carries the `<!-- mcp-name: io.github.AImplifier/eeg-mcp -->` marker |
| `LICENSE` / `NOTICE` | BSD-3-Clause, third-party attribution, safety notice |
| `CHANGELOG.md` | 0.1.0 entry, including known limitations |
| `MANIFEST.in` | Tests and docs in sdist, no data files ever |
| Distributions | Built, `twine check` **PASSED** on both |
| Docs | `mkdocs build --strict` clean |
| Tests | 126 checks passing (57 + 24 + 45) |
| Git | **Not initialised** — see step 1 |

## Version numbers live in three places

Bump all three together, or the registry will reject the submission:

1. `pyproject.toml` → `version`
2. `eeg_mcp/__init__.py` → `__version__`
3. `server.json` → both `version` and `packages[0].version`

## Step 1 — Initialise the repository

Not done automatically, since committing is your call.

```bash
cd eeg-mcp
git init -b main
git add .
git commit -m "Initial commit: eeg-mcp 0.1.0"
git remote add origin https://github.com/AImplifier/eeg-mcp.git
git push -u origin main
```

`.gitignore` already excludes recordings (`*.edf`, `*.fif`, …) and the
`recordings/` directory, so patient data cannot be committed by accident.
**Check `git status` before the first commit anyway.**

## Step 2 — Create the GitHub repository

Under the `AImplifier` organisation, named `eeg-mcp`, so the namespace matches
the `io.github.AImplifier/eeg-mcp` name in `server.json`. The registry verifies
this via OIDC — a mismatch is rejected.

Then in repository settings:

- **Pages** → Source = *GitHub Actions* (needed by `docs.yml`)
- **Environments** → create `pypi`

## Step 3 — Configure PyPI Trusted Publishing

At [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/),
add a pending publisher:

| Field | Value |
|---|---|
| PyPI project name | `eeg-mcp` |
| Owner | `AImplifier` |
| Repository name | `eeg-mcp` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

No API token is stored anywhere.

> **Note.** `eeg-mcp` should be free on PyPI, but confirm at
> <https://pypi.org/project/eeg-mcp/> before tagging. If taken, rename the
> distribution in `pyproject.toml` and `server.json` — the import package
> `eeg_mcp` can stay.

## Step 4 — Rehearse on TestPyPI (recommended)

```bash
python -m build
twine upload --repository testpypi dist/*

conda create -n eeg-mcp-verify python=3.11 -y && conda activate eeg-mcp-verify
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ eeg-mcp
python -c "from eeg_mcp.server import mcp; print('ok')"
python -m eeg_mcp   # should start and wait on stdio
```

The extra index is required — TestPyPI does not carry brainflow, mne, or fastmcp.

## Step 5 — Tag and release

```bash
git tag v0.1.0
git push origin v0.1.0
```

`publish.yml` then runs the three suites, builds, publishes to PyPI via Trusted
Publishing, waits for indexing, and submits to the MCP registry with GitHub
OIDC. Publishing is gated on the tests passing.

## Step 6 — Verify

```bash
pip install eeg-mcp                              # PyPI
curl "https://registry.modelcontextprotocol.io/v0/servers?search=eeg-mcp"
```

Check the docs site at <https://aimplifier.github.io/eeg-mcp/>.

## Pre-flight checklist

- [ ] Versions match in all three files
- [ ] `CHANGELOG.md` has an entry for this version
- [ ] `python testing/verify.py` passes
- [ ] `python testing/persona_bci_researcher.py` passes
- [ ] `python testing/persona_clinician.py` passes
- [ ] `python -m mkdocs build --strict` clean
- [ ] `python -m build && twine check dist/*` passes
- [ ] `git status` shows no recordings or patient data staged
- [ ] `README.md` still carries the `mcp-name` marker (registry ownership check)
- [ ] Safety language intact in README, NOTICE, and `docs/safety.md`

## Things to fix before a 1.0

Honest list, from the [known limitations](CHANGELOG.md):

- No stimulation backend has run against real hardware. Until one has, the
  hardware paths should be described as untested — which they currently are, in
  the README, NOTICE, docs, and CHANGELOG. **Do not soften that language to make
  the project look more finished.**
- No real amplifier has been exercised, only the synthetic board and replay.
- Long-run (multi-hour) stability is untested.
- Train timing carries a few ms of jitter — documented, but a hardware-sequenced
  path would be a genuine 1.0 feature.

## Regenerating derived files

```bash
python testing/gen_hardware_docs.py    # docs/hardware.md, after a BrainFlow bump
python testing/make_synthetic.py       # test fixtures (gitignored)
```
