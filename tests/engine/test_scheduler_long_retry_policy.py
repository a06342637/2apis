"""上游恢复时间不能缩短本地已升级的风控冷却，等值也不冒充采纳上游。"""

import pytest

from src.engine.scheduler import Candidate, Scheduler, clamp_retry_at
from src.provider.base import ErrKind

NOW = 1_000_000


@pytest.mark.parametrize("duration", [32 * 3600, 2 * 86400, 7 * 86400])
@pytest.mark.parametrize("hint", [None, 0, 20 * 86400])
def test_long_local_cooldown_is_never_shortened_by_global_hint_cap(duration, hint):
    retry_at = NOW + hint if hint is not None else None
    assert clamp_retry_at(retry_at, NOW, duration) == (NOW + duration, False)


def test_restricted_error_keeps_escalated_policy_and_does_not_log_false_adoption(caplog):
    candidate = Candidate(credential_id="account", provider="miora", err_count=4)
    with caplog.at_level("INFO", logger="src.engine.scheduler"):
        outcome = Scheduler().note_error(candidate, ErrKind.RESTRICTED, NOW,
                                         retry_at=NOW + 4 * 86400)
    assert outcome.cooling_until == NOW + 32 * 3600
    assert outcome.err_count == 5 and outcome.cooling_kind == "restricted"
    assert not outcome.disabled and "采用上游时刻" not in caplog.text


def test_equal_global_cap_does_not_change_local_policy_or_report_adoption():
    assert clamp_retry_at(NOW + 2 * 86400, NOW, 86400) == (NOW + 86400, False)


def test_short_local_policy_still_adopts_bounded_upstream_hint():
    assert clamp_retry_at(NOW + 4 * 86400, NOW, 12 * 3600) == (NOW + 86400, True)
