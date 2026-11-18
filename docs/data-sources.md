# Where cotop's numbers come from

cotop polls the account, the runtime list and each runtime's kernels at a
15-second interval, on `r` and after each action, and each runtime's resources
at a 2-second interval for the graphs. In this release those requests are GETs. The Colab
CLI's signed-in session refreshes its access token with a POST when it expires.
Stop sends a POST: the colab CLI's own `colab stop`, or for a runtime the CLI
didn't start, the CLI client's `unassign` call. The README's
[What cotop can touch](../README.md#what-cotop-can-touch) lists the guarantees
and the calls cotop makes into the CLI.

`cotop --demo` runs on `src/cotop/demo.py`, a made-up account, and sends no
requests at all.

| Panel | Source | Notes |
|---|---|---|
| Account email | `openidconnect.googleapis.com/v1/userinfo` | Signed in with the Colab CLI's OAuth token from the OS keyring |
| Balance, rate, eligible hardware | `colab.research.google.com/tun/m/ccu-info` | The same call as `colab usage`. The CLI drops `eligibleGpus` and `eligibleTpus`; cotop reads the raw body |
| Runtimes | `/tun/m/assignments` | Every VM the account holds, including ones opened in a browser. These show as *untracked* |
| Runtime names, running command | `~/.config/colab-cli/sessions.json` | Read under the CLI's file lock and joined on `endpoint`. `colab sessions` and `colab status` prune and rewrite this file, and `colab stop`, which cotop runs on `s`, updates it |
| Kernel state, idle time, clients | Runtime proxy `/api/kernels`, `/api/status` | Requests carry `?no_track_activity=1` |
| GPU, VRAM, RAM, disk | Runtime proxy `/api/colab/resources`, every 2 s | The same endpoint the Colab VS Code extension uses. The body has an XSSI prefix. Values change every second; a read takes about 0.4 s. A read that fails or takes over 3 s is a gap in the graph, never a zero |
| Assignment time, recent events, past sessions | `~/.config/colab-cli/history/*.jsonl` | Code, outputs and prompt replies are not shown; see the README's guarantees |

## Estimates

Colab reports only the account's total hourly rate, never a per-runtime rate.
The `CU/hr` column is therefore:

- **exact** when there is one runtime;
- **≈ split** when every runtime is the same hardware: total ÷ count;
- **≈ learned** from `~/.cache/cotop/rates.json`, which records each hardware kind's rate
  whenever only that kind is running;
- **?** otherwise.

Colab boots VMs before it assigns them. One G4 runtime booted 21 minutes
before `colab new` received it. Uptime and spend therefore count from the
assignment time in the CLI history. For a runtime the CLI did not start,
the VM's boot time is the only start time available. That figure is an upper
bound, shown as `≤`.

Colab settles the balance in steps a few minutes apart, so a sample taken
every 15 seconds shows no change for minutes at a time. "Charged last hour" is
the fall in balance between the first and last settled step in the past hour,
logged in `~/.cache/cotop/usage.jsonl`. You can compare it with the rate
Colab reports.

## Watching did not keep an idle runtime alive

Tested on 2026-11-17 with two idle CPU runtimes started with `colab new`, neither
with a kernel. One was read the way cotop reads a runtime: the same endpoints
and flag, its resources about every 2 seconds and its kernels about every 16 to
20 seconds. The other was never touched. Colab reclaimed both in the same
one-minute check, 3 hours 1 minute after they started.

The read requests carry `no_track_activity=1`, and Jupyter honors it: the
runtime's `last_activity` did not move after flagged reads, and did move after
the same reads without the flag. Colab does not document what it counts as
idle activity.

## Not verified

- That billing starts at assignment rather than at VM boot.
- Whether Colab reclaimed these runtimes for being idle or at a fixed time
  limit near 3 hours. The test had no runtime that stayed active, so it cannot
  tell the two apart.
- Runtimes with an idle kernel, and GPU or TPU runtimes. The test covered CPU
  runtimes without a kernel only.

## Actions

| Key | Runs | Notes |
|---|---|---|
| `s` | `colab stop -s NAME` | For an untracked runtime, the CLI's `Client.unassign(endpoint)`, because no name exists |
| `R` | `colab restart-kernel -s NAME` | Only for runtimes the CLI started |
| `x` | `colab ssh -s NAME [-i KEY]` | The dashboard suspends while the shell runs. A failed connection waits for Enter so its error stays readable |

The CLI resolves names, not endpoints. Before a named action, cotop reads
the assignments and the session file again. It acts only if the name still
points at the same live VM. A name that was reused since the last poll would
otherwise point at another VM. `colab ssh` also creates a new runtime for a
name it does not know.
