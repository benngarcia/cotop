"""cotop: top for Google Colab."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Label, Static
from textual.worker import Worker, WorkerState

from cotop.colab import Colab
from cotop.model import (
    Account,
    Rate,
    RateBook,
    Resources,
    Runtime,
    Samples,
    Snapshot,
    UsageLog,
    fmt_duration,
    runtimes_from,
    utc_now,
)
from cotop.widgets import AMBER, DIM, ORANGE, PRESSURE, RED, WORK, Graph, TimeAxis, meter, sparkline

CACHE = Path("~/.cache/cotop").expanduser()
POLL_SECONDS = 15
SAMPLE_SECONDS = 2
LOW_RUNWAY = timedelta(hours=24)
ENDINGS = {"user_requested": "stopped", "pruned": "gone"}  # the CLI's reason codes, as people say them

THEME = Theme(
    name="cotop",
    primary=AMBER,
    secondary=ORANGE,
    accent=AMBER,
    warning=ORANGE,
    error=RED,
    success=AMBER,
    foreground="#D5DAE1",
    background="#101318",
    surface="#101318",
    panel="#1B2028",
    dark=True,
)


def gb(n: int) -> str:
    return f"{n / 1e9:.0f}G"


def clock(at: datetime) -> str:
    return f"{at.astimezone():%H:%M}"


class Usage(Vertical):
    """The account: what is left, how fast it goes, and the rate over the last day."""

    def __init__(self, log: UsageLog):
        super().__init__()
        self.usage_log = log

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(id="usage-text")
            yield Graph(WORK, source=self.rate_values, id="usage-graph")

    def rate_values(self, n: int) -> list[float | None]:
        series = self.usage_log.rate_series(utc_now(), n)
        top = max((r for r in series if r), default=0) or 1
        return [None if r is None else r / top for r in series]

    def show(self, s: Snapshot, log: UsageLog) -> None:
        self.border_title = s.account.email
        self.query_one("#usage-text", Static).update(account_text(s, log.burn(s.taken_at)))
        self.query_one("#usage-graph", Graph).refresh()
        self.border_subtitle = "rate over 24h"


def account_text(s: Snapshot, burn: float | None) -> Text:
    """What is left, how long it lasts, and what was really charged."""
    a, now = s.account, s.taken_at
    runway = a.runway
    low = runway is not None and runway < LOW_RUNWAY
    left = Text.assemble((f"{a.balance:,.2f}", f"bold {RED}" if low else "bold"), (" compute units left", RED if low else ""))
    if not (s.runtimes or a.assignments_count or a.rate_hourly):  # only when both reads say so
        pace = Text("nothing running", style=DIM)
    elif runway is None:
        pace = Text(f"{a.rate_hourly:.2f} an hour")
    else:
        pace = Text.assemble(f"{a.rate_hourly:.2f} an hour, ", runway_text(runway, now, low))
    charged = Text(f"charged {burn:.2f} an hour over the last hour" if burn is not None else "", style=DIM)
    return Text("\n").join([left, pace, charged])


def runway_text(runway: timedelta, now: datetime, low: bool) -> Text:
    """How long the balance lasts, and until when: a weekday names the day only within the week."""
    if runway <= timedelta(0):
        return Text("none left", style=f"bold {RED}")
    if (years := runway.days // 365) >= 1:
        return Text(f"lasts over {years} year{'s' if years > 1 else ''}")
    end = (now + runway).astimezone()
    return Text.assemble(
        "lasts ",
        (fmt_duration(runway), f"bold {RED}" if low else "bold"),
        (f" until {end:%a %H:%M}" if runway <= timedelta(days=6) else f" until {end:%b %-d}", DIM),
    )


class RuntimePanel(Vertical):
    """The selected runtime: its GPU graph, meters and what it costs.

    There is one panel, pointed at whichever runtime is selected, so changing
    the selection never tears widgets down while readings arrive.
    """

    def __init__(self, samples: Samples, **kwargs):
        super().__init__(**kwargs)
        self.samples = samples

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(classes="plot"):
                yield Static(classes="plot-label")
                yield Graph(WORK)
                yield TimeAxis()
            with Vertical(classes="side"):
                yield Static(classes="meters")
                yield Static(classes="facts")
                yield Static(classes="activity")

    def show(self, r: Runtime, s: Snapshot, rates: RateBook) -> None:
        now = s.taken_at
        latest = self.samples.latest(r.endpoint)
        self.border_title = f"{r.label}  {r.hardware}{' high-RAM' if r.shape == 'HIGH_RAM' else ''}"
        self.border_subtitle = state_text(r, now, latest).plain + (f"  {r.running}" if r.running else "")
        self.show_readings(r)

        rate = rates.rate(r, s)
        billed = r.billed_for(now)
        lines = []
        if r.error:
            lines.append(Text(f"unreachable\n{r.error}", style=RED))
        for concern in r.concerns(now, latest):
            billing = ", still billing" if concern.startswith("idle") else ""
            lines.append(Text(concern + billing, style=ORANGE))
        if billed:
            since = f"since {clock(r.assigned_at)}" if r.assigned_at else "since VM boot"
            lines.append(Text.assemble(("up ", DIM), time_text(billed), (f"  {since}", DIM)))
            if rate.hourly is not None:
                lines.append(Text.assemble(("spent ", DIM), spend_text(rate, billed), (f"  at {rate_text(rate)}/hr", DIM)))
        if r.status:
            lines.append(Text.assemble(("clients ", DIM), str(r.status.clients)))
        self.query_one(".facts", Static).update(Text("\n").join(lines))
        events = s.history.recent(r.name, limit=12) if r.name else []
        activity = Text("\n").join(Text.assemble((clock(e.at) + "  ", DIM), e.summary) for e in events)
        self.query_one(".activity", Static).update(activity or Text("no CLI activity", style=DIM))

    def show_readings(self, r: Runtime) -> None:
        latest = self.samples.latest(r.endpoint)
        graph = self.query_one(Graph)
        has_gpu = bool(latest and latest.gpus)
        metric = (lambda x: x.gpu_utilization) if has_gpu or latest is None else (lambda x: fraction(*x.ram))
        graph.gradient = WORK if has_gpu or latest is None else PRESSURE
        graph.values = self.samples.series(r.endpoint, metric)
        self.query_one(TimeAxis).time_of = partial(self.samples.time_of, r.endpoint)
        now_value = metric(latest) if latest else None
        name = latest.gpus[0].name if has_gpu else ("RAM" if latest else "GPU")
        label = Text.assemble((f"{now_value:.0%}" if now_value is not None else "–", "bold"), ("  " + name, DIM))
        self.query_one(".plot-label", Static).update(label)
        self.query_one(".meters", Static).update(meters(latest, width=14))


def fraction(used: int, total: int) -> float | None:
    """Colab's client treats every size as optional, so a total can be 0."""
    return used / total if total else None


def meters(res: Resources | None, width: int) -> Text:
    if res is None:
        return Text("waiting for readings", style=DIM)
    rows = []
    for label, (used, total), gradient, show_sizes in (
        ("vram", res.vram or (0, 0), WORK, True),
        ("ram ", res.ram, PRESSURE, True),
        ("disk", res.disk, PRESSURE, False),
    ):
        f = fraction(used, total)
        if f is not None:
            rows.append((label, f, f"{gb(used)}/{gb(total)}" if show_sizes else f"{f:.0%}", gradient))
    return Text("\n").join(
        Text.assemble((label + " ", DIM), meter(f, width, g), " " + value) for label, f, value, g in rows
    )


def rate_text(rate: Rate) -> str:
    if rate.hourly is None:
        return "?"
    return f"{'' if rate.exact else '≈'}{rate.hourly:.2f}"


def spend_text(rate: Rate, billed: tuple[timedelta, bool]) -> str:
    match rate.spend(billed):
        case (spent, mark):
            return f"{mark}{spent:.1f}"
    return "?"


def time_text(billed: tuple[timedelta, bool]) -> str:
    duration, exact = billed
    return ("" if exact else "≤") + fmt_duration(duration)


def state_text(r: Runtime, now: datetime, latest: Resources | None) -> Text:
    if r.error:
        return Text("unreachable", style=RED)
    concerns = r.concerns(now, latest)
    if concerns:
        return Text(concerns[0], style=ORANGE)
    if r.status is None:
        return Text("starting", style=DIM)
    if r.status.busy:
        return Text("busy", style=AMBER)
    idle = r.idle_for(now)
    return Text(f"idle {fmt_duration(idle)}" if idle else "idle", style=DIM)


class Runtimes(DataTable):
    """The list to pick from, most expensive first; the GPU column moves every two seconds."""

    COLUMNS = ("Name", "Hardware", "GPU", "CU/hr", "Spent", "State")
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]
    WIDE = 100  # below this many columns "Up" is dropped; the panel still shows it

    def on_mount(self) -> None:
        for c in self.COLUMNS:
            self.add_column(c, key=c)

    def fit(self, width: int) -> None:
        """Show "Up" only when the terminal is wide enough for every column."""
        has_up = "Up" in self.columns
        if width >= self.WIDE and not has_up:
            self.add_column("Up", key="Up")
        elif width < self.WIDE and has_up:
            self.remove_column("Up")

    def show(self, s: Snapshot, rates: RateBook, samples: Samples) -> None:
        rate_of = {r.endpoint: rates.rate(r, s) for r in s.runtimes}
        ordered = sorted(s.runtimes, key=lambda r: (rate_of[r.endpoint].hourly is None, -(rate_of[r.endpoint].hourly or 0), r.label))
        keys = [r.endpoint for r in ordered]
        if [row.value for row in self.rows] != keys:
            selected, row = self.selected_endpoint, self.cursor_row
            self.clear()
            for r in ordered:
                self.add_row(*["" for _ in self.columns], key=r.endpoint)
            # A selection that vanished stays on its row, not row 0; the cursor clamps to the table.
            self.move_cursor(row=keys.index(selected) if selected in keys else row)
        self.border_title = f"Runtimes ({len(keys)})" if keys else "Runtimes"
        now = s.taken_at
        for r in ordered:
            rate = rate_of[r.endpoint]
            billed = r.billed_for(now)
            cells = {
                "Name": Text(r.label, style="" if r.name else DIM),
                "Hardware": r.hardware + (" hi-RAM" if r.shape == "HIGH_RAM" else ""),
                "CU/hr": rate_text(rate),
                "Spent": spend_text(rate, billed) if billed else "?",
                "Up": time_text(billed) if billed else "?",
                "State": state_text(r, now, samples.latest(r.endpoint)),
            }
            for column, value in cells.items():
                if column in self.columns:
                    self.update_cell(r.endpoint, column, value)
        self.show_readings(s, samples)

    def show_readings(self, s: Snapshot, samples: Samples) -> None:
        for r in s.runtimes:
            latest = samples.latest(r.endpoint)
            util = latest.gpu_utilization if latest else None
            cell = sparkline(samples.series(r.endpoint, lambda x: x.gpu_utilization), 8)
            cell.append(f" {util:4.0%}" if util is not None else "    –")
            self.update_cell(r.endpoint, "GPU", cell)

    @property
    def selected_endpoint(self) -> str | None:
        if not self.row_count:
            return None
        return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value


class Confirm(ModalScreen[bool]):
    """A yes/no question; No is focused, so Enter alone never stops a VM."""

    CSS = """
    Confirm { align: center middle; }
    #dialog { width: 64; height: auto; border: round $warning; padding: 1 2; background: $panel; }
    #dialog Label { width: 100%; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    """
    BINDINGS = [("y", "answer(True)", "Yes"), ("n,escape", "answer(False)", "No")]

    def __init__(self, question: str, yes: str):
        super().__init__()
        self.question, self.yes = question, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            with Horizontal(id="buttons"):
                yield Button(f"{self.yes} (y)", variant="error", id="yes")
                yield Button("Cancel (n)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class HistoryScreen(Screen):
    """Past sessions and what they cost; the account's eligible hardware."""

    BINDINGS = [Binding("h,escape", "app.pop_screen", "Back")]

    def __init__(self, s: Snapshot, rates: RateBook):
        super().__init__()
        self.snapshot, self.rates = s, rates

    def compose(self) -> ComposeResult:
        s = self.snapshot
        table = DataTable(cursor_type="row", zebra_stripes=True, classes="panel")
        table.border_title = "Sessions started with the colab CLI"
        yield table
        a = s.account
        eligible = Static(
            Text.assemble(("GPU  ", DIM), " ".join(a.eligible_gpus) or "none", ("    TPU  ", DIM), " ".join(a.eligible_tpus) or "none"),
            classes="panel",
        )
        eligible.border_title = "Hardware this account can use"
        yield eligible
        yield Footer()

    def on_mount(self) -> None:
        s = self.snapshot
        table = self.query_one(DataTable)
        table.add_columns("Name", "Hardware", "Started", "Lasted", "CU", "Ended")
        live = {r.endpoint for r in s.runtimes}
        for p in s.history.past_sessions():
            run = p.lasted(s.taken_at, live)
            ended = ENDINGS.get(p.reason, p.reason) if p.reason else ("running" if p.endpoint in live else "unknown")
            table.add_row(
                p.name,
                p.kind.replace("/STANDARD", "").replace("/HIGH_RAM", " hi-RAM"),
                f"{p.created.astimezone():%b %-d %H:%M}",
                time_text(run) if run else "?",
                spend_text(self.rates.learned(p.kind), run) if run else "?",
                ended,
            )


class Dashboard(App):
    TITLE = "cotop"
    CSS = """
    Screen { layout: vertical; }
    .panel, Usage, RuntimePanel, Runtimes {
        border: round $panel-lighten-2; border-title-color: $foreground; border-subtitle-color: $text-muted;
        padding: 0 1; background: $background;
    }
    Usage { height: 5; }
    Usage Horizontal { height: 3; }
    #usage-text { width: 46; }
    #usage-graph { height: 3; }
    Runtimes { height: auto; max-height: 45%; }
    Runtimes:focus { border: round $accent; }
    Runtimes > .datatable--cursor, Runtimes:focus > .datatable--cursor { background: $panel-lighten-1; text-style: bold; }
    Runtimes > .datatable--header { background: $background; color: $text-muted; }
    RuntimePanel { height: 1fr; min-height: 7; }
    RuntimePanel Horizontal { height: 1fr; }
    RuntimePanel .plot { width: 1fr; }
    RuntimePanel .plot-label { height: 1; }
    RuntimePanel .side { width: 34; padding-left: 2; }
    RuntimePanel .meters { height: auto; margin-bottom: 1; }
    RuntimePanel .facts { height: auto; margin-bottom: 1; }
    RuntimePanel .activity { height: 1fr; }
    #empty { height: 1fr; content-align: center middle; color: $text-muted; display: none; }
    HistoryScreen DataTable { height: 1fr; }
    HistoryScreen .panel { height: auto; }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("s", "stop", "Stop"),
        ("R", "restart_kernel", "Restart kernel"),
        ("x", "ssh", "SSH"),
        ("h", "history", "History"),
        Binding("question_mark", "keys", "Keys"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, colab: Colab, cache: Path):
        super().__init__()
        self.colab = colab
        self.rates = RateBook(cache / "rates.json")
        self.usage = UsageLog(cache / "usage.jsonl")
        self.samples = Samples()
        self.email: str | None = None
        self.snapshot: Snapshot | None = None
        self.polling: Worker | None = None
        self.refresh_pending = False  # asked for while a poll ran, which may predate the change
        self.sampling: Worker | None = None
        self.unsaved_reported = False
        self.acting: set[str] = set()  # endpoints with a stop or kernel restart not yet returned

    def compose(self) -> ComposeResult:
        yield Usage(self.usage)
        yield Runtimes(cursor_type="row", cursor_foreground_priority="renderable")
        yield RuntimePanel(self.samples)
        yield Static("No runtimes. Start one with colab new.", id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "cotop"
        self.query_one(Runtimes).border_title = "Runtimes"
        self.query_one(Runtimes).fit(self.size.width)
        self.action_refresh()
        self.set_interval(POLL_SECONDS, self.action_refresh)
        self.set_interval(SAMPLE_SECONDS, self.action_sample)

    # Polling: the account and kernel status every 15 s, resources every 2 s.

    def action_refresh(self) -> None:
        self.refresh_pending = bool(self.polling and self.polling.is_running)
        if not self.refresh_pending:
            self.polling = self.poll()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self.polling and event.state == WorkerState.SUCCESS and self.refresh_pending:
            self.action_refresh()

    def action_sample(self) -> None:
        if self.snapshot and not (self.sampling and self.sampling.is_running):
            self.sampling = self.sample(self.snapshot.runtimes)

    @work(thread=True)
    def poll(self) -> None:
        try:
            self.email = self.email or self.colab.email()
            account = Account.parse(self.email, self.colab.ccu_info())
            assignments = self.colab.assignments()
            history = self.colab.history()
            status = self.colab.status(assignments)
            runtimes = runtimes_from(assignments, self.colab.sessions(), status, history.assigned_at())
            snapshot = Snapshot(utc_now(), account, runtimes, history)
        except Exception as e:
            self.call_from_thread(self.show_error, e)
            return
        self.call_from_thread(self.show, snapshot)

    @work(thread=True)
    def sample(self, runtimes: list[Runtime]) -> None:
        at = utc_now()
        readings = self.colab.resources(runtimes)
        self.call_from_thread(self.add_readings, at, readings)

    def add_readings(self, at: datetime, readings: dict[str, Resources | None]) -> None:
        for endpoint, reading in readings.items():
            self.samples.add(endpoint, at, reading)
        if self.snapshot:
            if r := self.selected_runtime():
                self.query_one(RuntimePanel).show_readings(r)
            self.query_one(Runtimes).show_readings(self.snapshot, self.samples)

    # Showing.

    def show(self, s: Snapshot) -> None:
        self.snapshot = s
        self.usage.record(s.taken_at, s.account.balance, s.account.rate_hourly)
        self.rates.observe(s)
        self.samples.keep_only({r.endpoint for r in s.runtimes})
        self.query_one(Usage).show(s, self.usage)
        # The table stays visible when empty: hiding a focused widget blurs it, and
        # nothing would give the selection's only home its focus back.
        table = self.query_one(Runtimes)
        table.show(s, self.rates, self.samples)
        self.show_selected()
        self.report_unsaved()

    def report_unsaved(self) -> None:
        """Once each time saving starts to fail: the dashboard keeps working from memory."""
        unsaved = self.usage.unsaved or self.rates.unsaved
        if unsaved and not self.unsaved_reported:
            message = f"Can't save to {unsaved.filename or 'the cache'}: {unsaved.strerror or unsaved}. Usage history and learned rates won't outlast this run."
            self.notify(message, severity="warning", timeout=POLL_SECONDS, markup=False)
        self.unsaved_reported = unsaved is not None

    def show_selected(self) -> None:
        """Point the panel at the runtime under the table's cursor, the selection's only home."""
        r = self.selected_runtime()
        panel = self.query_one(RuntimePanel)
        panel.display = r is not None
        self.query_one("#empty").display = bool(self.snapshot) and not self.snapshot.runtimes
        if r:
            panel.show(r, self.snapshot, self.rates)

    def on_resize(self, event: events.Resize) -> None:
        table = self.query_one(Runtimes)
        table.fit(event.size.width)
        if self.snapshot:
            table.show(self.snapshot, self.rates, self.samples)

    def show_error(self, e: Exception) -> None:
        age = f" Showing data from {fmt_duration(utc_now() - self.snapshot.taken_at)} ago." if self.snapshot else ""
        self.notify(f"Refresh failed: {type(e).__name__}: {e}.{age}", severity="error", timeout=POLL_SECONDS, markup=False)

    # Selection and actions.

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if isinstance(event.data_table, Runtimes) and self.snapshot:
            self.show_selected()

    def selected_runtime(self) -> Runtime | None:
        endpoint = self.query_one(Runtimes).selected_endpoint
        if not self.snapshot or endpoint is None:
            return None
        return next((r for r in self.snapshot.runtimes if r.endpoint == endpoint), None)

    def target(self, named_to: str | None = None) -> Runtime | None:
        """The selected runtime, if it can take an action now; named_to: a verb that needs a CLI name."""
        r = self.selected_runtime()
        if r and r.endpoint in self.acting:  # a second action would race the first
            self.notify(f"Still waiting for the last action on {r.name or r.endpoint}.", severity="warning")
        elif r and named_to and r.name is None:
            self.notify(f"Can only {named_to} runtimes started with the colab CLI.", severity="warning")
        else:
            return r
        return None

    def action_stop(self) -> None:
        if not (r := self.target()):
            return
        busy = f" It is busy running {r.running}." if r.status and r.status.busy and r.running else ""
        question = f"Stop {r.name or r.endpoint} ({r.hardware})? Its VM and files will be deleted.{busy}"
        self.push_screen(Confirm(question, "Stop"), lambda yes: yes and self.act(self.colab.stop, r))

    def action_restart_kernel(self) -> None:
        if not (r := self.target(named_to="restart the kernel of")):
            return
        busy = " Its running code is interrupted." if r.status and r.status.busy else ""
        question = f"Restart {r.name}'s kernel? Everything in memory is lost; files stay.{busy}"
        self.push_screen(Confirm(question, "Restart"), lambda yes: yes and self.act(self.colab.restart_kernel, r))

    def action_ssh(self) -> None:
        """Check the name and open the shell in one step on the event loop, so nothing runs in between."""
        if not (r := self.target(named_to="ssh into")):
            return
        try:
            argv = self.colab.ssh_command(r)
        except Exception as e:
            self.notify(f"{r.name}: {e}", severity="error", timeout=10)
        else:
            with self.suspend():  # blocks the event loop: no timer redraws behind the shell, no key acts until it closes
                code = subprocess.run(argv).returncode
                if code != 0:  # keep colab's error on screen; resuming would paint over it
                    input(f"\ncolab ssh exited {code}. Press Enter to return to cotop.")
        self.action_refresh()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Runtime keys work only on the dashboard, where you can see the runtime they act on."""
        if action in {"stop", "restart_kernel", "ssh", "history"} and self.screen is not self.screen_stack[0]:
            return False
        return True

    def action_keys(self) -> None:
        """Toggle Textual's panel of every key that works here."""
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_history(self) -> None:
        if self.snapshot:
            self.push_screen(HistoryScreen(self.snapshot, self.rates))

    def act(self, action, runtime: Runtime) -> None:
        self.acting.add(runtime.endpoint)  # here on the event loop, before the next key is read
        self.send(action, runtime)

    @work(thread=True)
    def send(self, action, runtime: Runtime) -> None:
        label = runtime.name or runtime.endpoint
        self.call_from_thread(self.notify, f"{action.__name__.replace('_', ' ')} {label}…")
        try:
            result = action(runtime)
        except Exception as e:
            self.call_from_thread(self.notify, f"{label}: {e}", severity="error", timeout=10)
        else:
            self.call_from_thread(self.notify, f"{label}: {result}")
        finally:
            self.call_from_thread(self.returned, runtime)

    def returned(self, runtime: Runtime) -> None:
        self.acting.discard(runtime.endpoint)
        self.action_refresh()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--identity", help="SSH key for x (passed to colab ssh -i)")
    parser.add_argument("--demo", action="store_true", help="run on a made-up heavy account: no sign-in, nothing touched")
    args = parser.parse_args()
    if args.demo:
        run_demo()
        return
    try:
        colab = Colab.sign_in(ssh_identity=args.identity)  # before the UI: sign-in may prompt in the terminal
    except Exception as e:
        sys.exit(f"cotop: could not sign in with the colab CLI's credentials: {e}")
    Dashboard(colab, CACHE).run()


def demo_dashboard(cache: Path) -> Dashboard:
    """The dashboard on the demo account, its graphs already full."""
    from cotop.demo import STEP, DemoColab, seed_cache

    colab = DemoColab()
    seed_cache(cache, colab.start)
    app = Dashboard(colab, cache=cache)
    for endpoint, readings in colab.backfill().items():
        for i, reading in enumerate(readings):
            app.samples.add(endpoint, colab.start - STEP * (len(readings) - i), reading)
    return app


def run_demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="cotop-demo-") as cache:
        demo_dashboard(Path(cache)).run()


if __name__ == "__main__":
    main()
