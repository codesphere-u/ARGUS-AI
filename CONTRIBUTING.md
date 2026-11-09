# Contributing to ARGUS-AI

Thank you for your interest in contributing to **ARGUS-AI**! We welcome bug reports, feature requests, documentation enhancements, and pull requests.

## Engineering Philosophy & Guidelines

1. **Deterministic Invariants Over Heuristics**: Every core feature must preserve architectural invariants (zero-sum ledgers, hash-chain integrity, token conservation, sandbox isolation).
2. **Comprehensive Test Verification**: Any new code or bugfix must be accompanied by automated tests. PRs without test coverage will not be merged.
3. **Strict Truthfulness & Reproducibility**: Claims in documentation must match reproducible benchmarks and actual test execution.

### Development Workflow (Python)
1. Ensure Python 3.10+ and [`uv`](https://github.com/astral-sh/uv) are installed.
2. Synchronize dependencies: `uv sync --extra dev`
3. Run test suite: `uv run pytest -v`
4. Run static linters: `uv run ruff check .`


## Pull Request Guidelines

1. **Fork & Branch**: Create a feature branch with a descriptive name (`feat/camt053-parser`, `fix/idempotency-race`).
2. **Follow Commit Standards**: Use conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
3. **Verify Locally**: Ensure all existing and new tests pass with 100% green status before pushing.
4. **Fill Out the PR Template**: Detail the motivation, invariants verified, and test outputs.

## Code of Conduct

This repository adheres to the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## Questions & Contact

For questions or security disclosures, reach out to **codesphereinnovationz@gmail.com**.
