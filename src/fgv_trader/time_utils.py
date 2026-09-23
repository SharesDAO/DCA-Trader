from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketSession:
    session_date: date
    market_open: datetime
    first_15m_complete: datetime
    scan_end: datetime
    force_exit: datetime
    market_close: datetime


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


class TimeService:
    def __init__(self, exchange_timezone: str, local_timezone: str, schedule: dict):
        self.exchange_tz = ZoneInfo(exchange_timezone)
        self.local_tz = ZoneInfo(local_timezone)
        self.schedule = schedule

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_exchange(self) -> datetime:
        return self.now_utc().astimezone(self.exchange_tz)

    def to_exchange(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.exchange_tz)

    def to_local(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self.local_tz)

    def session_for(self, session_date: date) -> MarketSession:
        def at(name: str) -> datetime:
            return datetime.combine(
                session_date,
                parse_hhmm(self.schedule[name]),
                tzinfo=self.exchange_tz,
            )

        return MarketSession(
            session_date=session_date,
            market_open=at("market_open"),
            first_15m_complete=at("first_15m_complete"),
            scan_end=at("scan_end"),
            force_exit=at("force_exit"),
            market_close=at("market_close"),
        )

    def current_session(self, now: datetime | None = None) -> MarketSession:
        exchange_now = self.to_exchange(now or self.now_utc())
        return self.session_for(exchange_now.date())

    def is_scan_window(self, now: datetime | None = None) -> bool:
        exchange_now = self.to_exchange(now or self.now_utc())
        session = self.session_for(exchange_now.date())
        return session.first_15m_complete <= exchange_now < session.scan_end

    def is_force_exit_window(self, now: datetime | None = None) -> bool:
        exchange_now = self.to_exchange(now or self.now_utc())
        session = self.session_for(exchange_now.date())
        return session.force_exit <= exchange_now < session.market_close
