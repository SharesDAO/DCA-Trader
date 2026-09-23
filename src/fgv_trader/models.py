from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class Candle:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class FGVSignal:
    symbol: str
    session_date: str
    range_high: float
    trigger_low: float
    stop_loss: float
    take_profit: float
    risk: float
    c1_time: datetime
    c2_time: datetime
    c3_time: datetime
    c2_range_pct: float = 0.0
    c2_body_pct: float = 0.0
    c2_relative_volume: float = 0.0
    c3_body_pct: float = 0.0
    c3_close_position: float = 0.0


class SymbolState(str, Enum):
    WAIT_FIRST_15M = "WAIT_FIRST_15M"
    SCAN_5M_SIGNAL = "SCAN_5M_SIGNAL"
    WAIT_PRICE_PULLBACK = "WAIT_PRICE_PULLBACK"
    BUY_ORDER_SENT = "BUY_ORDER_SENT"
    IN_POSITION = "IN_POSITION"
    DONE_FOR_DAY = "DONE_FOR_DAY"


@dataclass
class Position:
    symbol: str
    session_date: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    buy_order_id: str
    buy_tx_hash: Optional[str] = None
