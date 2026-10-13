"""Golden set level 3: very large wheels with VC++ runtime needs (torch, CPU)."""

from ._smoke import run_checks


def check_torch():
    import torch

    a = torch.arange(6, dtype=torch.float32).reshape(2, 3)
    assert (a @ a.T).sum().item() == 83.0  # [[5, 14], [14, 50]]
    assert torch.nn.functional.relu(torch.tensor([-1.0, 2.0])).tolist() == [0.0, 2.0]


def run():
    return run_checks([check_torch])
