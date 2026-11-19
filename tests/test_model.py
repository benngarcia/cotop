"""Fixtures follow the shapes of real Colab responses; every value is made up."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cotop.model import (
    Account,
    History,
    RateBook,
    Resources,
    Samples,
    Status,
    UsageLog,
    parse_json,
    parse_time,
    runtimes_from,
)

NOW = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def ccu(rate, count):
    return {
        "currentBalance": 5227.43,
        "consumptionRateHourly": rate,
        "assignmentsCount": count,
        "eligibleGpus": ["H100", "G4", "A100", "L4", "T4"],
        "eligibleTpus": ["V6E1", "V5E1"],
    }


def assignment(endpoint, accelerator, shape=0, variant=1):
    return {
        "endpoint": endpoint,
        "variant": variant,
        "machineShape": shape,
        "accelerator": accelerator,
        "runtimeProxyInfo": {"token": "t", "tokenExpiresInSeconds": 3600, "url": f"https://8080-{endpoint}.colab.dev"},
    }


G4 = assignment("gpu-g4-s-usw1a1-example01", "G4")
T4 = assignment("gpu-t4-s-abc", "T4")
CPU = assignment("m-s-xyz", "NONE", variant=0)
LOCAL = {"train": {"name": "train", "endpoint": G4["endpoint"], "running": "run(train.py)"}}


def status(state="busy", last_activity="2026-11-17T21:59:00Z"):
    return Status.parse(
        {"started": "2026-11-17T20:59:46.480319Z", "last_activity": last_activity, "kernels": 1, "connections": 1},
        [{"id": "k", "name": "python3", "last_activity": last_activity, "execution_state": state, "connections": 1}],
    )


def resources(util=1.0, at=NOW, ram_free=None, disk_used=None):
    body = parse_json((FIXTURES / "resources_g4.txt").read_text())
    body["gpus"][0]["gpuUtilization"] = util
    if ram_free is not None:
        body["memory"]["freeBytes"] = ram_free
    if disk_used is not None:
        body["disks"][0]["filesystem"]["usedBytes"] = disk_used
    return Resources.parse(at, body)


def test_resources_payload_parses_through_xssi_prefix():
    r = resources()
    assert r.gpus[0].name == "NVIDIA RTX PRO 6000 Blackwell Server Edition"
    assert r.gpu_utilization == 1.0
    assert r.vram == (90_000_000_000, 100_000_000_000)
    assert r.disk == (65_000_000_000, 250_000_000_000)
    assert status().uptime(NOW) == timedelta(hours=1, seconds=13.519681)


def test_a_cpu_runtime_has_no_gpu_series():
    body = parse_json((FIXTURES / "resources_g4.txt").read_text())
    body["gpus"] = []
    r = Resources.parse(NOW, body)
    assert r.gpu_utilization is None and r.vram is None


def test_samples_keep_gaps_and_forget_ended_runtimes():
    samples = Samples()
    samples.add("a", NOW - timedelta(seconds=4), resources(0.5))
    samples.add("a", NOW - timedelta(seconds=2), None)  # a reading that failed
    samples.add("b", NOW, resources(1.0))
    assert samples.series("a", lambda r: r.gpu_utilization) == [0.5, None]
    assert samples.latest("a").gpu_utilization == 0.5
    assert samples.time_of("a", 1) == NOW - timedelta(seconds=2)  # a gap keeps its time
    assert samples.time_of("a", 99) == NOW - timedelta(seconds=4)
    samples.keep_only({"b"})
    assert samples.series("a", lambda r: r.gpu_utilization) == [] and samples.latest("b") is not None


def test_account_reads_eligible_hardware_and_runway():
    a = Account.parse("me@example.com", ccu(10.05, 1))
    assert a.eligible_gpus == ("H100", "G4", "A100", "L4", "T4")
    assert a.runway == timedelta(hours=5227.43 / 10.05)
    assert Account.parse("me@example.com", ccu(0, 0)).runway is None


def test_single_runtime_gets_the_reported_account_rate(tmp_path):
    a = Account.parse("me@example.com", ccu(10.05, 1))
    [r] = runtimes = runtimes_from([G4], LOCAL, {G4["endpoint"]: status()})
    assert (r.name, r.hardware, r.shape, r.running, r.token) == ("train", "G4", "STANDARD", "run(train.py)", "t")
    rate = RateBook(tmp_path / "r.json").rate(r, runtimes, a)
    assert (rate.hourly, rate.basis) == (10.05, "reported")
    assert "token=" not in repr(r)  # the proxy token never reaches a log line or the screen


def test_mixed_hardware_does_not_give_every_row_the_account_total(tmp_path):
    a = Account.parse("me@example.com", ccu(11.89, 2))
    runtimes = runtimes_from([G4, T4], LOCAL, {})
    book = RateBook(tmp_path / "r.json")
    assert [book.rate(r, runtimes, a).basis for r in runtimes] == ["unknown", "unknown"]

    book.observe(Account.parse("me@example.com", ccu(10.05, 1)), runtimes_from([G4], LOCAL, {}))
    book.observe(Account.parse("me@example.com", ccu(3.68, 2)), runtimes_from([T4, T4], {}, {}))
    assert [book.rate(r, runtimes, a).hourly for r in runtimes] == [10.05, 1.84]
    assert RateBook(tmp_path / "r.json").rates == {"G4/STANDARD": 10.05, "T4/STANDARD": 1.84}


def test_rate_book_learns_nothing_from_a_mixed_set(tmp_path):
    book = RateBook(tmp_path / "r.json")
    book.observe(Account.parse("me@example.com", ccu(11.89, 2)), runtimes_from([G4, T4], LOCAL, {}))
    assert book.rates == {}


def test_same_kind_runtimes_split_the_total(tmp_path):
    a = Account.parse("me@example.com", ccu(3.68, 2))
    runtimes = runtimes_from([T4, assignment("gpu-t4-s-def", "T4")], {}, {})
    rate = RateBook(tmp_path / "r.json").rate(runtimes[0], runtimes, a)
    assert (rate.hourly, rate.basis) == (1.84, "split")


def test_untracked_and_unreachable_runtimes():
    runtimes = runtimes_from([G4, CPU], LOCAL, {G4["endpoint"]: ConnectionError("proxy said 502")})
    assert runtimes[0].status is None and runtimes[0].error == "ConnectionError: proxy said 502"
    assert runtimes[0].concerns(NOW, None) == ["unreachable: ConnectionError: proxy said 502"]
    assert (runtimes[1].label, runtimes[1].hardware) == ("untracked", "CPU")


@pytest.mark.parametrize(
    "state,last_activity,util,expected",
    [
        ("idle", "2026-11-17T21:35:00Z", 0.0, ["idle 25m"]),
        ("idle", "2026-11-17T21:55:00Z", 0.0, []),  # idle, but not for long
        ("idle", "2026-11-17T21:35:00Z", 0.9, []),  # kernel idle, GPU still working
        ("busy", "2026-11-17T21:35:00Z", 0.0, []),
    ],
)
def test_idle_while_billing(state, last_activity, util, expected):
    [r] = runtimes_from([G4], LOCAL, {G4["endpoint"]: status(state, last_activity)})
    assert r.concerns(NOW, resources(util)) == expected


def test_full_ram_and_disk_are_concerns():
    [r] = runtimes_from([G4], LOCAL, {G4["endpoint"]: status()})
    full = resources(ram_free=5_000_000_000, disk_used=240_000_000_000)
    assert r.concerns(NOW, full) == ["RAM 97% full", "disk 96% full"]


def test_burn_is_measured_between_settled_steps(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    start = NOW - timedelta(minutes=50)
    log.record(start, 100.0, 10.05)  # baseline: first sight, mid-step
    for i in range(1, 6):  # a 0.84 step every 5 minutes is 10.08/hr
        log.record(start + timedelta(minutes=5 * i + 2), 100.0 - 0.84 * i, 10.05)
        log.record(start + timedelta(minutes=5 * i + 3), 100.0 - 0.84 * i, 10.05)  # unchanged, not stored
    assert len(log.samples) == 6
    assert log.burn(NOW) == pytest.approx(10.08)
    assert UsageLog(tmp_path / "u.jsonl").samples == log.samples


def test_burn_ignores_top_ups_and_short_windows(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    log.record(NOW - timedelta(minutes=30), 100.0, 10.0)
    log.record(NOW - timedelta(minutes=25), 99.0, 10.0)
    assert log.burn(NOW) is None  # one settled step is not a rate
    log.record(NOW - timedelta(minutes=10), 600.0, 10.0)
    assert log.burn(NOW) is None


def test_rate_series_steps_through_the_day(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    log.record(NOW - timedelta(hours=15), 100.0, 1.76)
    log.record(NOW - timedelta(hours=5), 90.0, 11.77)
    # four 6-hour columns, each the rate at its end: -18h (nothing logged yet), -12h, -6h, now
    assert log.rate_series(NOW, 4) == [None, 1.76, 1.76, 11.77]


SECRET_HISTORY = (  # a session's CLI history whose code, outputs and stdin reply are canaries
    '{"timestamp": "2026-11-17T20:59:00+00:00", "event_type": "session_created", "endpoint": "%s", "variant": "GPU", "accelerator": "G4", "machine_shape": "STANDARD"}\n'
    % G4["endpoint"]
    + '{"timestamp": "2026-11-17T21:00:00+00:00", "event_type": "execution", "cell_index": 3, "cell_id": "c", "code": "HF_TOKEN = \'SECRET_CODE\'", "outputs": ["SECRET_OUT"]}\n'
    '{"timestamp": "2026-11-17T21:01:00+00:00", "event_type": "input_reply", "value": "SECRET_PASSWORD"}\n'
    '{"timestamp": "2026-11-17T21:02:00+00:00", "event_type": "file_operation", "op": "download", "remote": "/content/out/model.pt", "local": "/Users/x/model.pt"}\n'
    '{"timestamp": "2026-11-17T21:30:00+00:00", "event_type": "session_terminated", "reason": "user_requested"}\n'
)


def test_history_never_surfaces_code_outputs_or_stdin(tmp_path):
    (tmp_path / "train.jsonl").write_text(SECRET_HISTORY)
    h = History.load(tmp_path)
    assert "SECRET" not in repr(h)  # not held at all, so no crash report can print it
    text = " ".join(e.summary for e in h.recent("train"))
    assert text == "terminated (user_requested) download model.pt input_reply exec cell 3 created"
    [p] = h.past_sessions()
    assert (p.kind, p.ended - p.created, p.reason) == ("G4/STANDARD", timedelta(minutes=31), "user_requested")


def test_reused_names_are_separate_sessions_and_repeats_fold(tmp_path):
    created = '{"timestamp": "%s", "event_type": "session_created", "endpoint": "e", "variant": "GPU", "accelerator": "G4", "machine_shape": "STANDARD"}\n'
    poll = '{"timestamp": "%s", "event_type": "file_operation", "op": "download", "remote": "/content/live.json", "local": "x"}\n'
    (tmp_path / "train.jsonl").write_text(
        created % "2026-11-17T20:11:00+00:00"
        + '{"timestamp": "2026-11-17T21:20:00+00:00", "event_type": "session_terminated", "reason": "user_requested"}\n'
        + created % "2026-11-17T20:59:00+00:00"
        + "".join(poll % f"2026-11-17T21:4{i}:00+00:00" for i in range(5))
    )
    h = History.load(tmp_path)
    assert [(p.created.hour, p.ended is None) for p in h.past_sessions()] == [(20, True), (20, False)]
    assert [p.created.minute for p in h.past_sessions()] == [59, 11]
    assert [e.summary for e in h.recent("train")] == ["download live.json ×5", "created", "terminated (user_requested)", "created"]


def test_billing_starts_at_assignment_not_vm_boot(tmp_path):
    (tmp_path / "train.jsonl").write_text(
        '{"timestamp": "2026-11-17T21:21:17+00:00", "event_type": "session_created", "endpoint": "%s", "variant": "GPU", "accelerator": "G4", "machine_shape": "STANDARD"}\n'
        % G4["endpoint"]
    )
    assigned = History.load(tmp_path).assigned_at()
    [tracked] = runtimes_from([G4], LOCAL, {G4["endpoint"]: status()}, assigned)
    assert tracked.billed_for(NOW) == (timedelta(minutes=38, seconds=43), True)
    [browser] = runtimes_from([G4], {}, {G4["endpoint"]: status()}, {})
    assert browser.billed_for(NOW) == (status().uptime(NOW), False)  # VM boot: an upper bound


def test_rate_book_skips_reads_that_straddle_a_start_or_stop(tmp_path):
    book = RateBook(tmp_path / "r.json")
    book.observe(Account.parse("me@example.com", ccu(11.89, 2)), runtimes_from([G4], LOCAL, {}))
    book.observe(Account.parse("me@example.com", ccu(0, 1)), runtimes_from([G4], LOCAL, {}))
    assert book.rates == {}


def test_past_session_is_live_only_while_its_endpoint_is_assigned(tmp_path):
    created = '{"timestamp": "%s", "event_type": "session_created", "endpoint": "%s", "variant": "GPU", "accelerator": "G4", "machine_shape": "STANDARD"}\n'
    (tmp_path / "a.jsonl").write_text(created % ("2026-11-17T21:00:00+00:00", "live-ep"))
    (tmp_path / "b.jsonl").write_text(created % ("2026-11-17T20:00:00+00:00", "gone-ep"))
    (tmp_path / "c.jsonl").write_text(
        created % ("2026-11-17T19:00:00+00:00", "pruned-ep")
        + '{"timestamp": "2026-11-17T19:30:00+00:00", "event_type": "session_terminated", "reason": "pruned"}\n'
    )
    a, b, c = History.load(tmp_path).past_sessions()
    assert a.lasted(NOW, {"live-ep"}) == (timedelta(hours=1), "")
    assert b.lasted(NOW, {"live-ep"}) is None  # reclaimed without the CLI noticing
    assert c.lasted(NOW, {"live-ep"}) == (timedelta(minutes=30), "≤")


def test_idle_counts_only_since_assignment(tmp_path):
    a = Account.parse("me@example.com", ccu(10.05, 1))
    idle_since_boot = {G4["endpoint"]: status("idle", "2026-11-17T21:00:00Z")}
    just_assigned = {G4["endpoint"]: NOW - timedelta(minutes=2)}
    [r] = runtimes_from([G4], LOCAL, idle_since_boot, just_assigned)
    assert r.idle_for(NOW) == timedelta(minutes=2)
    assert r.concerns(NOW, resources(0.0)) == []


def test_resources_tolerate_missing_fields():
    """Colab's own client treats every field as optional; a booting or TPU runtime may omit them."""
    r = Resources.parse(NOW, {"gpus": [{}], "disks": [{}]})
    assert (r.ram, r.disk, r.vram, r.gpu_utilization) == ((0, 0), (0, 0), None, None)
    [runtime] = runtimes_from([G4], LOCAL, {G4["endpoint"]: status()})
    assert runtime.concerns(NOW, r) == []


def test_a_rate_change_is_not_a_billing_step(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    log.record(NOW - timedelta(minutes=40), 100.0, 10.0)  # baseline
    log.record(NOW - timedelta(minutes=38), 100.0, 12.0)  # a runtime started: rate moved, balance did not
    log.record(NOW - timedelta(minutes=35), 99.0, 12.0)  # first settled step
    log.record(NOW - timedelta(minutes=5), 94.0, 12.0)
    # from the step at -35m to -5m: 5 CU in 30 minutes. Counting the rate change as a step would give 6 in 33.
    assert log.burn(NOW) == pytest.approx(10.0)


def test_a_missing_gpu_reading_is_a_gap_not_an_idle_gpu():
    r = Resources.parse(NOW, {"gpus": [{"name": "Tesla T4"}]})
    assert r.gpu_utilization is None


def test_damaged_cache_files_do_not_stop_cotop(tmp_path):
    (tmp_path / "u.jsonl").write_text('["2026-11-17T21:00:00+00:00", 100.0, 10.0]\n["2026-11-17T21:05:00+00:00", 99.')
    (tmp_path / "r.json").write_text('{"G4/STANDARD": 10.0')
    assert len(UsageLog(tmp_path / "u.jsonl").samples) == 1
    assert RateBook(tmp_path / "r.json").rates == {}


def test_history_skips_lines_it_cannot_read(tmp_path):
    """The CLI appends as it goes, so the last line can be half written when cotop reads it."""
    created = '{"timestamp": "2026-11-17T20:59:00+00:00", "event_type": "session_created", "endpoint": "e", "variant": "GPU", "accelerator": "G4", "machine_shape": "STANDARD"}\n'
    (tmp_path / "train.jsonl").write_bytes(
        created.encode()
        + b'[]\n"text"\nnull\n{"event_type": "execution"}\n'
        + b'{"timestamp": "2026-11-17T21:00:00", "event_type": "execution"}\n'  # no time zone
        + b'{"timestamp": "2026-11-17T21:01:00+00:00", "event_type": "session_created"}\n'  # no endpoint
        + b'{"timestamp": "2026-11-17T21:02:00+00:00", "event_type": 7}\n'
        + b'{"timestamp": 1763413380, "event_type": "execution"}\n'
        + b'{"timestamp": "2026-11-17T21:04:00+00:00", "event_type": "session_created", "endpoint": "f", "machine_shape": "STANDARD"}\n'
        + b'{"timestamp": "2026-11-17T21:05:00+00:00", "event_type": "session_created", "endpoint": "g", "accelerator": "G4"}\n'
        + b'\xff\xfe{"timestamp": "2026-11-17T21:03:00+00:00", "event_ty'
    )
    h = History.load(tmp_path)
    assert [e.summary for e in h.recent("train")] == ["created"]
    assert h.assigned_at() == {"e": parse_time("2026-11-17T20:59:00+00:00")}
    assert [p.endpoint for p in h.past_sessions()] == ["e"]
