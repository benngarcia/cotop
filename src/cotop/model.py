"""What a Colab account looks like at one moment: compute units and runtimes.

Every value here is parsed from a raw Colab response, so the parsing is the
single place that knows the wire format (XSSI prefix, camelCase, fractions).
A value of the wrong type raises there, never later on the screen; the types
follow Colab's own client, colab-vscode (src/colab/client/v1/api.ts).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from itertools import pairwise
from pathlib import Path

XSSI_PREFIX = ")]}'\n"
SHAPES = {0: "STANDARD", 1: "HIGH_RAM"}
LONGEST_RUNWAY = timedelta(days=365 * 1000)  # a near-zero rate is a real account; this keeps its runway a timedelta


def parse_json(body: str):
    """NaN and Infinity are not JSON, though Python's json reads them; here they raise."""
    return json.loads(body.removeprefix(XSSI_PREFIX), parse_constant=_not_json)


def _not_json(constant: str):
    raise ValueError(f"{constant} is not a JSON number")


def parse_time(value: str) -> datetime:
    """A time with a zone; one without can't be compared with now."""
    at = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    if at is None or at.tzinfo is None:
        raise ValueError(f"expected a time with a zone, got {value!r:.40}")
    return at


def is_number(value) -> bool:
    """A number arithmetic works on: json reads true as a bool, which Python counts as an int,
    1e999 as infinity, and big integers exactly."""
    match value:
        case bool():
            return False
        case int() | float():
            return abs(value) <= sys.float_info.max
    return False


def number(value) -> float:
    if not is_number(value):
        raise ValueError(f"expected a number, got {value!r:.40}")
    return value


def whole(value) -> int:
    """A count or a size in bytes: Colab's int64s, sent as JSON numbers."""
    match value:
        case bool():
            pass
        case int() if 0 <= value < 2**63:
            return value
    raise ValueError(f"expected a whole number, got {value!r:.40}")


def string(value) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r:.40}")
    return value


def strings(value) -> tuple[str, ...]:
    match value:
        case [*items] if all(isinstance(i, str) for i in items):
            return tuple(items)
    raise ValueError(f"expected a list of strings, got {value!r:.40}")


def write_atomic(path: Path, text: str) -> OSError | None:
    """Replace path in one step, so a reader never sees half a file, even with two cotops running.

    A failed write (a full disk, an unwritable cache) is returned, not raised: cotop
    keeps working from memory, and the dashboard says once that it can't save.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name, suffix=".tmp", delete_on_close=False) as f:
            f.write(text)
            f.close()
            os.replace(f.name, path)  # if this or the write fails, leaving the block deletes the temp file
    except OSError as e:
        return e
    return None


def read_cache(path: Path):
    """A cache file's JSON, or None if it is missing, unreadable or damaged; cotop rebuilds what it loses."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Account:
    email: str
    balance: float
    rate_hourly: float
    assignments_count: int
    eligible_gpus: tuple[str, ...]
    eligible_tpus: tuple[str, ...]

    @classmethod
    def parse(cls, email: str, ccu: dict) -> Account:
        """Numbers stay numbers: colab-vscode reads these as z.number(), so a string is an error, not a value."""
        return cls(
            email=string(email),
            balance=number(ccu["currentBalance"]),
            rate_hourly=number(ccu["consumptionRateHourly"]),
            assignments_count=whole(ccu["assignmentsCount"]),
            eligible_gpus=strings(ccu.get("eligibleGpus", [])),
            eligible_tpus=strings(ccu.get("eligibleTpus", [])),
        )

    @property
    def runway(self) -> timedelta | None:
        """How long the balance lasts at this rate, from none left up to LONGEST_RUNWAY; None while it isn't falling."""
        if self.rate_hourly <= 0:
            return None
        hours = self.balance / self.rate_hourly  # inf for a near-zero rate
        return timedelta(hours=min(max(hours, 0), LONGEST_RUNWAY / timedelta(hours=1)))


class KernelState(Enum):
    """A Jupyter kernel's execution_state, the values jupyter_server sets, most active first."""

    BUSY = "busy"
    STARTING = "starting"
    RESTARTING = "restarting"
    AUTORESTARTING = "autorestarting"  # it died, and Jupyter is starting it again
    IDLE = "idle"
    DEAD = "dead"


@dataclass(frozen=True)
class Kernel:
    state: KernelState
    last_activity: datetime
    connections: int


@dataclass(frozen=True)
class Gpu:
    name: str
    utilization: float | None  # None when Colab left it out: a gap, not an idle GPU
    memory_used: int
    memory_total: int

    @classmethod
    def parse(cls, gpu: dict) -> Gpu:
        return cls(
            string(gpu.get("name", "GPU")),
            utilization(gpu.get("gpuUtilization")),
            whole(gpu.get("memoryUsedBytes", 0)),
            whole(gpu.get("memoryTotalBytes", 0)),
        )


def utilization(value) -> float | None:
    """A fraction from 0 to 1, as colab-vscode documents gpuUtilization; 85 is not 85%, it is an error."""
    if value is None or (is_number(value) and 0 <= value <= 1):
        return value
    raise ValueError(f"expected a utilization from 0 to 1, got {value!r:.40}")


@dataclass(frozen=True)
class Resources:
    """One reading of a runtime's memory, disk and GPUs; cotop takes one every two seconds."""

    at: datetime
    memory_total: int
    memory_free: int
    disk_total: int
    disk_used: int
    gpus: tuple[Gpu, ...]

    @classmethod
    def parse(cls, at: datetime, resources: dict) -> Resources:
        """Sizes are optional and default to 0, as in Colab's own client (colab-vscode);
        a missing utilization stays None, so it draws as a gap rather than an idle GPU.
        A size that is there must be a whole number: a null or a string fails the reading."""
        memory = resources.get("memory", {})
        disks = [d.get("filesystem", {}) for d in resources.get("disks", [])]
        return cls(
            at=at,
            memory_total=whole(memory.get("totalBytes", 0)),
            memory_free=whole(memory.get("freeBytes", 0)),
            disk_total=sum(whole(d.get("totalBytes", 0)) for d in disks),
            disk_used=sum(whole(d.get("usedBytes", 0)) for d in disks),
            gpus=tuple(Gpu.parse(g) for g in resources.get("gpus", [])),
        )

    @property
    def gpu_utilization(self) -> float | None:
        """Mean over the GPUs that reported one; None on a CPU or TPU runtime, or if none did."""
        known = [g.utilization for g in self.gpus if g.utilization is not None]
        return sum(known) / len(known) if known else None

    @property
    def vram(self) -> tuple[int, int] | None:
        total = sum(g.memory_total for g in self.gpus)
        return (sum(g.memory_used for g in self.gpus), total) if total else None

    @property
    def ram(self) -> tuple[int, int]:
        return self.memory_total - self.memory_free, self.memory_total

    @property
    def disk(self) -> tuple[int, int]:
        return self.disk_used, self.disk_total


class Samples:
    """Recent resource readings per runtime. A failed reading is a gap (None), never a zero."""

    KEEP = 900  # 30 minutes at one reading every two seconds

    def __init__(self):
        self.readings: dict[str, deque[tuple[datetime, Resources | None]]] = {}

    def add(self, endpoint: str, at: datetime, reading: Resources | None) -> None:
        self.readings.setdefault(endpoint, deque(maxlen=self.KEEP)).append((at, reading))

    def latest(self, endpoint: str) -> Resources | None:
        for _, reading in reversed(self.readings.get(endpoint, ())):
            if reading is not None:
                return reading
        return None

    def series(self, endpoint: str, metric) -> list[float | None]:
        """metric(reading) for each reading, oldest first; gaps, and readings it has no value for, are None."""
        return [metric(r) if r is not None else None for _, r in self.readings.get(endpoint, ())]

    def time_of(self, endpoint: str, n: int) -> datetime | None:
        """When the n-th newest reading was taken, or the oldest if there are fewer."""
        readings = self.readings.get(endpoint)
        return readings[-min(n, len(readings))][0] if readings else None

    def keep_only(self, endpoints: set[str]) -> None:
        for gone in self.readings.keys() - endpoints:
            del self.readings[gone]


@dataclass(frozen=True)
class Status:
    """What the runtime's Jupyter server says about its kernels; a state it doesn't list fails the parse."""

    started: datetime
    kernels: tuple[Kernel, ...]

    @classmethod
    def parse(cls, status: dict, kernels: list) -> Status:
        return cls(
            started=parse_time(status["started"]),
            kernels=tuple(
                Kernel(KernelState(k["execution_state"]), parse_time(k["last_activity"]), whole(k["connections"]))
                for k in kernels
            ),
        )

    @property
    def state(self) -> KernelState:
        """The most active of its kernels' states; idle with no kernel."""
        return min((k.state for k in self.kernels), key=list(KernelState).index, default=KernelState.IDLE)

    @property
    def clients(self) -> int:
        return sum(k.connections for k in self.kernels)

    def idle_for(self, now: datetime) -> timedelta | None:
        """How long every kernel has been idle; None while one is doing anything else, or is dead."""
        if self.state is not KernelState.IDLE:
            return None
        if not self.kernels:
            return now - self.started
        return now - max(k.last_activity for k in self.kernels)

    def uptime(self, now: datetime) -> timedelta:
        return now - self.started


@dataclass(frozen=True)
class Unreachable:
    """Why the runtime's Jupyter server could not be read, as text: the exception's traceback holds the proxy token."""

    error: str


@dataclass(frozen=True)
class Runtime:
    """One Colab assignment: a VM the account is paying for."""

    endpoint: str
    accelerator: str
    shape: str
    url: str | None  # None: Colab has not given it a runtime proxy yet
    token: str = field(repr=False)  # runtime proxy token: used for requests, never shown
    health: Status | Unreachable
    name: str | None = None  # the colab CLI's local name, if it made this one
    running: str | None = None
    assigned_at: datetime | None = None  # from the CLI's history; Colab boots VMs before handing them out

    @property
    def kind(self) -> str:
        return f"{self.hardware}/{self.shape}"

    @property
    def hardware(self) -> str:
        return "CPU" if self.accelerator == "NONE" else self.accelerator

    @property
    def label(self) -> str:
        return self.name or "untracked"

    @property
    def busy(self) -> bool:
        return isinstance(self.health, Status) and self.health.state is KernelState.BUSY

    def idle_for(self, now: datetime) -> timedelta | None:
        """Idle time on the meter: a VM's kernel can sit idle before Colab hands it out."""
        idle = self.health.idle_for(now) if isinstance(self.health, Status) else None
        if idle is None or self.assigned_at is None:
            return idle
        return min(idle, now - self.assigned_at)

    def billed_for(self, now: datetime) -> tuple[timedelta, bool] | None:
        """Time on the meter, and whether it is exact or only an upper bound (VM boot)."""
        if self.assigned_at:
            return now - self.assigned_at, True
        if isinstance(self.health, Status):
            return self.health.uptime(now), False
        return None

    def concerns(self, now: datetime, resources: Resources | None) -> list[str]:
        """What is wrong with this runtime besides being unreachable, worst first, each short enough for a table cell."""
        found = []
        idle = self.idle_for(now)
        util = resources.gpu_utilization if resources else None
        if idle and idle >= IDLE_ALERT and (util is None or util < 0.05):
            found.append(f"idle {fmt_duration(idle)}")
        if resources:
            used, total = resources.ram
            if total and used / total > 0.9:
                found.append(f"RAM {used / total:.0%} full")
            used, total = resources.disk
            if total and used / total > 0.9:
                found.append(f"disk {used / total:.0%} full")
        return found


class Basis(Enum):
    """Where a runtime's hourly rate comes from; Colab reports only the account's total."""

    REPORTED = "reported"  # the account's total, and this runtime is the only one
    SPLIT = "split"  # the total over runtimes all of one kind
    LEARNED = "learned"  # what this kind cost when it last ran alone, scaled to the total when it can be
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Rate:
    hourly: float | None
    basis: Basis

    @property
    def exact(self) -> bool:
        return self.basis is Basis.REPORTED

    def spend(self, billed: tuple[timedelta, bool]) -> tuple[float, str] | None:
        """Compute units at this rate over billed time, and its mark: "≤" only when both are bounds,
        an exact rate over time since VM boot; "≈" otherwise, since billing may start before assignment."""
        if self.hourly is None:
            return None
        time, exact_time = billed
        return self.hourly * (time / timedelta(hours=1)), "≤" if self.exact and not exact_time else "≈"


class RateBook:
    """Hourly compute-unit rates per hardware kind, learned from the account total.

    Colab reports only the account's total rate. When every runtime is the
    same kind, total / count is that kind's rate; the book remembers it so a
    mixed set of runtimes can still be split later.
    """

    def __init__(self, path: Path):
        self.path = path
        self.unsaved: OSError | None = None  # why the last change could not be written
        self.last_read: tuple[set[str], float] | None = None  # the runtimes and total of the last snapshot
        match read_cache(path):
            case dict(rates) if all(is_number(r) for r in rates.values()):
                self.rates: dict[str, float] = rates
            case _:
                self.rates = {}

    def observe(self, s: Snapshot) -> None:
        """Learn a kind's rate when it runs alone and two polls in a row agree on the runtimes and the total.

        One poll is not enough: a runtime of one kind can stop and one of another start
        between its two reads, which the count does not show.
        """
        read = ({r.endpoint for r in s.runtimes}, s.account.rate_hourly)
        steady, self.last_read = read == self.last_read, read
        kinds = {r.kind for r in s.runtimes}
        if not (steady and s.consistent and len(kinds) == 1 and s.account.rate_hourly > 0):
            return
        rate = round(s.account.rate_hourly / len(s.runtimes), 4)
        kind = kinds.pop()
        if self.rates.get(kind) != rate or self.unsaved:  # retried until the disk takes it
            self.rates[kind] = rate
            self.unsaved = write_atomic(self.path, json.dumps(self.rates, indent=2, sort_keys=True))

    def rate(self, runtime: Runtime, s: Snapshot) -> Rate:
        """The account's total, split if it can be; a read that straddled a start or stop never splits.

        When every kind running has been learned, mixed kinds split the total in proportion
        to their learned rates, so the rows add up to what Colab charges now. Otherwise each
        row shows its kind's learned rate, or none.
        """
        total, kinds = s.account.rate_hourly, [r.kind for r in s.runtimes]
        if s.consistent and len(kinds) == 1:
            return Rate(total, Basis.REPORTED)
        if s.consistent and len(set(kinds)) == 1:
            return Rate(total / len(kinds), Basis.SPLIT)
        if s.consistent and set(kinds) <= self.rates.keys() and (known := sum(self.rates[k] for k in kinds)) > 0:
            return Rate(self.rates[runtime.kind] * total / known, Basis.LEARNED)
        return self.learned(runtime.kind)

    def learned(self, kind: str) -> Rate:
        return Rate(self.rates[kind], Basis.LEARNED) if kind in self.rates else Rate(None, Basis.UNKNOWN)


class UsageLog:
    """Balance and hourly rate on disk, for measured burn and the usage graph.

    Colab settles the balance in steps a few minutes apart, so burn is measured
    between the first and last step inside the window, not between polls.
    """

    KEEP = timedelta(days=7)

    def __init__(self, path: Path):
        self.path = path
        self.unsaved: OSError | None = None  # why the last sample could not be written
        self.samples: list[tuple[datetime, float, float]] = []
        if path.exists():
            self.samples = [s for line in path.read_text().splitlines() if (s := _sample(line))]

    def record(self, now: datetime, balance: float, rate: float) -> None:
        """Only numbers go in: one string here would break every burn for the next hour, even after a restart."""
        balance, rate = number(balance), number(rate)
        changed = not self.samples or self.samples[-1][1:] != (balance, rate)
        if changed:
            self.samples.append((now, balance, rate))
            self.samples = [s for s in self.samples if now - s[0] <= self.KEEP]
        if changed or self.unsaved:  # retried until the disk takes it
            self.unsaved = write_atomic(self.path, "".join(json.dumps([t.isoformat(), b, r]) + "\n" for t, b, r in self.samples))

    def burn(self, now: datetime, window: timedelta = timedelta(hours=1)) -> float | None:
        """Compute units per hour charged within the window: the drops between settled steps, over the time they span.

        A top-up's step counts as no charge, so while one is in the window the figure is a floor.
        Once no step has come for longer than the longest gap between steps, nothing is being
        charged, as after a stop, and that time counts too.
        """
        steps = []
        for t, b, _ in self.samples:
            if now - t <= window and (not steps or steps[-1][1] != b):
                steps.append((t, b))
        settled = steps[1:]  # the first is a baseline, not a settled step
        gaps = list(pairwise(settled))
        charged = sum(max(b0 - b1, 0) for (_, b0), (_, b1) in gaps)
        if not charged:
            return None
        quiet_from = now - max(t1 - t0 for (t0, _), (t1, _) in gaps)
        hours = (max(settled[-1][0], quiet_from) - settled[0][0]) / timedelta(hours=1)
        return charged / hours if hours >= 0.1 else None

    def rate_series(self, now: datetime, columns: int, window: timedelta = timedelta(hours=24)) -> list[float | None]:
        """The hourly rate across the window in equal steps, oldest first; None before the first sample."""
        out, i, rate = [], 0, None
        for c in range(columns):
            at = now - window + window * (c + 1) / columns
            while i < len(self.samples) and self.samples[i][0] <= at:
                rate = self.samples[i][2]
                i += 1
            out.append(rate)
        return out


def _sample(line: str) -> tuple[datetime, float, float] | None:
    """One usage line; a damaged one, or one an older cotop wrote with a string in it, is skipped."""
    try:
        match json.loads(line):
            case [ts, balance, rate] if is_number(balance) and is_number(rate):
                return parse_time(ts), balance, rate
    except ValueError:
        pass
    return None


@dataclass(frozen=True)
class PastSession:
    name: str
    endpoint: str
    kind: str
    created: datetime
    ending: Terminated | None  # None: no terminate event; live, or reclaimed without the CLI noticing

    def lasted(self, now: datetime, live: set[str]) -> tuple[timedelta, bool] | None:
        """How long it ran, and whether that is exact or only an upper bound; None if unknown."""
        if self.ending:
            # "pruned" is when the CLI noticed the VM was gone, not when it went
            return self.ending.at - self.created, self.ending.reason != "pruned"
        if self.endpoint in live:
            return now - self.created, True
        return None


@dataclass(frozen=True)
class Event:
    """One line of the CLI's history, as cotop shows it."""

    at: datetime
    summary: str


@dataclass(frozen=True)
class Created:
    at: datetime
    endpoint: str
    kind: str
    summary = "created"

    def session(self, name: str, ending: Terminated | None) -> PastSession:
        return PastSession(name, self.endpoint, self.kind, self.at, ending)


@dataclass(frozen=True)
class Terminated:
    at: datetime
    reason: str | None

    @property
    def summary(self) -> str:
        return f"terminated ({self.reason or '?'})"


@dataclass
class History:
    """The colab CLI's per-session event logs (~/.config/colab-cli/history).

    Each line is cut down to what cotop shows when it is read, so code, outputs
    and stdin replies never reach memory, the screen or a crash report.
    """

    events: dict[str, list[Event | Created | Terminated]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path) -> History:
        """Lines cotop can't read are skipped: the CLI may be halfway through writing the last one."""
        return cls({f.stem: [e for l in f.read_text(errors="replace").splitlines() if (e := _event(l))] for f in directory.glob("*.jsonl")})

    def assigned_at(self) -> dict[str, datetime]:
        """When the CLI was handed each endpoint."""
        return {e.endpoint: e.at for events in self.events.values() for e in events if isinstance(e, Created)}

    def recent(self, name: str, limit: int = 8) -> list[Event]:
        """Newest first, with runs of the same event folded into one line."""
        runs: list[tuple[str, datetime, int]] = []
        for e in reversed(self.events.get(name, [])):
            if runs and runs[-1][0] == e.summary:
                runs[-1] = (e.summary, runs[-1][1], runs[-1][2] + 1)
            elif len(runs) == limit:
                break
            else:
                runs.append((e.summary, e.at, 1))
        return [Event(at, summary if n == 1 else f"{summary} ×{n}") for summary, at, n in runs]

    def past_sessions(self) -> list[PastSession]:
        """One entry per `colab new`; the CLI reuses names, so a file can hold several."""
        sessions = []
        for name, events in self.events.items():
            created = None
            for e in [*events, None]:  # None flushes the last
                if isinstance(e, Terminated) and created:
                    sessions.append(created.session(name, e))
                    created = None
                elif e is None or isinstance(e, Created):
                    if created:
                        sessions.append(created.session(name, None))
                    created = e
        return sorted(sessions, key=lambda s: s.created, reverse=True)


@dataclass(frozen=True)
class Snapshot:
    """The account and its runtimes as one poll read them, a request apart."""

    taken_at: datetime
    account: Account
    runtimes: list[Runtime]
    history: History

    @property
    def consistent(self) -> bool:
        """Whether the account's figures cover the runtimes listed: a start or stop between the reads breaks this."""
        return self.account.assignments_count == len(self.runtimes)


def _event(line: str) -> Event | Created | Terminated | None:
    """One history line, if it is an event cotop can read, keeping only what History shows."""
    try:
        e = json.loads(line)
        at = parse_time(e["timestamp"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    match e:
        case {"event_type": "session_created", "endpoint": str(endpoint), "accelerator": str(accelerator), "machine_shape": str(shape)}:
            return Created(at, endpoint, f"{'CPU' if accelerator == 'NONE' else accelerator}/{shape}")
        case {"event_type": "session_created"}:  # without the fields History reads
            return None
        case {"event_type": "session_terminated"}:
            return Terminated(at, _text(e.get("reason")))
        case {"event_type": "file_operation"}:
            return Event(at, f"{_text(e.get('op')) or 'file'} {Path(_text(e.get('remote')) or '').name}")
        case {"event_type": "execution", "cell_index": int(cell)}:
            return Event(at, f"exec cell {cell}")
        case {"event_type": "execution"}:
            return Event(at, "exec")
        case {"event_type": str(other)}:
            return Event(at, other)
    return None


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


def runtimes_from(
    assignments: list[dict],
    local: dict[str, dict],
    status: dict[str, Status | Exception],
    assigned_at: dict[str, datetime] | None = None,
) -> list[Runtime]:
    """Join the server's assignments with the CLI's names, history and kernel status.

    An assignment with a field of the wrong type fails the whole poll, loudly:
    leaving it out would hide a VM that is billing. So does one without a
    status: Colab.status reads one for every assignment it is given.
    """
    by_endpoint = {s["endpoint"]: s for s in local.values()}
    runtimes = []
    for a in assignments:
        endpoint = string(a["endpoint"])
        mine = by_endpoint.get(endpoint, {})
        t = status[endpoint]
        url, token = runtime_proxy(a) or (None, "")
        shape = whole(a.get("machineShape", 0))  # an int enum in the colab CLI's ListedAssignment
        runtimes.append(
            Runtime(
                endpoint=endpoint,
                accelerator=string(a["accelerator"]),
                shape=SHAPES.get(shape, str(shape)),
                url=url,
                token=token,
                health=t if isinstance(t, Status) else Unreachable(f"{type(t).__name__}: {t}"),
                name=mine.get("name"),
                running=mine.get("running"),
                assigned_at=(assigned_at or {}).get(endpoint),
            )
        )
    return runtimes


def runtime_proxy(assignment: dict) -> tuple[str, str] | None:
    """The URL and token of a runtime's proxy; None while Colab has not given it one.

    colab-vscode's Runtime has connectionInfo as optional, so an assignment can
    come without it; its error never shows the token.
    """
    match assignment.get("runtimeProxyInfo"):
        case None:
            return None
        case {"url": str(url), "token": str(token)}:
            return url, token
    raise ValueError("runtimeProxyInfo without a url and token")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


IDLE_ALERT = timedelta(minutes=10)


def fmt_duration(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    return f"{hours // 24}d{hours % 24}h"
