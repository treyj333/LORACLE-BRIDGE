# Contributing to LORACLE

Thank you for your interest in contributing to LORACLE! Community contributions help keep this project growing and improving.

---

## Before You Start

**Open an issue first.** Before writing any code, please [open an issue](../../issues/new) to discuss your proposed change. This helps avoid duplicate work and ensures your contribution aligns with the project's direction.

---

## Getting Started

### Prerequisites

- macOS or Linux
- Python 3.9+
- A Meshtastic radio (for testing radio features)
- Ollama installed locally

### Fork & Clone

1. Fork this repository
2. Clone your fork locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/loracle.git
   cd loracle
   ```
3. Add the upstream remote:
   ```bash
   git remote add upstream https://github.com/treyj333/loracle.git
   ```

---

## Development Workflow

1. **Sync with upstream** before starting new work:
   ```bash
   git fetch upstream
   git checkout main
   git rebase upstream/main
   ```

2. **Create a feature branch:**
   ```bash
   git checkout -b fix/issue-123
   # or
   git checkout -b feature/add-new-feature
   ```

3. **Make your changes.** Follow existing code style and conventions. Test locally before submitting.

4. **Run tests:**
   ```bash
   cd meshtastic-bridge
   venv/bin/python -m pytest tests/
   ```

5. **Commit your changes** using [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat: add new mesh command
   fix: handle radio disconnect during send
   docs: update connection troubleshooting
   ```

6. **Push your branch** and open a pull request.

---

## Submitting a Pull Request

1. Push your branch to your fork
2. Open a pull request against the `main` branch
3. In the PR description:
   - Summarize what your changes do and why
   - Reference the related issue (e.g., `Closes #123`)
   - Note any testing steps
4. Be responsive to feedback

---

*LORACLE is licensed under the [Apache License 2.0](LICENSE).*
