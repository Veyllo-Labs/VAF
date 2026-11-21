# Contributing to VAF

We love your input! We want to make contributing to VAF as easy and transparent as possible, whether it's:

- Reporting a bug
- Discussing the current state of the code
- Proposing new features
- Building your own tools and workflows
- Sharing what you built with the community (e.g., a YouTube tutorial or write-up)

## How to Contribute

For anything beyond a small fix, please open an issue first to discuss the change.

1.  **Fork and Clone**
    Fork the repo and clone it to your local machine:
    ```bash
    git clone https://github.com/YOUR-USERNAME/VAF.git
    cd VAF
    pip install --require-hashes -r requirements.lock  # Python runtime deps, exactly as CI installs them
    pip install --no-deps -e .                          # the vaf package + the `vaf` CLI entry point
    ```
    `requirements.txt` gives you the newest versions its ranges allow instead, which is what
    `vaf update` and the installers do (see [Dependencies and the lock](#dependencies-and-the-lock)).
    For the **full runtime** (Docker memory stack + Web UI + tray), run `./install.sh` instead - it
    provisions Python, Node, Docker and the Web UI for you.

2.  **Create a Branch**
    Create a branch for your feature or fix:
    ```bash
    git checkout -b feature/amazing-feature
    # or
    git checkout -b fix/annoying-bug
    ```

3.  **Code & Test**
    Make your changes. We rely on the community to test their own code.
    - If you add functionality, **add tests**.
    - If you fix a bug, **add a regression test**.
    - **Continuous Integration (CI):** All pushes are automatically tested via GitHub Actions (Windows/Linux/Mac).
    - Run the same checks locally to save time:
      ```bash
      # Run the test suite (matches CI logic). On Linux and macOS it runs in a home folder
      # of its own, and it never connects to VAF's own ports, so a VAF running on this
      # machine is not touched.
      pytest tests/
      
      # Verify CLI startup (Smoke Test)
      vaf --version
      ```

4.  **Lint & Check**
    Run the same checks CI gates on before committing:
    ```bash
    # Ruff gate (these are the only rules that fail CI; everything else is warnings)
    ruff check . --select=E9,F63,F7,F82 --exclude vaf/tools/coder_templates

    # Doc links and license headers (both fail CI when broken)
    python scripts/check_doc_links.py
    python scripts/check_license_headers.py
    ```
    If your change touches the Web UI, also make sure it builds:
    ```bash
    cd web && npm install && npm run build
    ```
    Do not run a repo-wide formatter (`ruff format .` or `vaf automate format`): the codebase
    is not formatted that way, and you would produce a huge unrelated diff. If you use a
    formatter at all, apply it only to the files you touched.

5.  **Commit**
    Use descriptive commit messages. We prefer the [Conventional Commits](https://www.conventionalcommits.org/) specification (e.g., `feat: add new tool`, `fix: context overflow`).
    ```bash
    # Or let VAF generate it for you:
    vaf git commit --auto
    ```

6.  **Push & PR**
    Push your branch and open a Pull Request against `main`.
    ```bash
    git push origin feature/amazing-feature
    ```

## Dependencies and the lock

`requirements.txt` (which includes `requirements-headless.txt`) states the ranges VAF
supports. `requirements.lock` is one hash-pinned resolution of it for every operating system
and Python version CI tests (Linux, macOS and Windows, Python 3.10 to 3.13). Every push, the
nightly full matrix, the release gate and `scripts/ci_check.sh` install the lock with
`pip install --require-hashes`, so a new upstream release cannot turn a push red that your
local checks passed. `scripts/ci_check.sh` builds a venv of its own from the lock, one per
checkout (cached under `~/.cache/vaf-ci/`, about 1.6 GB and a minute or two the first time;
the venv of a removed checkout or an older lock is cleaned up by the next run) and never
installs into your `venv/`. Each new checkout or worktree downloads the whole lock again,
about 0.5 GB on Linux: the lock is installed without pip's cache (see below). Its small pip
cache in `~/.cache/vaf-ci/pip` (the wheels of pip, setuptools and uv) is not pruned.

**When you change a requirements file**, regenerate the lock in the same commit, from the
repository root, with the committed lock present (uv then keeps every pin that still fits),
in a throwaway venv with the uv the `lock-sync` job installs:

```bash
python -m pip install --require-hashes --force-reinstall -r .github/requirements/pip.lock
python -m pip install --require-hashes --force-reinstall -r .github/requirements/uv.lock
uv pip compile requirements.txt --config-file requirements-lock.toml --universal --generate-hashes --python-version 3.10 -o requirements.lock
venv/bin/python scripts/check_requirements_lock.py   # every CI target must report 0 problems
```

The checker needs the `packaging` library, which the throwaway venv does not have; the
repository's `venv/` (or any Python with `packaging`) runs it. On Windows that venv's Python
is `venv\Scripts\python.exe` (`venv/Scripts/python.exe` in Git Bash).

The `lock-sync` job runs the same command and fails when the result differs from the
committed lock. A diff cannot see a hand-edited hash (uv keeps the hashes a committed lock
already has for a pin it keeps), so the checker also compares every hash with what PyPI
publishes. `requirements-lock.toml` carries uv's settings: today one override for a package
whose wheels declare their requirements inconsistently (the comment there explains it).

Every lane installs the lock through `scripts/install_lock.sh`, which also says why each of
its steps is there: pip comes by hash, the one release without a wheel is built with the
locked setuptools instead of one fetched unpinned, every step reinstalls even a version that
is already there (pip checks no hash for a requirement it calls satisfied), and the lock is
installed without pip's cache (pip trusts a wheel it built and cached past
`--require-hashes`). Each CI lane first makes a fresh venv with `scripts/ci_venv.py` and
installs into that, never into the runner's own Python: a runner image ships packages of its
own there (pipx on the Windows runner), which the suite would run beside and the drift check
would report. `check_requirements_lock.py --drift` refuses a Python that is not such an
isolated venv, so run it by hand inside one as well (`scripts/ci_check.sh` already does).

The CI tools have small locks of their own in `.github/requirements/`: `pip.lock` (installed
first everywhere), `uv.lock` (regenerating the locks) and `publish.lock` (building and
checking the release artefacts). Each is generated from the `.in` file next to it with the
command in its header, and `lock-sync` checks them like `requirements.lock`. To raise pip,
edit `pip.in`; uv, `uv.in`; a build or upload tool, `publish.in`; then regenerate that lock.
Dependabot does not raise these: its pip support covers `.txt` files, and these are uv's
`.in`/`.lock` pairs, so they are refreshed by hand when a release is cut
(docs/setup/RELEASING.md).

**Moving a pin on purpose** (a security fix, a feature you need): add
`--upgrade-package <name>` to the command above, or `--upgrade` for everything, and review
the diff.

**The newest versions** are tested daily by "VAF CI (newest dependencies)"
(`.github/workflows/ci-latest-deps.yml`): it installs `requirements.txt` the way `vaf update`
does and runs the same suite. It never blocks a push. When it is red, its job summary lists
the installed versions that differ from the lock: reproduce with the newer version, then fix
the code or cap the range (in `requirements*.txt`, `pyproject.toml` and
`docs/legal/THIRD_PARTY.md` alike), and refresh the lock as above.

## PR Guidelines

### Before Submitting
- [ ] Target the `main` branch
- [ ] Test everything locally (`pytest tests/`)
- [ ] Run the Ruff gate and the doc-link/license-header checks (see "Lint & Check" above)
- [ ] Update `CHANGELOG.md` if this is a user-facing change
- [ ] **Verify CI/CD:** Ensure your local tests pass; GitHub Actions will run the same suite.
- [ ] Re-read your changes - ensure they are clean and well-thought-out
- [ ] **Sign off your commits** (`git commit -s`) - see [License & Contributor Terms](#license--contributor-terms) below
- [ ] **Web UI text:** If you add or change user-visible strings in the Web UI, use translation keys and update the locale files under `web/messages/` (see [docs/platform/I18N.md](docs/platform/I18N.md)).

### PR Description Template

```markdown
## Description
Brief description of what this PR does.

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] I have tested this locally
- [ ] All existing tests pass
- [ ] I have added tests for new functionality

## Additional Notes
Any additional context or notes for reviewers.
```

### What We Look For
**Good PRs:**
- Clear, focused changes
- Comprehensive testing
- Good commit messages
- Self-reviewed code

**Avoid:**
- Massive PRs that change everything
- Untested code
- Formatting issues
- AI-generated code that wasn't reviewed

## Project Structure
```
vaf/
├── core/
│   ├── agent.py          # The Brain (Main Agent logic)
│   ├── context.py        # Context Management & Compression
│   ├── api_backend.py    # LLM Provider Integration
│   └── subagent_ipc.py   # Inter-Process Communication
├── cli/
│   ├── tui.py            # Terminal UI Implementation
│   └── cmd/              # All CLI Commands (run, generate, scaffold, etc.)
├── tools/                # Drop-in Tools (Plugin System / BaseTool)
├── workflows/
│   └── workflows/        # Pre-built Workflow Definitions (.py)
└── main.py               # Application Entry Point
```
### Key Areas for Contribution
- **CLI Commands:** `vaf/cli/cmd/`
- **Tools:** `vaf/tools/`
- **Core Logic:** `vaf/core/`
- **Workflows:** `vaf/workflows/workflows/`
- **Tests:** `tests/`

## Reporting Issues

### Bug Reports
Please include:
- VAF version (`vaf --version`)
- Python version (`python --version`)
- Operating system
- Steps to reproduce
- Expected vs actual behavior
- Error messages/logs

### Feature Requests
Please include:
- Clear description of the feature
- Use case/motivation
- Proposed implementation (if you have ideas)
- Willingness to contribute

## AI-Generated Code

We **welcome** AI-generated code (after all, this is an AI framework!), but with conditions:
1.  **You are responsible:** You must review, understand, and test any code generated by an AI.
2.  **No blind copy-paste:** Ensure the code fits the project's architecture and style.
3.  **Clean up:** Remove any AI artifacts (excessive comments, "Here is the code" text).

## Developing Tools & Workflows

### Creating New Tools
VAF uses a drop-in plugin system. To add a tool:
1.  Create a file in `vaf/tools/` (e.g., `my_tool.py`).
2.  Inherit from `BaseTool`.
3.  Define `name`, `description`, and `run()`.
4.  **Tip:** If your tool is only for the Coder agent, set `coder_only = True`.

### Creating Workflows
Create a python script in `vaf/workflows/workflows/` (e.g., `my_workflow.py`). Define the `WORKFLOW` dictionary with triggers and steps.

### Sharing What You Built
Built a tool or workflow you're proud of? Share it with the community - record a short YouTube walkthrough, write a blog post, or post a demo. Link it back here so others can learn from it. Showing how you use VAF is one of the most valuable contributions you can make.

## Style Guide

- **Python:** We support Python 3.10-3.13 (exactly the CI-tested range; the installers reject newer interpreters and provision a supported one via uv). Type hints are **mandatory**.
- **Docstrings:** Every class and function must have a Google-style docstring.
- **Naming:** `snake_case` for functions/variables, `PascalCase` for classes.
- **Imports:** Group standard libs, third-party libs, and local imports.

## License & Contributor Terms

VAF is **dual-licensed**: open source under the **GNU AGPL-3.0-or-later** and, separately,
under a **commercial license** sold by Veyllo GmbH (see [LICENSING.md](LICENSING.md) and
[COMMERCIAL.md](../docs/legal/COMMERCIAL.md)). For this dual model to work, every contribution must be
usable under **both** licenses - which is what the terms below establish.

**Opening a pull request (or otherwise submitting a patch or material) constitutes your
agreement to all three Contributor Terms below.** These terms are accepted by the act of
submitting - the commit sign-off in (3) is a *separate* record of origin and is **not**, by
itself, the instrument that conveys the grant in (2).

1. **Inbound license.** Your contribution is provided under the **AGPL-3.0-or-later**, the
   same license as the project.

2. **Commercial relicensing grant.** By submitting a contribution you **also** grant
   **Veyllo GmbH** an irrevocable, perpetual, worldwide, non-exclusive, royalty-free,
   sublicensable, and transferable license to use, reproduce, modify, distribute, and
   **relicense** your contribution under **any terms, including proprietary/commercial
   terms**, so that your contribution can be included in both the AGPL Community Edition and
   Veyllo GmbH's commercial/Enterprise offerings. This grant is what makes the dual-license
   model possible; without it, your code could not be shipped under the commercial license.
   This grant binds independently of, and in addition to, the sign-off in (3).

3. **Certificate of origin.** You certify that you wrote the contribution or otherwise have
   the right to submit it under the terms above, in line with the
   [Developer Certificate of Origin](https://developercertificate.org/). Sign off your
   commits to record this certification of origin:

   ```bash
   git commit -s -m "feat: ..."
   ```

   The `-s` flag adds a `Signed-off-by: Your Name <you@example.com>` line. (The DCO attests
   to origin only; your acceptance of terms 1 and 2 comes from submitting the contribution,
   not from the sign-off line.)

If you cannot make the commercial relicensing grant in (2) - for example, your contribution
includes third-party code under an incompatible license - say so explicitly in the PR so we
can handle it appropriately (e.g. keep it clearly attributed and AGPL-only, outside the
commercial offering).

---
**Thank you for building VAF with us!**
