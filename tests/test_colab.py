from cotop.colab import still_assigned
from cotop.model import runtimes_from
from tests.test_model import G4, LOCAL


def test_a_name_counts_only_while_it_still_means_the_same_vm():
    [r] = runtimes_from([G4], LOCAL, {})
    assert still_assigned(r, [G4], LOCAL)
    assert not still_assigned(r, [], LOCAL)  # VM gone: `colab ssh -s` would create a new one
    reused = {"train": {"name": "train", "endpoint": "gpu-g4-new", "running": None}}
    assert not still_assigned(r, [G4], reused)  # name now means another VM
    [untracked] = runtimes_from([G4], {}, {})
    assert not still_assigned(untracked, [G4], {})


def test_no_cli_sessions_before_the_cli_has_run(tmp_path, monkeypatch):
    """A first-time user signed in through cotop has no ~/.config/colab-cli yet."""
    import cotop.colab

    monkeypatch.setattr(cotop.colab, "SESSIONS", tmp_path / "colab-cli" / "sessions.json")
    assert cotop.colab.Colab(None).sessions() == {}
    assert not (tmp_path / "colab-cli").exists()  # cotop creates nothing in the CLI's directory
