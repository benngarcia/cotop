"""The dashboard rendered from fixture payloads, with no network and temp state dirs."""

import pytest

import cotop.app as app_module
from cotop.app import Dashboard, RuntimePanel, Runtimes
from cotop.model import utc_now
from tests.test_model import CPU, G4, LOCAL, T4, ccu, resources, status


class StubColab:
    def __init__(self, assignments, status, readings, rate):
        self._assignments, self._status, self._readings, self._rate = assignments, status, readings, rate
        self.fail = False
        self.calls = []

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
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(app_module, "CLI_HOME", tmp_path / "cli")
    monkeypatch.setattr(app_module, "local_sessions", lambda: LOCAL)
    (tmp_path / "cli" / "history").mkdir(parents=True)
    return tmp_path


async def settle(app, pilot):
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


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
    app = Dashboard(mixed())
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        table = app.query_one(Runtimes)
        assert table.row_count == 3
        states = [str(table.get_row_at(i)[6]) for i in range(3)]
        assert states[0].startswith("idle") and states[1] == "busy" and states[2] == "unreachable"
        assert [str(table.get_row_at(i)[3]) for i in range(3)] == ["?", "?", "?"]  # mixed: no rate known
        assert not (isolated / "cache" / "rates.json").exists()
        panel = app.query_one(RuntimePanel)
        assert panel.border_title == "train  G4"
        await pilot.press("down")
        await settle(app, pilot)
        assert panel.border_title == "untracked  T4"


async def test_failed_refresh_keeps_the_last_data(isolated):
    colab = mixed()
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        colab.fail = True
        await pilot.press("r")
        await settle(app, pilot)
        assert any("Refresh failed: ConnectionError: ccu-info timed out." in str(n.message) for n in app._notifications)
        assert app.query_one(Runtimes).row_count == 3


async def test_no_runtimes(isolated):
    app = Dashboard(StubColab([], {}, {}, rate=0))
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        assert "nothing running" in str(app.query_one("#usage-text").render())
        assert "No runtimes" in str(app.query_one("#empty").render())


async def test_stop_needs_a_yes_and_acts_on_the_selected_runtime(isolated):
    colab = StubColab([G4, T4], {G4["endpoint"]: status(), T4["endpoint"]: status()}, {}, rate=11.89)
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("s")
        assert "Stop train (G4)? Its VM and files will be deleted. It is busy running run(train.py)." in str(
            app.screen.query_one("Label").render()
        )
        await pilot.press("enter")  # Cancel has focus
        await pilot.press("s", "n")
        await pilot.press("down", "s", "y")
        await settle(app, pilot)
        assert colab.calls == [("stop", T4["endpoint"])]


async def test_kernel_and_ssh_actions_refuse_untracked_runtimes(isolated):
    colab = StubColab([T4], {T4["endpoint"]: status()}, {}, rate=1.84)
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("k", "x")
        await pilot.pause()
        assert colab.calls == [] and len(app._notifications) == 2
        assert app.screen is app.screen_stack[0]


async def test_history_screen_opens_and_closes(isolated):
    app = Dashboard(mixed())
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("h")
        await pilot.pause()
        assert type(app.screen).__name__ == "HistoryScreen"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


async def test_selection_survives_the_runtime_list_changing(isolated):
    """A selection below row 0 must stay put, not flip between runtimes, when the list changes."""
    colab = mixed()
    app = Dashboard(colab)
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
    app = Dashboard(mixed())
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
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await empty_then_two(app, pilot, colab, empty_at_launch)
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is app.query_one(Runtimes)
        assert app.selected_runtime().endpoint == T4["endpoint"]
        assert app.query_one(RuntimePanel).border_title == "untracked  T4"


async def test_ssh_after_the_empty_state_targets_the_highlighted_row(isolated, monkeypatch):
    """x is the one key with no confirmation, so it gets its own test."""
    named = {**LOCAL, "eval": {"name": "eval", "endpoint": T4["endpoint"], "running": None}}
    monkeypatch.setattr(app_module, "local_sessions", lambda: named)
    colab = StubColab([], {}, {}, rate=0)

    def ssh_command(runtime):
        colab.calls.append(("ssh", runtime.endpoint))
        raise RuntimeError("stubbed: no shell in tests")

    colab.ssh_command = ssh_command
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await empty_then_two(app, pilot, colab, empty_at_launch=True)
        await pilot.press("down", "x")
        await settle(app, pilot)
        assert colab.calls == [("ssh", T4["endpoint"])]


async def test_the_panel_follows_a_poll_without_a_cursor_move(isolated):
    colab = StubColab([G4], {G4["endpoint"]: status()}, {}, rate=10.05)
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        assert app.query_one(RuntimePanel).border_subtitle.startswith("busy")
        colab._status = {G4["endpoint"]: status("idle", "2026-11-17T21:59:00Z")}
        await pilot.press("r")
        await settle(app, pilot)
        assert app.query_one(RuntimePanel).border_subtitle.startswith("idle")


async def test_runtime_keys_do_nothing_on_the_history_screen(isolated, monkeypatch):
    named = {**LOCAL, "eval": {"name": "eval", "endpoint": T4["endpoint"], "running": None}}
    monkeypatch.setattr(app_module, "local_sessions", lambda: named)
    colab = mixed()

    def ssh_command(runtime):
        colab.calls.append(("ssh", runtime.endpoint))
        raise RuntimeError("stubbed: no shell in tests")

    colab.ssh_command = ssh_command
    app = Dashboard(colab)
    async with app.run_test(size=(150, 46)) as pilot:
        await settle(app, pilot)
        await pilot.press("h", "x", "s", "k")
        await settle(app, pilot)
        assert type(app.screen).__name__ == "HistoryScreen"
        assert colab.calls == []
