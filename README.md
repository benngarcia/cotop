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

| | What | When |
|---|---|---|
| Reads | Your Colab CLI sign-in, through the CLI's own `get_credentials`. If the token has expired, the CLI refreshes it and saves it back to the OS keyring. If there is no token, the CLI asks you to sign in | At start |
| Reads | `~/.config/colab-cli/sessions.json` (under the CLI's lock) and `~/.config/colab-cli/history/*.jsonl` | Every 15 s |
| Sends GET | `colab.research.google.com` (balance, runtime list), Google's userinfo endpoint (your email address), and each runtime's proxy (kernels, status, resources) | Every 15 s; resources every 2 s |
| Writes | `~/.cache/cotop/rates.json` and `~/.cache/cotop/usage.jsonl`, and nothing else | When a rate or the balance changes |
| Runs | `colab stop -s NAME` or `colab restart-kernel -s NAME` | After you press `s` or `R` and then confirm |
| Sends POST | The CLI client's `unassign`, for a runtime the CLI didn't start | After you press `s` and then confirm |
| Runs | `colab ssh -s NAME [-i KEY]` in your terminal | When you press `x` |

- cotop sends requests only to Google, and never shows or stores a token.
- From the CLI's history it shows only event types, times and file names: never code, outputs or anything typed at a prompt.
- Before it runs a CLI command on a name, cotop checks that the name still points at the same runtime.
- `cotop --demo` touches none of the above: it sends no requests and keeps its cache in a temporary directory.

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
