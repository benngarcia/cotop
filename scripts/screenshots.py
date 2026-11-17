"""Render the README screenshots from made-up data; no Colab account is touched.

    uv run python scripts/screenshots.py OUT_DIR

writes OUT_DIR/dashboard.svg, OUT_DIR/stop.svg and OUT_DIR/small.svg (80x24).
To get the PNGs in docs/images:

    chrome --headless --force-device-scale-factor=2 --window-size=1380,880 \\
        --screenshot=docs/images/dashboard.png OUT_DIR/dashboard.svg
"""

import asyncio
import json
import math
import random
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import cotop.app as app_module
from cotop.app import Dashboard
from cotop.model import Gpu, Kernel, Resources, Status, utc_now

NOW = utc_now()
GB = 1_000_000_000


def ago(**kw):
    return NOW - timedelta(**kw)


def assignment(endpoint, accelerator, shape=0):
    return {
        "endpoint": endpoint,
        "accelerator": accelerator,
        "machineShape": shape,
        "variant": 1,
        "runtimeProxyInfo": {"token": "-", "url": f"https://8080-{endpoint}-a.us-west1.prod.r.colab.dev"},
    }


def status(started, state, last_activity):
    return Status(started=started, kernels=(Kernel(state, last_activity, 1),))


def reading(at, gpu, util, vram, ram, disk):
    return Resources(
        at=at,
        memory_total=ram[1] * GB,
        memory_free=int((ram[1] - ram[0]) * GB),
        disk_total=235 * GB,
        disk_used=int(disk * 235 * GB),
        gpus=(Gpu(gpu, util, int(vram[0] * GB), vram[1] * GB),),
    )


def training(i, rng):
    """A fine-tune: near-full GPU, with a dip for evaluation every three minutes."""
    if i % 90 < 7:
        return 0.18 + 0.15 * rng.random()
    return min(1.0, 0.9 + 0.08 * rng.random())


def inference(i, rng):
    """A notebook serving requests in bursts."""
    return max(0.0, min(1.0, 0.45 + 0.3 * math.sin(i / 11) + 0.2 * rng.random()))


SERIES = 450  # fifteen minutes of readings, one every two seconds


def readings(endpoint):
    rng = random.Random(endpoint)
    out = []
    for i in range(SERIES):
        at = NOW - timedelta(seconds=2 * (SERIES - i))
        if endpoint == A100["endpoint"]:
            out.append(reading(at, "NVIDIA A100-SXM4-40GB", training(i, rng), (37.2, 40), (61 + 2 * rng.random(), 83), 0.41))
        elif endpoint == T4["endpoint"]:
            out.append(reading(at, "Tesla T4", 0.0, (3.1, 15), (6.2, 13), 0.12))
        else:
            util = inference(i, rng)
            out.append(reading(at, "NVIDIA L4", util, (14 + 4 * util, 23), (19, 53), 0.18))
    return out


A100 = assignment("gpu-a100-s-usw1b2-demo0a1", "A100", shape=1)
T4 = assignment("gpu-t4-s-usc1a3-demo0b2", "T4")
L4 = assignment("gpu-l4-s-euw4a0-demo0c3", "L4")
ASSIGNMENTS = [A100, T4, L4]
STATUS = {
    A100["endpoint"]: status(ago(hours=3, minutes=12), "busy", ago(seconds=2)),
    T4["endpoint"]: status(ago(minutes=58), "idle", ago(minutes=34)),
    L4["endpoint"]: status(ago(minutes=21), "busy", ago(seconds=9)),
}
LOCAL = {
    "finetune": {"name": "finetune", "endpoint": A100["endpoint"], "running": "run(train.py)"},
    "eval": {"name": "eval", "endpoint": T4["endpoint"], "running": None},
}
RATES = {"A100/HIGH_RAM": 11.77, "T4/STANDARD": 1.76, "L4/STANDARD": 4.82}


def write_history(directory: Path) -> None:
    def event(at, kind, **fields):
        return json.dumps({"timestamp": at.isoformat(), "event_type": kind, **fields}) + "\n"

    def created(at, endpoint, accelerator, shape="STANDARD"):
        return event(at, "session_created", endpoint=endpoint, variant="GPU", accelerator=accelerator, machine_shape=shape)

    finetune = [created(ago(hours=3, minutes=12), A100["endpoint"], "A100", "HIGH_RAM")]
    finetune.append(event(ago(hours=3, minutes=10), "file_operation", op="upload", remote="/content/data.tar.zst", local="-"))
    finetune.append(event(ago(hours=3, minutes=9), "file_operation", op="upload", remote="/content/train.py", local="-"))
    finetune.append(event(ago(hours=3, minutes=8), "execution", cell_index=0, cell_id="-", code="-", outputs=[]))
    finetune.append(event(ago(hours=3, minutes=7), "execution", cell_index=1, cell_id="-", code="-", outputs=[]))
    finetune += [event(ago(minutes=m), "file_operation", op="download", remote="/content/runs/metrics.json", local="-") for m in range(40, 0, -5)]
    eval_ = [
        created(ago(minutes=58), T4["endpoint"], "T4"),
        event(ago(minutes=50), "file_operation", op="upload", remote="/content/eval.py", local="-"),
        event(ago(minutes=49), "execution", cell_index=0, cell_id="-", code="-", outputs=[]),
        event(ago(minutes=34), "file_operation", op="download", remote="/content/scores.csv", local="-"),
    ]
    past = {
        "sweep": [(ago(days=1, hours=2), ago(days=1), "A100", "HIGH_RAM", "user_requested")],
        "tokenize": [(ago(days=1, hours=5), ago(days=1, hours=4, minutes=25), "NONE", "STANDARD", "user_requested")],
        "baseline": [(ago(days=2, hours=6), ago(days=2, hours=1), "L4", "STANDARD", "pruned")],
        "smoke": [(ago(days=2, hours=7), ago(days=2, hours=6, minutes=52), "T4", "STANDARD", "user_requested")],
    }
    (directory / "finetune.jsonl").write_text("".join(finetune))
    (directory / "eval.jsonl").write_text("".join(eval_))
    for name, runs in past.items():
        lines = []
        for start, end, accelerator, shape, reason in runs:
            lines.append(created(start, f"gpu-{name}-ended", accelerator, shape))
            lines.append(event(end, "session_terminated", reason=reason))
        (directory / f"{name}.jsonl").write_text("".join(lines))


def write_cache(cache: Path) -> None:
    """A day of usage: a T4 in the morning, an A100 from midday, then three runtimes."""
    cache.mkdir(parents=True)
    (cache / "rates.json").write_text(json.dumps(RATES))
    steps = [(24, 0.0), (19, 1.76), (16, 0.0), (11, 11.77), (6, 13.53), (3.2, 11.77), (1, 13.53), (0.35, 18.35)]
    balance, lines = 512.80, []
    for (hours_ago, rate), (next_ago, _) in zip(steps, steps[1:] + [(0, 0)]):
        lines.append(json.dumps([ago(hours=hours_ago).isoformat(), round(balance, 2), rate]))
        balance -= rate * (hours_ago - next_ago)
    (cache / "usage.jsonl").write_text("\n".join(lines) + "\n")


class DemoColab:
    def email(self):
        return "you@example.com"

    def ccu_info(self):
        return {
            "currentBalance": 412.37,
            "consumptionRateHourly": round(sum(RATES.values()), 2),
            "assignmentsCount": len(ASSIGNMENTS),
            "eligibleGpus": ["H100", "G4", "A100", "L4", "T4"],
            "eligibleTpus": ["V6E1", "V5E1"],
        }

    def assignments(self):
        return ASSIGNMENTS

    def status(self, assignments):
        return STATUS

    def resources(self, runtimes):
        return {r.endpoint: readings(r.endpoint)[-1] for r in runtimes}


async def shoot(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "cli" / "history").mkdir(parents=True)
        write_history(root / "cli" / "history")
        write_cache(root / "cache")
        app_module.CACHE = root / "cache"
        app_module.CLI_HOME = root / "cli"
        app_module.local_sessions = lambda: LOCAL

        for name, size in (("dashboard", (138, 40)), ("small", (80, 24))):
            app = Dashboard(DemoColab())
            for a in ASSIGNMENTS:
                for r in readings(a["endpoint"]):
                    app.samples.add(a["endpoint"], r.at, r)
            async with app.run_test(size=size) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                app.save_screenshot(f"{name}.svg", path=str(out))
                if name == "dashboard":
                    await pilot.press("down")
                    await pilot.pause()
                    app.save_screenshot("eval.svg", path=str(out))
                    await pilot.press("s")
                    await pilot.pause()
                    app.save_screenshot("stop.svg", path=str(out))


if __name__ == "__main__":
    asyncio.run(shoot(Path(sys.argv[1])))
