# Security

cotop runs with your Colab CLI sign-in, so a bug in it could expose that token
or act on a runtime you didn't choose. The [guarantees](#guarantees) below say
what cotop's code holds to, each with the test on `main` that checks it.

## Reporting a vulnerability

Don't put details in a public issue. Report it privately through
[GitHub's form](https://github.com/benngarcia/cotop/security/advisories/new),
which goes to the maintainer instead of the public.

Fixes go into the latest release on PyPI.

## Scope

In scope is cotop's own code. Examples:
- a token reaching the screen, the cache, or a host other than the one Colab returned;
- an action on a runtime other than the one you chose.

Behavior inside `google-colab-cli` or the `colab` command belongs to that
project.

## What cotop can touch

cotop signs in with the Colab CLI's own credentials, so it can do anything `colab` can, with the access you granted when you signed in. It adds no credentials of its own.

### Guarantees

Each line is true of this release and names the test on `main` that checks it:

- Before it stops, restarts or opens a shell on a named runtime, cotop checks that the name still points at the runtime you selected (`test_every_named_action_checks_the_name_still_means_this_runtime`).
- No module in `src/cotop` other than `colab_cli_calls.py` has an import statement for `google-colab-cli`, or writes `_session` (the attribute that holds the signed-in session) as an attribute or a string literal (`test_only_the_handoff_module_imports_colab_cli_or_touches_the_session`).
- A runtime's proxy token never follows a redirect (`test_the_proxy_token_never_follows_a_redirect`).
- From the CLI's history, cotop never shows code, outputs or prompt replies, and drops them as it reads each line, so a crash report can't print them either (`test_history_never_surfaces_code_outputs_or_stdin`, `test_the_activity_panel_shows_history_without_code`, `test_a_crash_report_never_shows_cli_history`).

### What cotop does in this release

- It sends GET requests through the CLI's signed-in session: to `colab.research.google.com` for the balance and the runtime list, and to Google's userinfo endpoint for your email address.
- It sends GET requests to each runtime's proxy, at the URL Colab returns for it, with that runtime's proxy token.
- It reads the CLI's history files in `~/.config/colab-cli/history/` and the CLI's session list.
- It writes learned rates and balance samples to `~/.cache/cotop/`.
- For a runtime the CLI started, it runs `colab stop -s NAME` or `colab restart-kernel -s NAME` when you press `s` or `R` and confirm, and `colab ssh -s NAME [-i KEY]` in your terminal when you press `x`.
- `cotop --demo` returns from `main()` before `sign_in` and runs on the made-up account in `src/cotop/demo.py`, with its cache in a temporary directory (`run_demo`).

[docs/data-sources.md](docs/data-sources.md) gives each request and how often cotop sends it.

### Calls into the Colab CLI

`src/cotop/colab_cli_calls.py` hands these calls to `google-colab-cli`:

- `sign_in` calls the CLI's `get_credentials()` at start. It returns a `Google`, which keeps the session private and offers two calls:
  - `Google.get` sends a GET through the session. When the session's access token expires, the session refreshes it with a POST.
  - `Google.unassign` calls the CLI client's `unassign`, to stop a runtime the CLI didn't start.
- `named_sessions` calls the CLI's `StateStore(path).list()`, once `~/.config/colab-cli/sessions.json` exists.
- `colab_argv` builds the `colab` command lines described above.

What these calls read, write and send inside the CLI is the CLI's behavior.

### What the CLI does (0.8.0; not a complete list)

These examples are taken from [google-colab-cli 0.8.0](https://pypi.org/project/google-colab-cli/0.8.0/):

- `get_credentials()` reads and writes the OS keyring. It moves an old plaintext `~/.config/colab-cli/token.json` into the keyring, and reads `~/.colab-cli-oauth-config.json` if that file exists.
- The three `colab` commands cotop runs log to `~/.config/colab-cli/colab.log` with secrets redacted, update files under `~/.config/colab-cli/`, and check `pypi.org` for an update at most once a day.
- `colab ssh` reads your SSH public key.
