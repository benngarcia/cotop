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
