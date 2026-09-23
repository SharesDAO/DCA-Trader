from datetime import datetime, timezone

from fgv_trader.time_utils import TimeService


def test_uses_named_timezones_across_dst():
    service = TimeService(
        "America/New_York",
        "America/Los_Angeles",
        {
            "market_open": "09:30",
            "first_15m_complete": "09:45",
            "scan_end": "12:30",
            "force_exit": "15:45",
            "market_close": "16:00",
        },
    )

    winter = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)

    assert service.to_exchange(winter).hour == 9
    assert service.to_local(winter).hour == 6
    assert service.to_exchange(summer).hour == 9
    assert service.to_local(summer).hour == 6
