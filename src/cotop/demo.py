"""A made-up heavy Colab account, for `cotop --demo` and the README screenshots.

It answers the same calls as `cotop.colab.Colab`, so the dashboard runs on it
unchanged. Nothing here touches Google, the colab CLI's files or ~/.cache/cotop.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cotop.model import Gpu, History, Kernel, Resources, Runtime, Status, utc_now

GB = 1_000_000_000
STEP = timedelta(seconds=2)  # one reading every two seconds, like the real sampler


@dataclass(frozen=True)
class Machine:
    """One demo runtime: what it is, and what its readings do over time."""

    name: str | None  # None: opened in a browser, so the colab CLI has no name for it
    endpoint: str
    accelerator: str
    shape: int  # 0 standard, 1 high-RAM
    gpu: str | None
    vram_total: float
    ram_total: float
    disk: float
    rate: float  # CU/hr, illustrative
    started_ago: timedelta
    idle_for: timedelta | None  # None: the kernel is busy
    load: str  # train | sweep | infer | idle | data | tpu
    running: str | None = None
    unreachable: bool = False


MACHINES = [
    Machine("pretrain-7b", "gpu-h100-s-usc1a1-demo01", "H100", 0, "NVIDIA H100 80GB HBM3", 80, 234, 0.58, 15.04, timedelta(hours=9, minutes=40), None, "train", "run(pretrain.py)"),
    Machine("finetune", "gpu-a100-s-usw1b2-demo02", "A100", 1, "NVIDIA A100-SXM4-40GB", 40, 83, 0.41, 11.77, timedelta(hours=3, minutes=12), None, "train", "run(train.py)"),
    Machine("sweep-lr1", "gpu-l4-s-use4a1-demo03", "L4", 0, "NVIDIA L4", 23, 53, 0.22, 4.82, timedelta(hours=1, minutes=5), None, "sweep", "run(sweep.py --lr 1e-4)"),
    Machine("sweep-lr2", "gpu-l4-s-use4a1-demo04", "L4", 0, "NVIDIA L4", 23, 53, 0.22, 4.82, timedelta(hours=1, minutes=5), None, "sweep", "run(sweep.py --lr 3e-4)"),
    Machine("sweep-lr3", "gpu-l4-s-use4a2-demo05", "L4", 0, "NVIDIA L4", 23, 53, 0.97, 4.82, timedelta(hours=1, minutes=4), None, "sweep", "run(sweep.py --lr 1e-3)"),
    Machine("eval", "gpu-t4-s-usc1a3-demo06", "T4", 0, "Tesla T4", 15, 13, 0.12, 1.76, timedelta(minutes=58), timedelta(minutes=34), "idle"),
    Machine("data-prep", "m-s-usc1b1-demo07", "NONE", 1, None, 0, 52, 0.66, 0.29, timedelta(hours=2), None, "data", "run(tokenize.py)"),
    Machine("jax-tpu", "tpu-v6e1-s-usc1a1-demo08", "V6E1", 0, None, 0, 176, 0.31, 3.52, timedelta(minutes=47), None, "tpu", "run(train_jax.py)"),
    Machine(None, "gpu-g4-s-euw4a0-demo09", "G4", 0, "NVIDIA RTX PRO 6000 Blackwell", 96, 190, 0.18, 10.05, timedelta(minutes=21), None, "infer"),
    Machine(None, "gpu-t4-s-euw4b1-demo10", "T4", 0, "Tesla T4", 15, 13, 0.09, 1.76, timedelta(hours=3, minutes=10), timedelta(hours=2, minutes=48), "idle"),
    Machine("render", "gpu-l4-s-use1c2-demo11", "L4", 0, "NVIDIA L4", 23, 53, 0.2, 4.82, timedelta(hours=5), None, "infer", "run(render.py)", unreachable=True),
    Machine("notebook", "gpu-a100-s-usw1b4-demo12", "A100", 0, "NVIDIA A100-SXM4-40GB", 40, 83, 0.11, 11.77, timedelta(minutes=12), timedelta(minutes=3), "idle"),
]
BALANCE = 612.40  # with the rates above this lasts under a day, so it shows in red


def utilization(m: Machine, i: int, rng: random.Random) -> float:
    """GPU (or, without one, RAM) use for reading i, shaped like the job."""
    match m.load:
        case "train":  # near full, with an evaluation dip every three minutes
            return 0.18 + 0.15 * rng.random() if i % 90 < 7 else min(1.0, 0.9 + 0.08 * rng.random())
        case "sweep":
            return min(1.0, 0.72 + 0.2 * math.sin(i / 23 + len(m.endpoint)) + 0.06 * rng.random())
        case "infer":  # bursts of requests
            return max(0.0, min(1.0, 0.45 + 0.3 * math.sin(i / 11) + 0.2 * rng.random()))
        case "data":
            return min(1.0, 0.35 + 0.004 * (i % 200) + 0.05 * rng.random())
        case "tpu":
            return 0.55 + 0.1 * math.sin(i / 40) + 0.05 * rng.random()
        case _:
            return 0.0


def reading(m: Machine, i: int, at: datetime) -> Resources:
    rng = random.Random(f"{m.endpoint}:{i}")
    u = utilization(m, i, rng)
    gpus = ()
    ram_used = m.ram_total * (0.12 + (0.6 * u if m.gpu is None else 0.25))
    if m.gpu:
        gpus = (Gpu(m.gpu, u, int(m.vram_total * (0.15 + 0.78 * (u > 0.05)) * GB), int(m.vram_total * GB)),)
    return Resources(
        at=at,
        memory_total=int(m.ram_total * GB),
        memory_free=int((m.ram_total - ram_used) * GB),
        disk_total=235 * GB,
        disk_used=int(m.disk * 235 * GB),
        gpus=gpus,
    )


class DemoColab:
    """The account behind `cotop --demo`. Stopping a runtime removes it, as on Colab."""

    def __init__(self, now: datetime | None = None):
        self.start = now or utc_now()
        self.machines = {m.endpoint: m for m in MACHINES}

    # What the dashboard reads.

    def email(self) -> str:
        return "you@example.com"

    def ccu_info(self) -> dict:
        return {
            "currentBalance": BALANCE,
            "consumptionRateHourly": round(sum(m.rate for m in self.machines.values()), 2),
            "assignmentsCount": len(self.machines),
            "eligibleGpus": ["H100", "G4", "A100", "L4", "T4"],
            "eligibleTpus": ["V6E1", "V5E1"],
        }

    def assignments(self) -> list[dict]:
        return [
            {
                "endpoint": m.endpoint,
                "accelerator": m.accelerator,
                "machineShape": m.shape,
                "variant": 2 if m.load == "tpu" else (0 if m.accelerator == "NONE" else 1),
                "runtimeProxyInfo": {"token": "demo", "url": f"https://8080-{m.endpoint}-a.us-central1.prod.r.colab.dev"},
            }
            for m in self.machines.values()
        ]

    def status(self, assignments: list[dict]) -> dict[str, Status | Exception]:
        now = utc_now()
        out = {}
        for a in assignments:
            m = self.machines[a["endpoint"]]
            if m.unreachable:
                out[m.endpoint] = ConnectionError("runtime proxy returned 502")
                continue
            state = "busy" if m.idle_for is None else "idle"
            last = now - (m.idle_for or timedelta(seconds=1))
            out[m.endpoint] = Status(started=now - m.started_ago, kernels=(Kernel(state, last, 1),))
        return out

    def resources(self, runtimes: list[Runtime]) -> dict[str, Resources | None]:
        now = utc_now()
        i = HISTORY + int((now - self.start) / STEP)
        return {
            r.endpoint: None if self.machines[r.endpoint].unreachable else reading(self.machines[r.endpoint], i, now)
            for r in runtimes
            if r.endpoint in self.machines
        }

    def backfill(self) -> dict[str, list[Resources | None]]:
        """Readings for the last HISTORY steps, so graphs open full instead of empty."""
        return {
            m.endpoint: [None if m.unreachable else reading(m, i, self.start - STEP * (HISTORY - i)) for i in range(HISTORY)]
            for m in self.machines.values()
        }

    def sessions(self) -> dict[str, dict]:
        return {m.name: {"name": m.name, "endpoint": m.endpoint, "running": m.running} for m in self.machines.values() if m.name}

    def history(self) -> History:
        return History(demo_history(self.start))

    # What the dashboard does.

    def stop(self, runtime: Runtime) -> str:
        self.machines.pop(runtime.endpoint, None)
        return "stopped (demo)"

    def restart_kernel(self, runtime: Runtime) -> str:
        return "kernel restarted (demo)"

    def ssh_command(self, runtime: Runtime) -> list[str]:
        raise RuntimeError("the demo has no machines to open a shell on")


HISTORY = 450  # fifteen minutes of readings


def demo_history(now: datetime) -> dict[str, list[dict]]:
    """The colab CLI's event log for the demo: live sessions plus a week of finished ones."""

    def event(at, kind, **fields):
        return {"timestamp": at.isoformat(), "event_type": kind, **fields}

    def created(at, m_endpoint, accelerator, shape="STANDARD"):
        return event(at, "session_created", endpoint=m_endpoint, variant="GPU", accelerator=accelerator, machine_shape=shape)

    events: dict[str, list[dict]] = {}
    for m in MACHINES:
        if not m.name:
            continue
        start = now - m.started_ago
        log = [created(start, m.endpoint, m.accelerator, "HIGH_RAM" if m.shape else "STANDARD")]
        log.append(event(start + timedelta(minutes=2), "file_operation", op="upload", remote="/content/data.tar.zst", local="-"))
        log.append(event(start + timedelta(minutes=3), "execution", cell_index=0, cell_id="-", code="-", outputs=[]))
        if m.idle_for is None:
            log += [event(now - timedelta(minutes=k), "file_operation", op="download", remote="/content/runs/metrics.json", local="-") for k in range(40, 0, -5)]
        events[m.name] = log
    rng = random.Random(7)
    kinds = [("A100", "HIGH_RAM", 11.77), ("H100", "STANDARD", 15.04), ("L4", "STANDARD", 4.82), ("T4", "STANDARD", 1.76), ("NONE", "STANDARD", 0.08)]
    for k in range(18):
        accelerator, shape, _ = kinds[k % len(kinds)]
        start = now - timedelta(hours=10 + 9 * k, minutes=rng.randrange(60))
        end = start + timedelta(minutes=rng.randrange(8, 400))
        name = f"run-{k:02d}"
        events[name] = [
            created(start, f"demo-ended-{k:02d}", accelerator, shape),
            event(end, "session_terminated", reason="pruned" if k % 7 == 3 else "user_requested"),
        ]
    return events


def seed_cache(cache: Path, now: datetime) -> None:
    """A day of balance and rate history, and the per-hardware rates cotop learns over time."""
    cache.mkdir(parents=True, exist_ok=True)
    rates = {f"{'CPU' if m.accelerator == 'NONE' else m.accelerator}/{'HIGH_RAM' if m.shape else 'STANDARD'}": m.rate for m in MACHINES}
    (cache / "rates.json").write_text(json.dumps(rates))
    steps = [(24, 0.0), (21, 4.82), (17, 16.59), (12, 31.0), (8, 45.6), (4, 62.3), (1.5, 74.1), (0.3, round(sum(m.rate for m in MACHINES), 2))]
    balance, lines = BALANCE + 520.0, []
    for (hours_ago, rate), (next_ago, _) in zip(steps, steps[1:] + [(0, 0)]):
        lines.append(json.dumps([(now - timedelta(hours=hours_ago)).isoformat(), round(balance, 2), rate]))
        balance -= rate * (hours_ago - next_ago)
    (cache / "usage.jsonl").write_text("\n".join(lines) + "\n")
