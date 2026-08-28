"""LossGuard size-down behavior."""

from __future__ import annotations

from loss_guard import LossGuard


def test_size_down_after_losses_and_reset_on_win():
    lg = LossGuard(size_down_after_losses=2, size_down_scale=0.5)
    lg.record_close(-10.0)
    ok, _, scale = lg.allow_new_entry(equity=10_000, score=0.8, open_count=0)
    assert ok and scale == 1.0

    lg.record_close(-10.0)
    ok, reason, scale = lg.allow_new_entry(equity=10_000, score=0.8, open_count=0)
    assert ok
    assert scale == 0.5
    assert "size-down" in reason.lower() or lg.defensive

    lg.record_close(20.0)
    ok, _, scale = lg.allow_new_entry(equity=10_000, score=0.8, open_count=0)
    assert ok and scale == 1.0
    assert lg.consecutive_losses == 0
