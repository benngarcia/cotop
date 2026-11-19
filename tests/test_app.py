"""The dashboard rendered from fixture payloads, with no network and temp state dirs."""

import copy
import json
import math
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import cotop.colab
from cotop.app import Dashboard, HistoryScreen, RuntimePanel, Runtimes, Snapshot, account_text, meters, state_text
from cotop.colab import Colab
from rich.color import Color as RichColor
from rich.style import Style

from cotop.widgets import RED, sparkline
from cotop.model import Account, History, PastSession, RateBook, Resources, Status, parse_json, runtimes_from, utc_now
from tests.test_model import CPU, FIXTURES, G4, LOCAL, NOW, SECRET_HISTORY, T4, assignment, ccu, resources, status


class StubColab:
    def __init__(self, assignments, status, readings, rate):
        self._assignments, self._status, self._readings, self._rate = assignments, status, readings, rate
        self.local = LOCAL
        self.fail = False
        self.calls = []

    def sessions(self):
        return self.local

    def history(self):
        return History({})

    def email(self):
        return "me@example.com"

    def ccu_info(self):
        if self.fail:
            raise ConnectionError("ccu-info timed out")
        return ccu(self._rate, len(self._assignments))

    def assignments(self):
        return self._assignments

    def status(self, assignments):
        return self._status

    def resources(self, runtimes):
        return {r.endpoint: self._readings.get(r.endpoint) for r in runtimes}

    def stop(self, runtime):
        self.calls.append(("stop", runtime.endpoint))
        return "[colab] Session terminated."

    def restart_kernel(self, runtime):
        self.calls.append(("restart_kernel", runtime.endpoint))
        return "done"


@pytest.fixture
def isolated(tmp_path):
    """The cache dir for one test; nothing else is read from or written to disk."""
    return tmp_path / "cache"


async def settle(app, pilot):
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def learned(cache, rates):
    """Rates cotop learned on an earlier run, as RateBook stores them."""
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "rates.json").write_text(json.dumps(rates))


def cli_history(directory, **sessions):
    """The CLI's history directory, one JSONL file per session name, read the way Colab.history reads it."""
    directory.mkdir()
    for name, events in sessions.items():
        (directory / f"{name}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return History.load(directory)


def created(at, endpoint):
    return {"timestamp": at.isoformat(), "event_type": "session_created", "endpoint": endpoint, "accelerator": "G4", "machine_shape": "STANDARD"}


def mixed():
    return StubColab(
        [G4, T4, CPU],
        {
            G4["endpoint"]: status("idle", "2026-11-17T21:35:00Z"),
            T4["endpoint"]: status(),
            CPU["endpoint"]: ConnectionError("proxy said 502"),
        },
        {G4["endpoint"]: resources(0.0), T4["endpoint"]: resources(1.0)},
        rate=11.96,
    )


async def test_mixed_untracked_unreachable_and_idle(isolated):
    app = Dashboard(mixed(), isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        table = app.query_one(Runtimes)
        assert table.row_count == 3
        states = [str(table.get_row_at(i)[5]) for i in range(3)]
        assert states[0].startswith("idle") and states[1] == "busy" and states[2] == "unreachable"
        assert [str(table.get_row_at(i)[3]) for i in range(3)] == ["?", "?", "?"]  # mixed: no rate known
        assert not (isolated / "rates.json").exists()
        panel = app.query_one(RuntimePanel)
        assert panel.border_title == "train  G4"
        await pilot.press("down")
        await settle(app, pilot)
        assert panel.border_title == "untracked  T4"


async def test_failed_refresh_keeps_the_last_data(isolated):
    colab = mixed()
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        colab.fail = True
        await pilot.press("r")
        await settle(app, pilot)
        assert any("Refresh failed: ConnectionError: ccu-info timed out." in str(n.message) for n in app._notifications)
        assert app.query_one(Runtimes).row_count == 3


async def test_an_error_message_is_shown_as_text_not_markup(isolated):
    """Exception text such as "[/]" is not Textual markup; parsing it as markup exits cotop when the toast draws."""
    colab = mixed()
    colab.ccu_info = lambda: (_ for _ in ()).throw(ValueError("unexpected '[/]' in the reply"))
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46), notifications=True) as pilot:
        await settle(app, pilot)
        await pilot.pause(0.2)
        assert any("ValueError: unexpected '[/]' in the reply." in str(n.message) for n in app._notifications)
        assert app.is_running


def secret_colab(tmp_path):
    """One runtime whose CLI history holds code, outputs and a stdin reply, all canaries."""
    (tmp_path / "history").mkdir()
    (tmp_path / "history" / "train.jsonl").write_text(SECRET_HISTORY)
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    colab.history = lambda: History.load(tmp_path / "history")
    return colab


async def test_a_crash_report_never_shows_cli_history(isolated, tmp_path, monkeypatch, capfd):
    """Textual prints every frame's locals when cotop crashes; the snapshot in them must hold no code, outputs or stdin."""

    def crash(*_):
        raise RuntimeError("planted")

    monkeypatch.setattr(PastSession, "lasted", crash)  # a bug in a screen, outside any error boundary
    app = Dashboard(secret_colab(tmp_path), isolated)
    with pytest.raises(RuntimeError, match="planted"):
        async with app.run_test(size=(150, 46)) as pilot:
            await settle(app, pilot)
            await pilot.press("h")
            await settle(app, pilot)
    report = capfd.readouterr().err
    assert "history=History(" in report  # the report did print the snapshot
    assert "SECRET" not in report


async def test_no_runtimes(isolated):
    app = Dashboard(StubColab([], {}, {}, rate=0), isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        assert "nothing running" in str(app.query_one("#usage-text").render())
        assert "No runtimes" in str(app.query_one("#empty").render())


async def test_stop_needs_a_yes_and_acts_on_the_selected_runtime(isolated):
    colab = StubColab([G4, T4], {G4["endpoint"]: status(), T4["endpoint"]: status()}, {}, rate=11.89)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("s")
        assert "Stop train (G4)? Its VM and files will be deleted. It is busy running run(train.py)." in str(
            app.screen.query_one("Label").render()
        )
        await pilot.press("enter")  # Cancel has focus
        await pilot.press("s", "n")
        await pilot.press("s", "escape")
        await pilot.press("down", "s", "y")
        await settle(app, pilot)
        assert colab.calls == [("stop", T4["endpoint"])]


async def test_stop_acts_on_the_row_shown_when_the_table_order_differs_from_colabs(isolated):
    learned(isolated, {"T4/STANDARD": 1.84, "G4/STANDARD": 10.05})
    colab = StubColab([T4, G4], {T4["endpoint"]: status(), G4["endpoint"]: status()}, {}, rate=11.89)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        assert str(app.query_one(Runtimes).get_row_at(0)[0]) == "train"  # the G4 sorts first: it costs more
        await pilot.press("s", "y")
        await settle(app, pilot)
        assert colab.calls == [("stop", G4["endpoint"])]


async def test_stop_acts_on_the_runtime_the_prompt_named_even_if_the_list_changes(isolated):
    colab = StubColab([G4, T4], {G4["endpoint"]: status(), T4["endpoint"]: status()}, {}, rate=11.89)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("s")
        assert "Stop train (G4)?" in str(app.screen.query_one("Label").render())
        colab._assignments = [T4]  # train ends while the prompt is open
        app.action_refresh()
        await settle(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert colab.calls == [("stop", G4["endpoint"])]  # which Colab.stop then refuses as gone


async def test_kernel_and_ssh_actions_refuse_untracked_runtimes(isolated):
    colab = StubColab([T4], {T4["endpoint"]: status()}, {}, rate=1.84)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("R", "x")
        await pilot.pause()
        assert colab.calls == [] and len(app._notifications) == 2
        assert app.screen is app.screen_stack[0]


async def test_history_screen_opens_and_closes(isolated):
    app = Dashboard(mixed(), isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("h")
        await pilot.pause()
        assert type(app.screen).__name__ == "HistoryScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


async def test_the_history_screen_marks_bounds_and_costs_live_sessions(isolated, tmp_path):
    learned(isolated, {"G4/STANDARD": 10.0})
    now = utc_now()
    pruned = {"timestamp": (now - timedelta(hours=4)).isoformat(), "event_type": "session_terminated", "reason": "pruned"}
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.0)
    history = cli_history(
        tmp_path / "history",
        train=[created(now - timedelta(hours=1), G4["endpoint"])],
        old=[created(now - timedelta(hours=5), "gone"), pruned],
    )
    colab.history = lambda: history
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("h")
        await pilot.pause()
        table = app.screen.query_one("DataTable")
        rows = [[str(c) for c in table.get_row_at(i)] for i in range(2)]
        assert [r[:2] + r[3:] for r in rows] == [  # all but Started, which is local time
            ["train", "G4", "1h00m", "≈10.0", "running"],
            ["old", "G4", "≤1h00m", "≤10.0", "gone"],
        ]


async def test_the_activity_panel_shows_history_without_code(isolated, tmp_path):
    """SECURITY.md: from the CLI's history, cotop never shows code, outputs or prompt replies."""
    execution = {"timestamp": utc_now().isoformat(), "event_type": "execution", "cell_index": 3, "code": "SECRET_CODE", "outputs": ["SECRET_OUT"]}
    reply = {"timestamp": utc_now().isoformat(), "event_type": "input_reply", "value": "SECRET_PASSWORD"}
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    history = cli_history(tmp_path / "history", train=[execution, reply])
    colab.history = lambda: history
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        activity = str(app.query_one(RuntimePanel).query_one(".activity").render())
        assert "exec cell 3" in activity and "input_reply" in activity and "SECRET" not in activity


async def test_estimates_and_upper_bounds_are_marked_and_spend_is_rate_times_time(isolated):
    """README: an estimated rate is marked ≈, and time since VM boot is an upper bound, ≤."""
    t4 = assignment("gpu-t4-s-def", "T4")
    colab = StubColab([T4, t4], {T4["endpoint"]: status(), t4["endpoint"]: status()}, {}, rate=3.68)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        up = app.snapshot.taken_at - status().started  # untracked: billed since VM boot at most
        spent = f"≤{1.84 * up.total_seconds() / 3600:.1f}"
        table = app.query_one(Runtimes)
        cells = {column: str(table.get_row_at(0)[i]) for i, column in enumerate(table.columns)}
        assert (cells["CU/hr"], cells["Spent"], cells["Up"][0]) == ("≈1.84", spent, "≤")
        facts = str(app.query_one(RuntimePanel).query_one(".facts").render())
        assert f"spent {spent}  at ≈1.84/hr" in facts and "up ≤" in facts


async def test_a_failed_reading_is_kept_as_a_gap(isolated):
    app = Dashboard(StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05), isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        app.add_readings(utc_now(), {G4["endpoint"]: resources(0.5)})
        app.add_readings(utc_now(), {G4["endpoint"]: None})
        assert app.samples.series(G4["endpoint"], lambda r: r.gpu_utilization)[-2:] == [0.5, None]


async def test_selection_survives_the_runtime_list_changing(isolated):
    """A selection below row 0 must stay put, not flip between runtimes, when the list changes."""
    colab = mixed()
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("down")  # T4, on row 1
        await settle(app, pilot)
        new = {**G4, "endpoint": "gpu-a100-new", "accelerator": "A100"}
        colab._assignments = [new, G4, T4, CPU]  # a runtime appears in front
        colab._status = {**colab._status, "gpu-a100-new": status()}
        await pilot.press("r")
        await settle(app, pilot)
        titles = set()
        for _ in range(10):
            await pilot.pause(0.05)
            titles.add(app.query_one(RuntimePanel).border_title)
        assert titles == {"untracked  T4"}
        assert app.selected_runtime().endpoint == T4["endpoint"]


async def test_readings_arriving_while_the_selection_moves(isolated):
    app = Dashboard(mixed(), isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        for step in range(6):
            await pilot.press("down" if step % 2 == 0 else "up")
            app.add_readings(utc_now(), {G4["endpoint"]: resources(0.3), T4["endpoint"]: None})
            await pilot.pause()
        assert app.is_running


async def empty_then_two(app, pilot, colab, empty_at_launch):
    if not empty_at_launch:
        await settle(app, pilot)
        colab._assignments, colab._status = [], {}
        await pilot.press("r")
    await settle(app, pilot)
    colab._assignments, colab._status = [G4, T4], {G4["endpoint"]: status(), T4["endpoint"]: status()}
    await pilot.press("r")
    await settle(app, pilot)


@pytest.mark.parametrize("empty_at_launch", [True, False])
async def test_arrows_move_the_selection_after_the_list_was_empty(isolated, empty_at_launch):
    colab = StubColab([] if empty_at_launch else [G4], {} if empty_at_launch else {G4["endpoint"]: status()}, {}, rate=0)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await empty_then_two(app, pilot, colab, empty_at_launch)
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one(Runtimes)
        assert app.selected_runtime().endpoint == T4["endpoint"]
        assert app.query_one(RuntimePanel).border_title == "untracked  T4"


async def test_ssh_after_the_empty_state_targets_the_highlighted_row(isolated):
    """x is the one key with no confirmation, so it gets its own test."""
    colab = StubColab([], {}, {}, rate=0)
    colab.local = {**LOCAL, "eval": {"name": "eval", "endpoint": T4["endpoint"], "running": None}}

    def ssh_command(runtime):
        colab.calls.append(("ssh", runtime.endpoint))
        raise RuntimeError("stubbed: no shell in tests")

    colab.ssh_command = ssh_command
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await empty_then_two(app, pilot, colab, empty_at_launch=True)
        table = app.query_one(Runtimes)
        second = table.coordinate_to_cell_key((1, 0)).row_key.value
        await pilot.press("down", "x")
        await settle(app, pilot)
        assert colab.calls == [("ssh", second)]


async def test_the_panel_follows_a_poll_without_a_cursor_move(isolated):
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        assert app.query_one(RuntimePanel).border_subtitle.startswith("busy")
        colab._status = {G4["endpoint"]: status("idle", "2026-11-17T21:59:00Z")}
        await pilot.press("r")
        await settle(app, pilot)
        assert app.query_one(RuntimePanel).border_subtitle.startswith("idle")


async def test_runtime_keys_do_nothing_on_the_history_screen(isolated):
    colab = mixed()
    colab.local = {**LOCAL, "eval": {"name": "eval", "endpoint": T4["endpoint"], "running": None}}

    def ssh_command(runtime):
        colab.calls.append(("ssh", runtime.endpoint))
        raise RuntimeError("stubbed: no shell in tests")

    colab.ssh_command = ssh_command
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("h", "x", "s", "R")
        await settle(app, pilot)
        assert type(app.screen).__name__ == "HistoryScreen"
        assert colab.calls == []


async def test_the_demo_runs_and_sorts_the_most_expensive_first(tmp_path):
    from cotop.app import demo_dashboard

    app = demo_dashboard(tmp_path)
    async with app.run_test(size=(138, 40)) as pilot:
        await settle(app, pilot)
        table = app.query_one(Runtimes)
        assert table.row_count == 12 and table.border_title == "Runtimes (12)"
        rates = [float(str(table.get_row_at(i)[3]).lstrip("≈")) for i in range(table.row_count)]
        assert rates == sorted(rates, reverse=True)
        axis = str(app.query_one(RuntimePanel).query_one("TimeAxis").render())
        assert "m ago" in axis and axis.endswith("now")  # the selected runtime's oldest visible reading
        assert "Up" in table.columns
        await pilot.resize_terminal(80, 24)
        await settle(app, pilot)
        assert "Up" not in table.columns  # too narrow: the panel still shows uptime


async def test_compute_units_turn_red_when_they_last_under_a_day(isolated):
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        assert not balance_is_red(app)  # 5,227 left at 10.05 an hour lasts weeks
        colab._rate = 300.0  # now it lasts under a day
        await pilot.press("r")
        await settle(app, pilot)
        assert balance_is_red(app)


def balance_is_red(app) -> bool:
    text = app.query_one("#usage-text").render()
    span = next(s for s in text.spans if text.plain[s.start:s.end].startswith("5,227"))
    color = Style.parse(str(span.style)).color
    return color is not None and color.get_truecolor() == RichColor.parse(RED).get_truecolor()


async def test_vim_keys_move_and_restart_needs_shift_r(isolated):
    colab = StubColab([G4, T4], {G4["endpoint"]: status(), T4["endpoint"]: status()}, {}, rate=11.89)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        table = app.query_one(Runtimes)
        await pilot.press("j")
        assert table.cursor_row == 1
        await pilot.press("k")
        assert table.cursor_row == 0 and app.screen is app.screen_stack[0]  # k moves; it no longer restarts
        await pilot.press("R")
        assert "Restart train's kernel?" in str(app.screen.query_one("Label").render())
        await pilot.press("n", "question_mark")
        assert app.screen.query("HelpPanel")
        await pilot.press("question_mark")
        assert not app.screen.query("HelpPanel")


async def test_a_failed_ssh_refreshes_like_any_other_action(isolated):
    """x on a stale name fails, and the list must not stay stale until the next tick."""
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=11.89)
    polls = []
    assignments = colab.assignments
    colab.assignments = lambda: polls.append(1) or assignments()

    def ssh_command(runtime):
        raise RuntimeError("train is no longer this runtime; refresh and retry")

    colab.ssh_command = ssh_command
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        before = len(polls)
        await pilot.press("x")
        await settle(app, pilot)
        assert len(polls) == before + 1


async def test_the_demo_connects_to_nothing_and_runs_nothing(tmp_path, monkeypatch):
    import socket
    import subprocess

    from cotop.app import demo_dashboard

    attempts = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a: attempts.append(("connect", a)))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: attempts.append(("run", a)))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: attempts.append(("popen", a)))
    app = demo_dashboard(tmp_path)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("s", "y", "R", "y", "x", "h", "escape", "r")
        await settle(app, pilot)
    assert attempts == []


async def test_ssh_runs_the_command_line_unchanged(isolated, monkeypatch):
    import contextlib

    import cotop.app

    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=11.89)
    expected = ["/venv/bin/colab", "ssh", "-s", "train"]
    colab.ssh_command = lambda runtime: list(expected)  # a fresh list, so a change made in place shows
    ran = []
    monkeypatch.setattr(cotop.app.subprocess, "run", lambda a, **k: ran.append(list(a)) or type("Done", (), {"returncode": 0})())
    app = Dashboard(colab, isolated)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("x")
        await settle(app, pilot)
    assert ran == [expected]


async def test_a_refresh_asked_for_during_a_poll_runs_after_it(isolated):
    """A stop that lands while a poll is reading must not wait for the next 15 s tick."""
    import threading

    colab = StubColab([G4, T4], {G4["endpoint"]: status(), T4["endpoint"]: status()}, {}, rate=11.89)
    gate, polls = threading.Event(), []
    assignments = colab.assignments

    def slow_assignments():
        polls.append(1)
        if len(polls) == 2:
            gate.wait(5)  # the second poll is still reading when the stop finishes
        return assignments()

    colab.assignments = slow_assignments
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        app.action_refresh()  # poll 2, held at the gate
        await pilot.pause()
        await pilot.press("s", "y")  # its refresh arrives while poll 2 runs
        await pilot.pause(0.3)
        gate.set()
        await settle(app, pilot)
        assert len(polls) == 3
        assert colab.calls == [("stop", G4["endpoint"])]


def test_the_demo_signs_in_to_nothing_and_leaves_the_cache_alone(tmp_path, monkeypatch):
    import cotop.app
    import cotop.colab

    home_cache = tmp_path / "home" / ".cache" / "cotop"
    monkeypatch.setattr(cotop.app, "CACHE", home_cache)
    monkeypatch.setattr(cotop.colab, "cli_sign_in", lambda: (_ for _ in ()).throw(AssertionError("signed in")))
    used = []
    monkeypatch.setattr(cotop.app.Dashboard, "run", lambda self: used.append(self))
    cotop.app.run_demo()
    [app] = used
    assert home_cache not in (app.rates.path.parent, app.usage.path.parent)
    assert not (tmp_path / "home").exists()


async def test_a_second_x_checks_the_name_right_before_its_own_shell(isolated, monkeypatch):
    """SECURITY.md: x pressed twice must not open the second shell on a check made before the first one closed."""
    import contextlib
    import time

    import cotop.app
    from cotop.colab import RuntimeGone, still_assigned

    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    reused = assignment("gpu-g4-s-reused", "G4")

    def ssh_command(runtime):
        time.sleep(0.3)  # the check reads the assignments from Colab
        if not still_assigned(runtime, colab.assignments(), colab.sessions()):
            raise RuntimeGone(f"{runtime.name} is no longer this runtime; refresh and retry")
        return ["colab", "ssh", "-s", runtime.name]

    shells = []

    def shell(argv, **kwargs):
        shells.append(colab.local["train"]["endpoint"])  # the VM the name means as the shell opens
        time.sleep(0.6)  # the shell is open
        colab._assignments = [reused]  # meanwhile train is stopped and its name reused
        colab.local = {"train": {"name": "train", "endpoint": reused["endpoint"], "running": None}}
        colab.fail = True  # and the refresh after the shell has not landed when the next x is read
        return type("Done", (), {"returncode": 0})()

    colab.ssh_command = ssh_command
    monkeypatch.setattr(cotop.app.subprocess, "run", shell)
    app = Dashboard(colab, isolated)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("x")
        await pilot.pause(0.05)
        await pilot.press("x")
        await settle(app, pilot)
        assert shells == [G4["endpoint"]]
        assert any("train is no longer this runtime" in str(n.message) for n in app._notifications)


async def test_a_runtime_takes_no_other_action_while_one_is_running(isolated):
    """s y s y must not send two stops of one VM; R and x wait too. The keys work again once it returns."""
    import threading

    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    gate = threading.Event()

    def stop(runtime):
        colab.calls.append(("stop", runtime.endpoint))
        if len(colab.calls) == 1:
            gate.wait(5)  # `colab stop` takes seconds, and the VM stays listed until it returns
            raise RuntimeError("colab stop failed")
        return "stopped"

    def ssh_command(runtime):
        colab.calls.append(("ssh", runtime.endpoint))
        raise RuntimeError("stubbed: no shell in tests")

    colab.stop, colab.ssh_command = stop, ssh_command
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("s", "y")
        await pilot.pause(0.1)
        await pilot.press("s", "y", "R", "y", "x")
        await pilot.pause(0.1)
        assert colab.calls == [("stop", G4["endpoint"])]
        gate.set()
        await settle(app, pilot)
        await pilot.press("s", "y")  # the first stop failed, so stopping again is the user's call
        await settle(app, pilot)
        assert colab.calls == [("stop", G4["endpoint"]), ("stop", G4["endpoint"])]


async def test_a_vanished_selection_stays_on_its_row_and_x_follows_it(isolated):
    """The cursor must not jump to row 0, the most expensive VM, where x opens a shell with no prompt."""
    a100 = assignment("gpu-a100-x", "A100")
    everyone = [G4, T4, CPU, a100]
    colab = StubColab(everyone, {a["endpoint"]: status() for a in everyone}, {}, rate=0)
    colab.local = {
        name: {"name": name, "endpoint": a["endpoint"], "running": None}
        for name, a in {"train": G4, "eval": T4, "cpu": CPU, "big": a100}.items()
    }

    def ssh_command(runtime):
        colab.calls.append(("ssh", runtime.endpoint))
        raise RuntimeError("stubbed: no shell in tests")

    colab.ssh_command = ssh_command
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        table = app.query_one(Runtimes)
        rows = lambda: [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]
        await pilot.press("down", "down")
        await settle(app, pilot)
        gone = rows()[2]
        colab._assignments = [a for a in everyone if a["endpoint"] != gone]
        await pilot.press("r")
        await settle(app, pilot)
        after = rows()
        assert table.cursor_row == 2 and after[2] != after[0]
        assert app.query_one(RuntimePanel).border_title.startswith(app.selected_runtime().label + " ")
        await pilot.press("x")
        await settle(app, pilot)
        assert colab.calls == [("ssh", after[2])]


# Wire data Colab could send, through the real Colab class: only the HTTP edge is faked.

WIRE = {
    "ccu": {"currentBalance": 5227.43, "consumptionRateHourly": 10.05, "assignmentsCount": 1, "eligibleGpus": ["H100"], "eligibleTpus": ["V6E1"]},
    "assignments": {"assignments": [G4]},
    "/api/status": {"started": "2026-11-17T20:59:46.480319Z", "last_activity": "2026-11-17T21:59:00Z", "kernels": 1, "connections": 1},
    "/api/kernels": [{"id": "k", "name": "python3", "last_activity": "2026-11-17T21:59:00Z", "execution_state": "busy", "connections": 1}],
    "/api/colab/resources": (FIXTURES / "resources_g4.txt").read_text(),
    "sessions": {"train": {"name": "train", "token": "t", "url": G4["runtimeProxyInfo"]["url"], "endpoint": G4["endpoint"], "running": "run(train.py)"}},
    "history": SECRET_HISTORY,
}


def wire(body: dict, key: str, *path, value):
    """body with the value at key/path replaced: a dict, or a raw string Colab could send."""
    out = copy.deepcopy(body)
    if not path:
        out[key] = value
        return out
    node = out[key] = parse_json(out[key]) if isinstance(out[key], str) else out[key]
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return out


class WireColab(Colab):
    """The real Colab, over fake Google and runtime proxy replies."""

    def __init__(self, body, monkeypatch, tmp_path):
        cli = tmp_path / "colab-cli"
        (cli / "history").mkdir(parents=True)
        (cli / "sessions.json").write_text(json.dumps(body["sessions"]))
        (cli / "history" / "train.jsonl").write_text(body["history"])
        monkeypatch.setattr(cotop.colab, "CLI_HOME", cli)
        monkeypatch.setattr(cotop.colab, "SESSIONS", cli / "sessions.json")
        monkeypatch.setattr(cotop.colab, "_cli", lambda *a: pytest.fail(f"ran colab {a}"))
        monkeypatch.setattr(cotop.colab, "colab_argv", lambda *a: pytest.fail(f"built colab {a}"))
        monkeypatch.setattr(cotop.colab.requests, "get", self.proxy_get)
        super().__init__(self)
        self.body = body
        self.unassigned = []

    def unassign(self, endpoint):  # Google, for stopping a runtime the CLI has no name for
        self.unassigned.append(endpoint)

    @staticmethod
    def reply(value) -> str:
        return value if isinstance(value, str) else ")]}'\n" + json.dumps(value)

    def get(self, url, params, headers, timeout):  # Google, for account calls
        key = "ccu" if "ccu-info" in url else "assignments" if "assignments" in url else None
        return self.reply(self.body[key]) if key else self.reply({"email": "me@example.com"})

    def proxy_get(self, url, headers, params, timeout, allow_redirects):
        path = next(p for p in ("/api/status", "/api/kernels", "/api/colab/resources") if url.endswith(p))
        return SimpleNamespace(is_redirect=False, raise_for_status=lambda: None, text=self.reply(self.body[path]))


NO_PROXY = {k: v for k, v in assignment("gpu-t4-provisioning", "T4").items() if k != "runtimeProxyInfo"}
NAN = WIRE["/api/colab/resources"].replace('"gpuUtilization": 1.0', '"gpuUtilization": NaN')


@pytest.mark.parametrize(
    "body,keys,shows",
    [
        pytest.param(WIRE, (), "ok", id="baseline"),
        pytest.param(wire(WIRE, "/api/colab/resources", value=NAN), (), "gap", id="res_util_nan"),
        pytest.param(wire(WIRE, "/api/colab/resources", "gpus", 0, "gpuUtilization", value="0.5"), (), "gap", id="res_util_string"),
        pytest.param(wire(WIRE, "/api/colab/resources", "gpus", 0, "gpuUtilization", value=85), (), "gap", id="res_util_percent"),
        pytest.param(wire(WIRE, "/api/colab/resources", "memory", value={"totalBytes": "190000000000", "freeBytes": "180000000000"}), (), "gap", id="res_mem_int64_string"),
        pytest.param(wire(WIRE, "/api/colab/resources", "disks", 0, "filesystem", "totalBytes", value="250000000000"), (), "gap", id="res_disk_int64_string"),
        pytest.param(wire(WIRE, "/api/colab/resources", "gpus", 0, "name", value=None), (), "gap", id="res_gpu_name_null"),
        pytest.param(wire(WIRE, "/api/colab/resources", value=")]}'\n<html>502</html>"), (), "gap", id="res_not_json"),
        pytest.param(wire(WIRE, "ccu", "consumptionRateHourly", value=None), (), "toast", id="ccu_rate_null"),
        pytest.param(wire(WIRE, "ccu", "currentBalance", value="5227.43"), (), "toast", id="ccu_balance_string"),
        pytest.param(wire(WIRE, "ccu", "eligibleGpus", value=None), (), "toast", id="ccu_eligible_null"),
        pytest.param(wire(WIRE, "ccu", "eligibleGpus", value=[{"name": "H100"}]), ("h",), "toast", id="ccu_eligible_objects"),
        pytest.param(wire(WIRE, "ccu", value="<html>Sign in</html>"), (), "toast", id="ccu_html_200"),
        pytest.param(wire(WIRE, "assignments", "assignments", value=[G4, NO_PROXY]), (), "row", id="asg_no_proxyinfo_on_one"),
        pytest.param(wire(WIRE, "assignments", "assignments", 0, "accelerator", value=None), (), "toast", id="asg_accel_null"),
        pytest.param(wire(WIRE, "assignments", value={"error": {"code": 500}}), (), "none", id="asg_error_body_200"),
        pytest.param(wire(WIRE, "/api/status", "started", value="2026-11-17T20:59:46"), (), "row", id="status_naive_started"),
        pytest.param(wire(WIRE, "/api/kernels", 0, "connections", value=None), (), "row", id="kernels_connections_null"),
        pytest.param(wire(WIRE, "/api/kernels", value={"message": "Forbidden"}), (), "row", id="kernels_error_dict"),
        pytest.param(wire(WIRE, "history", value=WIRE["history"].replace('"remote": "/content/out/model.pt"', '"remote": ["a", "b"]')), (), "ok", id="hist_remote_list"),
        pytest.param(wire(WIRE, "history", value=WIRE["history"].replace('"reason": "user_requested"', '"reason": ["x"]')), ("h",), "ok", id="hist_reason_list"),
        pytest.param(wire(WIRE, "history", value=WIRE["history"].replace(f'"endpoint": "{G4["endpoint"]}"', '"endpoint": ["x"]')), (), "ok", id="hist_endpoint_list"),
        pytest.param(wire(WIRE, "history", value="\xff\xfe garbage\n" + WIRE["history"]), (), "ok", id="hist_non_utf8"),
        pytest.param(wire(WIRE, "sessions", value='{"train": {"name": "train"'), (), "ok", id="sessions_corrupt"),
        pytest.param(wire(WIRE, "sessions", "other", value={"name": "other", "endpoint": "gpu-x"}), (), "ok", id="sessions_one_bad_entry"),
        pytest.param(wire(WIRE, "sessions", value="{"), ("R",), "ok", id="sessions_corrupt_then_R"),
        pytest.param(wire(WIRE, "sessions", value="{"), ("s", "y"), "ok", id="sessions_corrupt_then_s"),
    ],
)
async def test_wrong_wire_data_becomes_a_gap_a_row_error_or_a_toast(body, keys, shows, isolated, monkeypatch, tmp_path):
    """A value of the wrong type is turned away where it is parsed, into a path that already exists."""
    colab = WireColab(body, monkeypatch, tmp_path)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        app.action_sample()
        await settle(app, pilot)
        for key in keys:
            await pilot.press(key)
            await settle(app, pilot)
        rows = [[str(c) for c in app.query_one(Runtimes).get_row_at(i)] for i in range(app.query_one(Runtimes).row_count)]
        failed = [str(n.message) for n in app._notifications if str(n.message).startswith("Refresh failed")]
        assert app.is_running
    assert colab.unassigned == ([G4["endpoint"]] if "s" in keys else [])
    assert {
        "ok": failed == [] and len(rows) == 1 and rows[0][2].endswith("100%") and rows[0][5] != "unreachable",
        "gap": failed == [] and len(rows) == 1 and rows[0][2].endswith("–"),
        "row": failed == [] and bool(rows) and rows[-1][5] == "unreachable",
        "toast": len(failed) == 1 and rows == [] and not (isolated / "usage.jsonl").exists(),
        "none": failed == [] and rows == [],
    }[shows], (rows, failed)


class OneBadReading(WireColab):
    """Two runtimes; once bad is set, the one not selected reports a null freeBytes."""

    bad = False

    def proxy_get(self, url, **kwargs):
        reply = super().proxy_get(url, **kwargs)
        if self.bad and "other" in url and url.endswith("/resources"):
            reply.text = self.reply(wire(WIRE, "/api/colab/resources", "memory", "freeBytes", value=None)["/api/colab/resources"])
        return reply


async def test_a_bad_reading_on_another_runtime_survives_a_resize(isolated, monkeypatch, tmp_path):
    """A null freeBytes on a runtime that isn't selected used to be kept, then fail the next redraw."""
    other = assignment("gpu-g4-s-other", "G4")
    colab = OneBadReading(wire(WIRE, "assignments", "assignments", value=[G4, other]), monkeypatch, tmp_path)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        colab.bad = True
        app.action_sample()
        await settle(app, pilot)
        await pilot.resize_terminal(120, 46)
        await settle(app, pilot)
        assert app.is_running
        assert app.samples.latest(G4["endpoint"]) is not None and app.samples.latest(other["endpoint"]) is None


async def test_an_unwritable_cache_is_reported_once_and_the_dashboard_keeps_drawing(tmp_path):
    (tmp_path / "cache").write_text("a file where cotop's cache directory should be")
    colab = mixed()
    app = Dashboard(colab, tmp_path / "cache")
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        colab._rate = 12.0  # a new rate: the usage log writes again
        app.action_refresh()
        await settle(app, pilot)
        assert app.query_one(Runtimes).row_count == 3
        assert "compute units left" in str(app.query_one("#usage-text").render())
        assert [n.severity for n in app._notifications if "Can't save" in str(n.message)] == ["warning"]


def test_a_large_balance_on_one_cpu_runtime_still_draws():
    """The runway bound only keeps the end date inside a datetime; a real account never reaches it."""
    a = Account.parse("me@example.com", {"currentBalance": 500_000, "consumptionRateHourly": 0.07, "assignmentsCount": 1})
    assert "until" in account_text(a, NOW, None).plain


# Fuzz: any value Colab could put at any place in a reply either fails its parse or draws.

ODD = st.one_of(
    st.sampled_from([0, 1, 0.5, 85, -1, True, False, None, "", "5227.43", [], {}, ["H100"], [{"name": "H100"}], 10**400, 2**63, 2**63 - 1]),
    st.sampled_from([1e308, -1e308, 5e-324, float("inf"), float("nan"), "2026-11-17T20:59:46", "9999-12-31T23:59:59+00:00", "0001-01-01T00:00:00Z"]),
    st.integers(),
    st.floats(),
    st.text(max_size=8),
)
GONE = object()  # the key is left out


def places(node, at=()):
    yield at
    children = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for key, child in children:
        yield from places(child, (*at, key))


def mutated(data, body):
    """body with up to three of its values, at any depth, replaced by odd ones or left out."""
    body = copy.deepcopy(body)
    for at in data.draw(st.lists(st.sampled_from(list(places(body))[1:]), max_size=3)):
        value = data.draw(st.one_of(ODD, st.just(GONE)))
        try:
            parent = body
            for key in at[:-1]:
                parent = parent[key]
            if value is GONE:
                del parent[at[-1]]
            else:
                parent[at[-1]] = value
        except (KeyError, IndexError, TypeError):  # an earlier change removed this place
            pass
    return body


def parsed(parse):
    try:
        return parse()
    except Exception:  # turned away: a gap, a row error or a toast, as the replay cases show
        return None


def is_whole(n) -> bool:
    return type(n) is int and 0 <= n < 2**63


def is_fraction(u) -> bool:
    return type(u) in (int, float) and 0 <= u <= 1


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_every_value_that_parses_draws(data):
    email = data.draw(st.one_of(st.just("me@example.com"), ODD))
    account = parsed(lambda: Account.parse(email, mutated(data, WIRE["ccu"])))
    reading = parsed(lambda: Resources.parse(NOW, mutated(data, parse_json(WIRE["/api/colab/resources"]))))
    health = parsed(lambda: Status.parse(mutated(data, WIRE["/api/status"]), mutated(data, WIRE["/api/kernels"])))
    runtimes = parsed(lambda: runtimes_from([mutated(data, G4)], LOCAL, {G4["endpoint"]: health or ValueError("bad status")}))

    if account:
        assert type(account.email) is str and all(type(n) is str for n in account.eligible_gpus + account.eligible_tpus)
        assert all(type(n) in (int, float) and math.isfinite(n) for n in (account.balance, account.rate_hourly))
        assert is_whole(account.assignments_count)
        account_text(account, NOW, None)
        list(HistoryScreen(Snapshot(NOW, account, runtimes or [], History({})), RateBook(Path("/nonexistent/rates.json"))).compose())
    if reading:
        assert all(is_whole(n) for n in (reading.memory_total, reading.memory_free, reading.disk_total, reading.disk_used))
        assert all(type(g.name) is str and (g.utilization is None or is_fraction(g.utilization)) for g in reading.gpus)
        assert all(is_whole(g.memory_used) and is_whole(g.memory_total) for g in reading.gpus)
        meters(reading, 14)
        sparkline([reading.gpu_utilization], 8)
    if health:
        assert health.started.tzinfo and all(k.last_activity.tzinfo for k in health.kernels)
        assert all(type(k.state) is str and is_whole(k.connections) for k in health.kernels)
    for r in runtimes or []:
        assert all(type(v) is str for v in (r.endpoint, r.accelerator, r.shape, r.token)) and (r.url is None or type(r.url) is str)
        r.concerns(NOW, reading)
        state_text(r, NOW, reading)


QUIT_WHILE_A_PROXY_STALLS = """
import pathlib, tempfile, time
import cotop.colab
from cotop.app import Dashboard
from tests.test_app import StubColab
from tests.test_model import G4, status

colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
colab.status = cotop.colab.Colab(None).status
app = Dashboard(colab, pathlib.Path(tempfile.mkdtemp()))

def stalled(assignment):
    app.call_from_thread(app.exit)  # q, pressed while this read waits
    time.sleep(60)

cotop.colab._runtime_status = stalled
app.run(headless=True)
"""


def test_quitting_does_not_wait_for_a_read_in_flight():
    """A runtime proxy stalled mid-poll must not hold the terminal after q. Python joins pool threads and
    non-daemon threads before the process exits, so this runs as its own process."""
    import subprocess
    import sys
    from pathlib import Path

    subprocess.run([sys.executable, "-c", QUIT_WHILE_A_PROXY_STALLS], cwd=Path(__file__).parents[1], timeout=15, check=True)


async def test_a_stuck_refresh_is_reported_and_the_header_turns_stale(isolated, monkeypatch):
    """A poll that never returns (the CLI's lock read waits for weeks) must not leave old figures looking current."""
    import threading

    import cotop.app

    monkeypatch.setattr(cotop.app, "POLL_DEADLINE", 0.5)
    monkeypatch.setattr(cotop.app, "STALE", timedelta(seconds=1))
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    app = Dashboard(colab, isolated)
    stuck = threading.Event()
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        usage = app.query_one("Usage")
        assert "updated" in usage.border_title and "stale" not in usage.border_title
        colab.sessions = lambda: stuck.wait(10)
        await pilot.press("r")
        await pilot.pause(1.2)
        assert any("still reading after 0.5 s" in str(n.message) for n in app._notifications)
        usage.show_age()  # what its one-second timer does
        assert "stale, updated" in usage.border_title
        stuck.set()


async def test_keys_typed_while_x_checks_the_name_answer_nothing(isolated):
    """The check blocks the loop; `s y` typed then would stop the runtime before its prompt was drawn."""
    from textual import events

    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    app = Dashboard(colab, isolated)

    def ssh_command(runtime):
        for key in "sy":  # what the driver's input thread does with keys read meanwhile
            app._driver.send_message(events.Key(key, key))
        raise RuntimeError("train is no longer this runtime; refresh and retry")

    colab.ssh_command = ssh_command
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("x")
        await settle(app, pilot)
        assert colab.calls == [] and app.screen is app.screen_stack[0]
        await pilot.press("s")  # typed after: works
        assert "Stop train" in str(app.screen.query_one("Label").render())


async def test_a_read_that_returns_while_cotop_quits_touches_no_widgets(isolated):
    """Textual takes the screens down before it cancels workers; a read landing in between must not show anything."""
    import time

    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)

        def returns_as_cotop_quits():
            while app.is_running:
                time.sleep(0.001)
            return ccu(10.05, 1)

        colab.ccu_info = returns_as_cotop_quits
        app.action_refresh()
