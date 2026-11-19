"""Fixtures follow the shapes of real Colab responses; every value is made up."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cotop.model import (
    Account,
    History,
    Basis,
    Rate,
    RateBook,
    Resources,
    Samples,
    Snapshot,
    Status,
    UsageLog,
    parse_json,
    parse_time,
    runtimes_from,
)

NOW = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def snapshot(runtimes, rate, count=None):
    """What one poll read: the account's rate and count, then the runtime list."""
    count = len(runtimes) if count is None else count
    return Snapshot(NOW, Account.parse("me@example.com", ccu(rate, count)), runtimes, History({}))


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
    [r] = runtimes = runtimes_from([G4], LOCAL, {G4["endpoint"]: status()})
    assert (r.name, r.hardware, r.shape, r.running, r.token) == ("train", "G4", "STANDARD", "run(train.py)", "t")
    rate = RateBook(tmp_path / "r.json").rate(r, snapshot(runtimes, 10.05))
    assert (rate.hourly, rate.basis) == (10.05, Basis.REPORTED)
    assert "token=" not in repr(r)  # the proxy token never reaches a log line or the screen


def test_mixed_hardware_does_not_give_every_row_the_account_total(tmp_path):
    s = snapshot(runtimes_from([G4, T4], LOCAL, {}), 11.89)
    book = RateBook(tmp_path / "r.json")
    assert [book.rate(r, s).basis for r in s.runtimes] == [Basis.UNKNOWN, Basis.UNKNOWN]

    for alone in (snapshot(runtimes_from([G4], LOCAL, {}), 10.05), snapshot(runtimes_from([T4, T4], {}, {}), 3.68)):
        book.observe(alone)
        book.observe(alone)  # the next poll agrees
    assert [book.rate(r, s).hourly for r in s.runtimes] == [10.05, 1.84]
    assert RateBook(tmp_path / "r.json").rates == {"G4/STANDARD": 10.05, "T4/STANDARD": 1.84}


def test_rate_book_learns_nothing_from_a_mixed_set(tmp_path):
    book = RateBook(tmp_path / "r.json")
    book.observe(snapshot(runtimes_from([G4, T4], LOCAL, {}), 11.89))
    assert book.rates == {}


def test_same_kind_runtimes_split_the_total(tmp_path):
    s = snapshot(runtimes_from([T4, assignment("gpu-t4-s-def", "T4")], {}, {}), 3.68)
    rate = RateBook(tmp_path / "r.json").rate(s.runtimes[0], s)
    assert (rate.hourly, rate.basis) == (1.84, Basis.SPLIT)


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
    assert log.burn(start + timedelta(minutes=30)) == pytest.approx(10.08)  # three minutes after the last step
    assert UsageLog(tmp_path / "u.jsonl").samples == log.samples


def test_burn_ignores_top_ups_and_short_windows(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    log.record(NOW - timedelta(minutes=30), 100.0, 10.0)
    log.record(NOW - timedelta(minutes=25), 99.0, 10.0)
    assert log.burn(NOW) is None  # one settled step is not a rate
    log.record(NOW - timedelta(minutes=10), 600.0, 10.0)
    assert log.burn(NOW) is None


def test_a_top_up_in_the_window_counts_as_no_charge_not_a_refund(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    balance = 200.0
    log.record(NOW - timedelta(minutes=58), balance, 15.0)  # baseline
    for k in range(1, 12):  # 1.25 every 5 minutes is 15 an hour
        balance += 10.0 - 1.25 if k == 6 else -1.25  # 10 arrive in the sixth step
        log.record(NOW - timedelta(minutes=58 - 5 * k), balance, 15.0)
    # the top-up's step hides its own charge, so nine steps' charge over the 50 minutes: a floor
    assert log.burn(NOW) == pytest.approx(9 * 1.25 / (50 / 60))


def test_burn_after_a_stop_counts_the_quiet_time_since(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    log.record(NOW - timedelta(minutes=59), 300.0, 15.0)  # baseline
    for k in range(10):  # 0.5 every 2 minutes is 15 an hour, from -58m to -40m
        log.record(NOW - timedelta(minutes=58 - 2 * k), 299.5 - 0.5 * k, 15.0)
    log.record(NOW - timedelta(minutes=39), 295.0, 0.0)  # stopped
    assert log.burn(NOW - timedelta(minutes=39)) == pytest.approx(15.0)  # a step may still be on its way
    # no step for 40 minutes against 2-minute steps: 4.5 charged from -58m until one step after the last
    assert log.burn(NOW) == pytest.approx(4.5 / (56 / 60))


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


def test_history_reads_fields_of_the_wrong_type_as_missing(tmp_path):
    """A field that is not text must not reach the screen, where a list breaks Path() and dict lookups."""
    (tmp_path / "train.jsonl").write_text(
        SECRET_HISTORY.splitlines(keepends=True)[0]
        + '{"timestamp": "2026-11-17T21:02:00+00:00", "event_type": "file_operation", "op": "download", "remote": ["a", "b"]}\n'
        '{"timestamp": "2026-11-17T21:30:00+00:00", "event_type": "session_terminated", "reason": ["x"]}\n'
    )
    h = History.load(tmp_path)
    assert [e.summary for e in h.recent("train")] == ["terminated (?)", "download ", "created"]
    assert [p.reason for p in h.past_sessions()] == [None]


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


def test_a_read_that_straddles_a_start_or_stop_gives_no_exact_rate(tmp_path):
    """The count says the account's rate covers other runtimes than the ones listed."""
    [t4] = runtimes = runtimes_from([T4], {}, {})
    straddling = [snapshot(runtimes, 16.81, count=2), snapshot(runtimes, 0.0, count=0)]
    assert [RateBook(tmp_path / "r.json").rate(t4, s) for s in straddling] == [Rate(None, Basis.UNKNOWN)] * 2
    (tmp_path / "r.json").write_text('{"T4/STANDARD": 1.84}')
    assert [RateBook(tmp_path / "r.json").rate(t4, s) for s in straddling] == [Rate(1.84, Basis.LEARNED)] * 2


def test_spend_is_an_upper_bound_only_when_rate_and_time_both_are():
    since_boot, since_assignment = (timedelta(hours=2), False), (timedelta(hours=2), True)
    assert Rate(1.5, Basis.REPORTED).spend(since_boot) == (3.0, "≤")
    assert Rate(1.5, Basis.REPORTED).spend(since_assignment) == (3.0, "≈")  # billing may start at VM boot
    assert Rate(1.5, Basis.LEARNED).spend(since_boot) == (3.0, "≈")  # a learned rate can be low
    assert Rate(0.0, Basis.REPORTED).spend(since_boot) == (0.0, "≤")
    assert Rate(None, Basis.UNKNOWN).spend(since_boot) is None


def test_rate_book_skips_reads_that_straddle_a_start_or_stop(tmp_path):
    book = RateBook(tmp_path / "r.json")
    for straddling in (snapshot(runtimes_from([G4], LOCAL, {}), 11.89, count=2), snapshot(runtimes_from([G4], LOCAL, {}), 0, count=1)):
        book.observe(straddling)
        book.observe(straddling)
    assert book.rates == {}


def test_a_kind_swap_between_the_reads_teaches_nothing(tmp_path):
    """An H100 stops and a T4 starts between the account read and the list: the count still matches."""
    book = RateBook(tmp_path / "r.json")
    h100, t4 = runtimes_from([assignment("gpu-h100-s-x", "H100")], {}, {}), runtimes_from([T4], {}, {})
    for s in (snapshot(h100, 15.04), snapshot(h100, 15.04), snapshot(t4, 15.04)):
        book.observe(s)
    assert RateBook(tmp_path / "r.json").rates == {"H100/STANDARD": 15.04}
    book.observe(snapshot(t4, 1.84))
    book.observe(snapshot(t4, 1.84))  # two polls in a row agree
    assert RateBook(tmp_path / "r.json").rates == {"H100/STANDARD": 15.04, "T4/STANDARD": 1.84}


def test_learned_rates_are_scaled_to_add_up_to_the_account_total(tmp_path):
    (tmp_path / "r.json").write_text('{"G4/STANDARD": 5.0, "T4/STANDARD": 1.0}')  # learned when both cost less
    book = RateBook(tmp_path / "r.json")
    s = snapshot(runtimes_from([G4, T4], LOCAL, {}), 12.0)
    assert [book.rate(r, s) for r in s.runtimes] == [Rate(10.0, Basis.LEARNED), Rate(2.0, Basis.LEARNED)]
    straddling = snapshot(s.runtimes, 12.0, count=3)  # the total covers a runtime not listed
    assert [book.rate(r, straddling).hourly for r in s.runtimes] == [5.0, 1.0]
    partly_known = snapshot(runtimes_from([G4, T4, CPU], LOCAL, {}), 12.0)  # the CPU's share is not known
    assert [book.rate(r, partly_known).hourly for r in partly_known.runtimes] == [5.0, 1.0, None]


def test_past_session_is_live_only_while_its_endpoint_is_assigned(tmp_path):
    created = '{"timestamp": "%s", "event_type": "session_created", "endpoint": "%s", "variant": "GPU", "accelerator": "G4", "machine_shape": "STANDARD"}\n'
    (tmp_path / "a.jsonl").write_text(created % ("2026-11-17T21:00:00+00:00", "live-ep"))
    (tmp_path / "b.jsonl").write_text(created % ("2026-11-17T20:00:00+00:00", "gone-ep"))
    (tmp_path / "c.jsonl").write_text(
        created % ("2026-11-17T19:00:00+00:00", "pruned-ep")
        + '{"timestamp": "2026-11-17T19:30:00+00:00", "event_type": "session_terminated", "reason": "pruned"}\n'
    )
    a, b, c = History.load(tmp_path).past_sessions()
    assert a.lasted(NOW, {"live-ep"}) == (timedelta(hours=1), True)
    assert b.lasted(NOW, {"live-ep"}) is None  # reclaimed without the CLI noticing
    assert c.lasted(NOW, {"live-ep"}) == (timedelta(minutes=30), False)  # pruned: an upper bound


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


def test_damaged_cache_files_do_not_stop_cotop(tmp_path):
    (tmp_path / "u.jsonl").write_text('["2026-11-17T21:00:00+00:00", 100.0, 10.0]\n["2026-11-17T21:05:00+00:00", 99.')
    (tmp_path / "r.json").write_text('{"G4/STANDARD": 10.0')
    assert len(UsageLog(tmp_path / "u.jsonl").samples) == 1
    assert RateBook(tmp_path / "r.json").rates == {}
    (tmp_path / "r.json").write_text('{"G4/STANDARD": "10.0"}')
    assert RateBook(tmp_path / "r.json").rates == {}


def test_a_balance_that_is_not_a_number_never_reaches_the_usage_log(tmp_path):
    """0.3.x wrote one string balance, and every burn for the next hour failed, even after a restart."""
    (tmp_path / "u.jsonl").write_text(
        '["2026-11-17T21:00:00+00:00", 5230.0, 10.05]\n["2026-11-17T21:05:00+00:00", "5227.43", 10.05]\n'
        '["2026-11-17T21:06:00+00:00", NaN, 10.05]\n{"balance": 1}\n'
    )
    log = UsageLog(tmp_path / "u.jsonl")
    assert [b for _, b, _ in log.samples] == [5230.0]
    for balance, rate in (("5227.43", 10.05), (5227.0, None), (True, 10.05)):
        with pytest.raises(ValueError):
            log.record(NOW, balance, rate)
    for minutes, balance in ((10, 5229.0), (20, 5227.3), (30, 5225.6)):
        log.record(NOW - timedelta(minutes=60 - minutes), balance, 10.05)
    assert log.burn(NOW - timedelta(minutes=25)) == pytest.approx(10.2)  # five minutes after the last step


def test_a_failed_cache_write_is_kept_not_raised_and_leaves_no_temp_file(tmp_path):
    (tmp_path / "cache").mkdir()
    log = UsageLog(tmp_path / "cache" / "u.jsonl")
    (tmp_path / "cache" / "u.jsonl").mkdir()  # os.replace onto a directory fails
    log.record(NOW, 100.0, 10.0)
    assert isinstance(log.unsaved, OSError) and log.samples == [(NOW, 100.0, 10.0)]
    assert [p.name for p in (tmp_path / "cache").iterdir()] == ["u.jsonl"]
    (tmp_path / "cache" / "u.jsonl").rmdir()
    log.record(NOW, 100.0, 10.0)  # the same sample: still written once the disk takes it
    assert log.unsaved is None and len(UsageLog(tmp_path / "cache" / "u.jsonl").samples) == 1

    book = RateBook(tmp_path / "cache" / "r.json")
    (tmp_path / "cache" / "r.json").mkdir()
    g4 = snapshot(runtimes_from([G4], LOCAL, {}), 10.05)
    book.observe(g4)
    book.observe(g4)
    assert isinstance(book.unsaved, OSError)
    (tmp_path / "cache" / "r.json").rmdir()
    book.observe(g4)
    assert book.unsaved is None and RateBook(tmp_path / "cache" / "r.json").rates == {"G4/STANDARD": 10.05}


def test_whole_numbers_and_zeros_are_numbers():
    """Over-rejecting blanks a whole reading or refresh, so the plain values JSON sends as ints must pass."""
    a = Account.parse("me@example.com", {"currentBalance": 5227, "consumptionRateHourly": 0, "assignmentsCount": 0})
    assert (a.balance, a.rate_hourly, a.eligible_gpus, a.runway) == (5227, 0, (), None)
    for util in (0, 1, 0.0, 1.0):
        assert Resources.parse(NOW, {"gpus": [{"gpuUtilization": util}]}).gpu_utilization == util
    for bad in (True, 85, -0.1, "0.5"):
        with pytest.raises(ValueError):
            Resources.parse(NOW, {"gpus": [{"gpuUtilization": bad}]})
    with pytest.raises(ValueError, match="not a JSON number"):
        parse_json('{"gpuUtilization": NaN}')


def wrong(body: dict, *path, value):
    body = parse_json(json.dumps(body))
    node = body
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return body


GOOD_CCU = ccu(10.05, 1)
GOOD_READING = parse_json((FIXTURES / "resources_g4.txt").read_text())
GOOD_KERNELS = [{"execution_state": "busy", "last_activity": "2026-11-17T21:59:00Z", "connections": 1}]


@pytest.mark.parametrize(
    "parse",
    [
        lambda: Account.parse(None, GOOD_CCU),
        lambda: Account.parse("me@example.com", wrong(GOOD_CCU, "assignmentsCount", value=True)),
        lambda: Account.parse("me@example.com", wrong(GOOD_CCU, "assignmentsCount", value=-1)),
        lambda: Account.parse("me@example.com", wrong(GOOD_CCU, "currentBalance", value=float("inf"))),
        lambda: Account.parse("me@example.com", wrong(GOOD_CCU, "currentBalance", value=10**400)),
        lambda: Account.parse("me@example.com", wrong(GOOD_CCU, "eligibleTpus", value="V6E1")),
        lambda: Resources.parse(NOW, wrong(GOOD_READING, "memory", "totalBytes", value=True)),
        lambda: Resources.parse(NOW, wrong(GOOD_READING, "disks", 0, "filesystem", "usedBytes", value=-1)),
        lambda: Resources.parse(NOW, wrong(GOOD_READING, "gpus", 0, "memoryUsedBytes", value=2**63)),
        lambda: Resources.parse(NOW, wrong(GOOD_READING, "gpus", 0, "memoryTotalBytes", value=1.5)),
        lambda: Status.parse({"started": "2026-11-17T20:59:46Z"}, wrong(GOOD_KERNELS, 0, "execution_state", value=None)),
        lambda: Status.parse({"started": 1763413186}, GOOD_KERNELS),
        lambda: runtimes_from([wrong(G4, "endpoint", value=7)], {}, {}),
        lambda: runtimes_from([wrong(G4, "machineShape", value="HIGH_RAM")], {}, {}),
        lambda: runtimes_from([wrong(G4, "runtimeProxyInfo", "url", value=None)], {}, {}),
        lambda: runtimes_from([wrong(G4, "runtimeProxyInfo", "token", value=7)], {}, {}),
    ],
)
def test_a_wrong_type_is_turned_away_where_it_is_parsed(parse):
    """Fixed cases for what the fuzz target finds only on some draws."""
    with pytest.raises(ValueError):
        parse()


def test_a_runway_too_long_for_a_date_is_not_an_account():
    """timedelta and the "until" date overflow long before a finite balance or rate does."""
    for balance, rate in ((5227.43, 5e-324), (1e6, 1e-6), (-1e300, 1.0)):
        with pytest.raises(ValueError, match="is not an account"):
            Account.parse("me@example.com", {"currentBalance": balance, "consumptionRateHourly": rate, "assignmentsCount": 1})


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
