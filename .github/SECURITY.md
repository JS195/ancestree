# Security Policy

This is the security policy for **Ancestree** (published on PyPI as
[`ancestree-track`](https://pypi.org/project/ancestree-track/), imported as
`ancestree`). Source: [github.com/JS195/ancestree](https://github.com/JS195/ancestree).
Maintainer: Joshua Smith ([78921007+JS195@users.noreply.github.com](mailto:78921007+JS195@users.noreply.github.com)).

## Supported Versions

Only the latest stable release is currently supported with security updates.

| Version | Supported          |
|---------|--------------------|
| Latest  | :white_check_mark: |
| Older   | :x:                |

If you are using an older version, we strongly recommend upgrading to the latest release.

## Reporting a Vulnerability

We take security seriously. If you discover a security vulnerability in this project, please report it responsibly.

**Please do not report security vulnerabilities through public GitHub issues.**

### How to Report

1. **Email the maintainer**, Joshua Smith, at: [78921007+JS195@users.noreply.github.com](mailto:78921007+JS195@users.noreply.github.com)

2. Provide the following information in your report:
   - Project version affected
   - Description of the vulnerability
   - Steps to reproduce the issue
   - Potential impact (e.g., data leak, remote code execution, etc.)
   - Any suggested mitigation or fix (if available)
   - Your name and contact information (optional but helpful)

### What to Expect

- You will receive an acknowledgment of your report within **48 hours**.
- We will investigate and provide a timeline for a fix.
- We will keep you informed of our progress.
- Once the issue is resolved, we will publicly acknowledge your contribution (unless you prefer to remain anonymous).

## Security Best Practices

- Keep your Python environment and dependencies up to date.
- Use a virtual environment for this package.
- Regularly run `pip check` or dependency vulnerability scanners like `pip-audit` or `safety`.

## Disclosure Policy

We follow a coordinated disclosure process:
- We aim to fix confirmed vulnerabilities as quickly as possible.
- Security fixes will be released with a new version and clearly documented in the [changelog](CHANGELOG.md).
- We will notify users through GitHub Releases, PyPI, and relevant channels.

---

Thank you for helping keep this project secure! 🙏

If you have any questions about this policy, feel free to open a [Discussion](https://github.com/JS195/ancestree/discussions).