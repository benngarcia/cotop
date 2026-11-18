# cotop

`top` for Google Colab: compute units, runtimes and what they cost, live in your terminal.

![cotop --demo: moving through twelve runtimes with j, each one's live GPU graph, the ? keys panel, past sessions with h, and the stop confirmation](docs/images/demo.gif)

- **Your account:** compute units left, the hourly rate, when you run out, and the rate over the last day. Compute units left turn red when they run out within a day.
- **Runtimes:** every VM on your account, most expensive first, including ones opened in a browser, with a live GPU sparkline, rate, spend, uptime and state. A runtime idle while it bills, out of RAM or disk, or unreachable says so in its row.
- **The selected runtime:** a braille graph of GPU utilization, read every two seconds, with VRAM, RAM and disk meters, what it has cost and its recent activity. On a runtime without a GPU, the graph shows RAM.
- **History:** press `h` for past sessions, how long each ran and roughly what it cost.
- **Actions:** stop a runtime, restart its kernel, or open a shell on it.

cotop works on top of the official [Colab CLI](https://pypi.org/project/google-colab-cli/). It uses the CLI's sign-in, session names and history, so runtimes you start with `colab new` show up by name.

## Install

```sh
uv tool install cotop
cotop
cotop --demo   # a made-up account with twelve runtimes; no sign-in
```

cotop needs Python 3.12 or later. It is tested on macOS and should work on Linux. If you have not signed in to the Colab CLI yet, cotop runs the same Google sign-in on first launch. On macOS, Keychain may ask once whether cotop's Python can read the Colab CLI's token.

## Keys

| Key | Action |
|---|---|
| `↑` `↓` or `k` `j` | Select a runtime |
| `h` | Past sessions and the hardware your account can use |
| `r` | Refresh now (cotop also refreshes every 15 seconds) |
| `s` | Stop the selected runtime |
| `R` | Restart its kernel |
| `x` | Open a shell on it with `colab ssh` |
| `?` | Show every key |
| `q` | Quit |

![The stop confirmation, with Cancel selected](docs/images/stop.png)

Stop and restart ask first, and Cancel has focus. `colab ssh` looks for `~/.ssh/id_ed25519.pub` or `~/.ssh/id_ecdsa.pub`. For another key, start cotop with `cotop -i ~/.ssh/KEY.pub`. Restart and ssh work only on runtimes started with the Colab CLI. Stop works on any runtime.

## Which numbers are estimates

Colab reports one hourly rate for the whole account, not one per runtime. cotop shows a per-runtime rate as follows:

- With one runtime, the rate is exact.
- With several runtimes on the same hardware, cotop splits the total evenly.
- Otherwise cotop uses a rate it learned earlier, when only that hardware was running. These rates are stored in `~/.cache/cotop/rates.json`.
- With no learned rate, the column shows `?`.

Estimates start with `≈`, and upper bounds start with `≤`. Colab boots VMs before it assigns them, so uptime and spend count from the assignment time in the CLI's history. A runtime opened in a browser has no assignment time, so cotop counts from VM boot and shows an upper bound.

[docs/data-sources.md](docs/data-sources.md) lists each request cotop makes and what it reads.

## What cotop can touch

cotop signs in with the Colab CLI's own credentials, so it can do anything `colab` can, with the access you granted when you signed in. It adds no credentials of its own.

### Guarantees

Each of these holds because a test on `main` fails without it:

- Before it stops, restarts or opens a shell on a named runtime, cotop checks that the name still points at the runtime you selected (`test_every_named_action_checks_the_name_still_means_this_runtime`).
- Stop, restart and ssh run exactly `colab stop -s NAME`, `colab restart-kernel -s NAME` and `colab ssh -s NAME [-i KEY]`, unchanged (`test_cotop_runs_exactly_these_command_lines`, `test_ssh_runs_the_command_line_unchanged`).
- Only `src/cotop/colab_cli_calls.py` imports `google-colab-cli`, and no other module reads `Google._session`, the signed-in session (`test_only_the_handoff_module_imports_colab_cli_or_touches_the_session`).
- A runtime's proxy token never follows a redirect (`test_the_proxy_token_never_follows_a_redirect`).
- From the CLI's history, cotop never shows code, outputs or prompt replies (`test_history_never_surfaces_code_outputs_or_stdin`).
- `cotop --demo` signs in to nothing, connects to nothing, and leaves `~/.cache/cotop` alone (`test_the_demo_signs_in_to_nothing_and_leaves_the_cache_alone`, `test_the_demo_connects_to_nothing_and_runs_nothing`).

### What cotop does in this release

- It sends GET requests through the CLI's signed-in session: to `colab.research.google.com` for the balance and the runtime list, and to Google's userinfo endpoint for your email address.
- It sends GET requests to each runtime's proxy, at the URL Colab returns for it, with that runtime's proxy token.
- It reads the CLI's history files in `~/.config/colab-cli/history/` and the CLI's session list.
- It writes learned rates and balance samples to `~/.cache/cotop/`.
- It runs the `colab` commands above when you press `s` or `R` and confirm, or when you press `x`.

[docs/data-sources.md](docs/data-sources.md) gives each request and how often cotop sends it.

### Calls into the Colab CLI

`src/cotop/colab_cli_calls.py` hands these calls to `google-colab-cli`:

- `sign_in` calls the CLI's `get_credentials()` at start. It returns a `Google`, which keeps the session private and offers two calls:
  - `Google.get` sends a GET through the session. When the session's access token expires, the session refreshes it with a POST.
  - `Google.unassign` calls the CLI client's `unassign`, to stop a runtime the CLI didn't start.
- `named_sessions` calls the CLI's `StateStore(path).list()`, once `~/.config/colab-cli/sessions.json` exists.
- `colab_argv` builds the command lines above.

What these calls read, write and send inside the CLI is the CLI's behavior.

### What the CLI does (0.8.0; not a complete list)

These examples are taken from [google-colab-cli 0.8.0](https://pypi.org/project/google-colab-cli/0.8.0/):

- `get_credentials()` reads and writes the OS keyring. It moves an old plaintext `~/.config/colab-cli/token.json` into the keyring, and reads `~/.colab-cli-oauth-config.json` if that file exists.
- The three `colab` commands cotop runs log to `~/.config/colab-cli/colab.log` with secrets redacted, update files under `~/.config/colab-cli/`, and check `pypi.org` for an update at most once a day.
- `colab ssh` reads your SSH public key.

## Not affiliated with Google

cotop is an independent project. It relies on internal parts of `google-colab-cli` and on Colab endpoints that are not public APIs. Either can change without notice. cotop pins the Colab CLI's minor version for that reason.

## Development

```sh
uv sync
uv run pytest
uv run python scripts/screenshots.py OUT_DIR   # README screenshots, rendered from the demo account
```

To release, set `version` in `pyproject.toml`, then push a matching tag such as `v0.1.0`. The [publish workflow](.github/workflows/publish.yml) builds the package and uploads it to PyPI with trusted publishing.

## License

[Apache 2.0](LICENSE)
