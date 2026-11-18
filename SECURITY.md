# Security

cotop runs with your Colab CLI sign-in, so a bug in it could expose that token
or act on a runtime you didn't choose. The README's
[guarantees](README.md#guarantees) say what cotop's code holds to, each with the
test on `main` that checks it.

## Scope

In scope is cotop's own code. Examples:
- a token reaching the screen, the cache, or a host other than the one Colab returned;
- an action on a runtime other than the one you chose.

Behavior inside `google-colab-cli` or the `colab` command belongs to that
project.

## Reporting a vulnerability

Don't put details in a public issue. Open an issue titled "Security contact"
with no other text, and the maintainer will reply with a private way to send
the report.

Fixes go into the latest release on PyPI.
