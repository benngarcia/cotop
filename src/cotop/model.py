"""What a Colab account looks like at one moment: compute units and runtimes.

Every value here is parsed from a raw Colab response, so the parsing is the
single place that knows the wire format (XSSI prefix, camelCase, fractions).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

XSSI_PREFIX = ")]}'\n"
SHAPES = {0: "STANDARD", 1: "HIGH_RAM"}
VARIANTS = {0: "DEFAULT", 1: "GPU", 2: "TPU"}


def parse_json(body: str):
    return json.loads(body.removeprefix(XSSI_PREFIX))


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
        return cls(
            email=email,
            balance=ccu["currentBalance"],
            rate_hourly=ccu["consumptionRateHourly"],
            assignments_count=ccu["assignmentsCount"],
            eligible_gpus=tuple(ccu.get("eligibleGpus", ())),
            eligible_tpus=tuple(ccu.get("eligibleTpus", ())),
        )

    @property
    def runway(self) -> timedelta | None:
        if self.rate_hourly <= 0:
            return None
        return timedelta(hours=self.balance / self.rate_hourly)


@dataclass(frozen=True)
class Kernel:
    state: str
    last_activity: datetime
    connections: int


@dataclass(frozen=True)
class Gpu:
    name: str
    utilization: float | None  # None when Colab left it out: a gap, not an idle GPU
    memory_used: int
    memory_total: int


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
        a missing utilization stays None, so it draws as a gap rather than an idle GPU."""
        memory = resources.get("memory", {})
        disks = [d.get("filesystem", {}) for d in resources.get("disks", [])]
        return cls(
            at=at,
            memory_total=memory.get("totalBytes", 0),
            memory_free=memory.get("freeBytes", 0),
            disk_total=sum(d.get("totalBytes", 0) for d in disks),
            disk_used=sum(d.get("usedBytes", 0) for d in disks),
            gpus=tuple(
                Gpu(g.get("name", "GPU"), g.get("gpuUtilization"), g.get("memoryUsedBytes", 0), g.get("memoryTotalBytes", 0))
                for g in resources.get("gpus", [])
            ),
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
    """What the runtime's Jupyter server says about its kernels."""

    started: datetime
    kernels: tuple[Kernel, ...]

    @classmethod
    def parse(cls, status: dict, kernels: list) -> Status:
        return cls(
            started=parse_time(status["started"]),
            kernels=tuple(
                Kernel(k["execution_state"], parse_time(k["last_activity"]), k["connections"])
                for k in kernels
            ),
        )

    @property
    def busy(self) -> bool:
        return any(k.state == "busy" for k in self.kernels)

    @property
    def clients(self) -> int:
        return sum(k.connections for k in self.kernels)

    def idle_for(self, now: datetime) -> timedelta | None:
        """How long every kernel has been idle; None while one is busy."""
        if self.busy:
            return None
        if not self.kernels:
            return now - self.started
        return now - max(k.last_activity for k in self.kernels)

    def uptime(self, now: datetime) -> timedelta:
        return now - self.started


@dataclass(frozen=True)
class Runtime:
    """One Colab assignment: a VM the account is paying for."""

    endpoint: str
    accelerator: str
    shape: str
    variant: str
    url: str
    token: str = field(default="", repr=False)  # runtime proxy token: used for requests, never shown
    name: str | None = None  # the colab CLI's local name, if it made this one
    running: str | None = None
    assigned_at: datetime | None = None  # from the CLI's history; Colab boots VMs before handing them out
    status: Status | None = None
    error: str | None = None

    @property
    def kind(self) -> str:
        return f"{self.hardware}/{self.shape}"

    @property
    def hardware(self) -> str:
        return "CPU" if self.accelerator == "NONE" else self.accelerator

    @property
    def label(self) -> str:
        return self.name or "untracked"

    def idle_for(self, now: datetime) -> timedelta | None:
        """Idle time on the meter: a VM's kernel can sit idle before Colab hands it out."""
        idle = self.status.idle_for(now) if self.status else None
        if idle is None or self.assigned_at is None:
            return idle
        return min(idle, now - self.assigned_at)

    def billed_for(self, now: datetime) -> tuple[timedelta, bool] | None:
        """Time on the meter, and whether it is exact or only an upper bound (VM boot)."""
        if self.assigned_at:
            return now - self.assigned_at, True
        if self.status:
            return self.status.uptime(now), False
        return None

    def concerns(self, now: datetime, resources: Resources | None) -> list[str]:
        """What is wrong with this runtime, worst first, each short enough for a table cell."""
        if self.error:
            return [f"unreachable: {self.error}"]
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


@dataclass(frozen=True)
class Rate:
    hourly: float | None
    basis: str  # reported | split | learned | unknown

    @property
    def exact(self) -> bool:
        return self.basis == "reported"


class RateBook:
    """Hourly compute-unit rates per hardware kind, learned from the account total.

    Colab reports only the account's total rate. When every runtime is the
    same kind, total / count is that kind's rate; the book remembers it so a
    mixed set of runtimes can still be split later.
    """

    def __init__(self, path: Path):
        self.path = path
        self.rates: dict[str, float] = json.loads(path.read_text()) if path.exists() else {}

    def observe(self, account: Account, runtimes: list[Runtime]) -> None:
        kinds = {r.kind for r in runtimes}
        if len(kinds) != 1 or account.rate_hourly <= 0:
            return
        if account.assignments_count != len(runtimes):  # the two reads straddled a start or stop
            return
        rate = round(account.rate_hourly / len(runtimes), 4)
        kind = kinds.pop()
        if self.rates.get(kind) != rate:
            self.rates[kind] = rate
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.rates, indent=2, sort_keys=True))

    def rate(self, runtime: Runtime, runtimes: list[Runtime], account: Account) -> Rate:
        if len(runtimes) == 1:
            return Rate(account.rate_hourly, "reported")
        if len({r.kind for r in runtimes}) == 1:
            return Rate(account.rate_hourly / len(runtimes), "split")
        if runtime.kind in self.rates:
            return Rate(self.rates[runtime.kind], "learned")
        return Rate(None, "unknown")


class UsageLog:
    """Balance and hourly rate on disk, for measured burn and the usage graph.

    Colab settles the balance in steps a few minutes apart, so burn is measured
    between the first and last step inside the window, not between polls.
    """

    KEEP = timedelta(days=7)

    def __init__(self, path: Path):
        self.path = path
        self.samples: list[tuple[datetime, float, float]] = []
        if path.exists():
            for line in path.read_text().splitlines():
                ts, balance, rate = json.loads(line)
                self.samples.append((parse_time(ts), balance, rate))

    def record(self, now: datetime, balance: float, rate: float) -> None:
        if self.samples and self.samples[-1][1:] == (balance, rate):
            return
        self.samples.append((now, balance, rate))
        self.samples = [s for s in self.samples if now - s[0] <= self.KEEP]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps([t.isoformat(), b, r]) + "\n" for t, b, r in self.samples))

    def burn(self, now: datetime, window: timedelta = timedelta(hours=1)) -> float | None:
        """Compute units per hour actually charged within the window."""
        steps = []
        for t, b, _ in self.samples:
            if now - t <= window and (not steps or steps[-1][1] != b):
                steps.append((t, b))
        if len(steps) < 3:  # the first is a baseline, not a settled step
            return None
        (t0, b0), (t1, b1) = steps[1], steps[-1]
        hours = (t1 - t0).total_seconds() / 3600
        if hours < 0.1 or b1 > b0:  # too short, or a top-up landed
            return None
        return (b0 - b1) / hours

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


@dataclass(frozen=True)
class PastSession:
    name: str
    endpoint: str
    kind: str
    created: datetime
    ended: datetime | None  # None: no terminate event; live, or reclaimed without the CLI noticing
    reason: str | None

    def lasted(self, now: datetime, live: set[str]) -> tuple[timedelta, str] | None:
        """How long it ran, and "" if exact, "≤" if an upper bound; None if unknown."""
        if self.ended:
            # "pruned" is when the CLI noticed the VM was gone, not when it went
            return self.ended - self.created, "≤" if self.reason == "pruned" else ""
        if self.endpoint in live:
            return now - self.created, ""
        return None


@dataclass(frozen=True)
class Event:
    at: datetime
    summary: str


def summarize_event(e: dict) -> str:
    """One line per CLI history event; never code, outputs or stdin replies."""
    match e["event_type"]:
        case "file_operation":
            return f"{e.get('op', 'file')} {Path(e.get('remote') or '').name}"
        case "execution":
            return f"exec cell {e['cell_index']}" if e.get("cell_index") is not None else "exec"
        case "session_created":
            return "created"
        case "session_terminated":
            return f"terminated ({e.get('reason', '?')})"
        case other:
            return other


@dataclass
class History:
    """The colab CLI's per-session event logs (~/.config/colab-cli/history)."""

    events: dict[str, list[dict]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path) -> History:
        events = {}
        for f in directory.glob("*.jsonl"):
            events[f.stem] = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return cls(events)

    def assigned_at(self) -> dict[str, datetime]:
        """When the CLI was handed each endpoint."""
        return {
            e["endpoint"]: parse_time(e["timestamp"])
            for events in self.events.values()
            for e in events
            if e["event_type"] == "session_created"
        }

    def recent(self, name: str, limit: int = 8) -> list[Event]:
        """Newest first, with runs of the same event folded into one line."""
        runs: list[tuple[str, datetime, int]] = []
        for e in reversed(self.events.get(name, [])):
            summary, at = summarize_event(e), parse_time(e["timestamp"])
            if runs and runs[-1][0] == summary:
                runs[-1] = (summary, runs[-1][1], runs[-1][2] + 1)
            elif len(runs) == limit:
                break
            else:
                runs.append((summary, at, 1))
        return [Event(at, summary if n == 1 else f"{summary} ×{n}") for summary, at, n in runs]

    def past_sessions(self) -> list[PastSession]:
        """One entry per `colab new`; the CLI reuses names, so a file can hold several."""
        sessions = []
        for name, events in self.events.items():
            created = None
            for e in events + [{"event_type": "session_created"}]:  # sentinel flushes the last
                if e["event_type"] == "session_created":
                    if created:
                        sessions.append(_past(name, created, None))
                    created = e if "timestamp" in e else None
                elif e["event_type"] == "session_terminated" and created:
                    sessions.append(_past(name, created, e))
                    created = None
        return sorted(sessions, key=lambda s: s.created, reverse=True)


def _past(name: str, created: dict, ended: dict | None) -> PastSession:
    hardware = "CPU" if created["accelerator"] == "NONE" else created["accelerator"]
    return PastSession(
        name=name,
        endpoint=created["endpoint"],
        kind=f"{hardware}/{created['machine_shape']}",
        created=parse_time(created["timestamp"]),
        ended=parse_time(ended["timestamp"]) if ended else None,
        reason=ended.get("reason") if ended else None,
    )


def runtimes_from(
    assignments: list[dict],
    local: dict[str, dict],
    status: dict[str, Status | Exception],
    assigned_at: dict[str, datetime] | None = None,
) -> list[Runtime]:
    """Join the server's assignments with the CLI's names, history and kernel status."""
    by_endpoint = {s["endpoint"]: s for s in local.values()}
    runtimes = []
    for a in assignments:
        mine = by_endpoint.get(a["endpoint"], {})
        t = status.get(a["endpoint"])
        runtimes.append(
            Runtime(
                endpoint=a["endpoint"],
                accelerator=a["accelerator"],
                shape=SHAPES.get(a.get("machineShape", 0), str(a.get("machineShape"))),
                variant=VARIANTS.get(a.get("variant", 0), str(a.get("variant"))),
                url=a["runtimeProxyInfo"]["url"],
                token=a["runtimeProxyInfo"]["token"],
                name=mine.get("name"),
                running=mine.get("running"),
                assigned_at=(assigned_at or {}).get(a["endpoint"]),
                status=t if isinstance(t, Status) else None,
                error=f"{type(t).__name__}: {t}" if isinstance(t, Exception) else None,
            )
        )
    return runtimes


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
