# Security

cotop runs with your Colab CLI sign-in, so a bug in it could expose that token
or act on a runtime you didn't choose. The README section
[What cotop can touch](README.md#what-cotop-can-touch) lists what cotop's own
code reads, writes, sends and runs, and each call it makes into the Colab CLI.

## Scope

In scope is cotop's own code. Examples:
- a token reaching the screen, the cache, or a host other than the one Colab returned;
- an action on a runtime other than the one you chose.

Behavior inside `google-colab-cli` or the `colab` command belongs to that
project.

## Reporting a vulnerability

Don't put details in a public issue. Instead, open an issue titled
"Security contact" with nothing else in it, and the maintainer will reply with a
private way to send the report.

Only the latest release on PyPI gets fixes.
