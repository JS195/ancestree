# Contributing to Ancestree

Thanks for your interest in improving Ancestree! Issues and pull requests are
welcome — whether it's a bug report, a feature request, documentation, or code.

If you just want to flag something quickly, open an
[issue](https://github.com/JS195/ancestree/issues) or reach out directly at
[78921007+JS195@users.noreply.github.com](mailto:78921007+JS195@users.noreply.github.com).

## Getting started

Ancestree targets **Python 3.9+** and has no runtime dependencies.

```bash
git clone https://github.com/JS195/ancestree.git
cd ancestree
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The `dev` extra installs everything you need to work on the project: `pytest`,
`pytest-cov`, `ruff`, and `mypy`.

> The published package is `ancestree-track` on PyPI, but it imports as
> `ancestree` — that's the name you'll use in code.

## Making a change

1. Fork the repository and create a branch off `main`.
2. Make your change, with tests where it makes sense.
3. Run the checks below and make sure they pass.
4. Open a pull request describing what changed and why.

Small, focused PRs are easier to review and land faster. If you're planning a
large or breaking change, please open an issue first so we can discuss the
approach.

## Checks

These are the same checks CI runs on every push and pull request, so running
them locally first saves a round trip:

```bash
ruff check .                                    # lint
ruff format .                                   # format
mypy src/                                        # type check (strict)
pytest --cov=src/ancestree --cov-report=term    # tests + coverage
```

A PR is in good shape when lint, formatting, type checking, and tests all pass.

### Style

- Formatting and linting are handled by **ruff** (line length 88, double
  quotes). Run `ruff format .` rather than formatting by hand.
- Type hints are required — `mypy` runs in **strict** mode and the package
  ships a `py.typed` marker, so public APIs must stay fully typed.
- Match the conventions of the surrounding code.

### Tests

- Tests live in `tests/` and use `pytest`.
- Please cover new behavior and any bug you fix (a failing test that your
  change makes pass is ideal).
- Run the full suite before opening a PR.

## Documentation

User-facing docs live in `docs/` and are built with MkDocs. If your change
affects behavior or the public API, please update the docs and, where relevant,
the example notebooks in `docs/examples/`. Notable changes should also get an
entry in [`CHANGELOG.md`](CHANGELOG.md).

To preview the docs locally:

```bash
pip install mkdocs-material
mkdocs serve
```

## Reporting bugs

When filing a bug, it helps to include:

- What you expected to happen and what actually happened.
- A minimal snippet that reproduces the issue.
- Your Python version and operating system.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE) that covers this project.
