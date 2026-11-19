"""The dashboard rendered from fixture payloads, with no network and temp state dirs."""

import json
from datetime import timedelta

import pytest

from cotop.app import Dashboard, RuntimePanel, Runtimes
from rich.color import Color as RichColor
from rich.style import Style

from cotop.widgets import RED
from cotop.model import History, PastSession, utc_now
from tests.test_model import CPU, G4, LOCAL, SECRET_HISTORY, T4, assignment, ccu, resources, status


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
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    history = cli_history(tmp_path / "history", train=[execution])
    colab.history = lambda: history
    app = Dashboard(colab, isolated)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        activity = str(app.query_one(RuntimePanel).query_one(".activity").render())
        assert "exec cell 3" in activity and "SECRET" not in activity


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
