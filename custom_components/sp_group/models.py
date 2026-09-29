"""SP Group domain models shared by the client, mapper, and history layers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

from .const import fold_text

SG_TZ = timezone(timedelta(hours=8))


class Clock(Protocol):
    """Wall-clock source for token expiry and AMI window selection.

    Production passes ``SystemClock``; a test passes a clock the caller steps
    by hand, so a poll's requests and the periods derived from them depend on
    the time chosen for the run and not on when the run happened to start.
    """

    def now(self) -> datetime:
        """Current time in ``SG_TZ``, timezone-aware."""

    def timestamp(self) -> int:
        """Whole seconds since the epoch, for OAuth ``expires_at`` compare."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, for measuring intervals only.

        The sign-in cooldown is stored as such an interval, so a test can move
        it without a wall clock, a sleep, or a patched module global.
        """


@dataclass(frozen=True)
class SystemClock:
    """The real clock. Every time-dependent code path takes its ``Clock``."""

    def now(self) -> datetime:
        return datetime.now(SG_TZ)

    def timestamp(self) -> int:
        return int(datetime.now(UTC).timestamp())

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class PeriodReading:
    start: datetime
    amount: float
    previous: float | None = None
    status: str | None = None


@dataclass(frozen=True)
class UtilitySeries:
    total: float
    unit: str
    periods: tuple[PeriodReading, ...]
    average: float | None
    comparison: str | None


@dataclass(frozen=True)
class MfaChallenge:
    oob_code: str


@dataclass(frozen=True)
class PremiseInfo:
    id: str
    address: str | None
    account_number: str | None
    account_status: str | None
    account_type: str | None
    premise_type: str | None
    utilities: tuple[str, ...]
    ami_elec: bool | None
    retailer_name: str | None
    ppms_exists: bool


@dataclass(frozen=True)
class MeterReadingInfo:
    message: str | None
    title: str | None
    start: str | None
    end: str | None


@dataclass(frozen=True)
class BillInfo:
    amount_sgd: float
    date: str | None
    period: str | None
    due_date: str | None
    issued_at: datetime | None = None


@dataclass(frozen=True)
class PayableInfo:
    amount_sgd: float
    currency: str | None
    giro_enabled: bool | None
    recurring_enabled: bool | None


@dataclass(frozen=True)
class MeterRegister:
    utility: str
    meter_id: str | None
    value: float
    last_actual_at: str | None


@dataclass(frozen=True)
class GreenGoal:
    kind: str
    month: str | None
    used: float
    target: float
    percent_difference: float | None
    cost_difference_sgd: float | None
    unit: str


@dataclass(frozen=True)
class GreenUpInfo:
    points: float
    tier_name: str | None
    tier_level: float | None
    points_to_level_up: float | None


@dataclass(frozen=True)
class EvWalletInfo:
    points: float
    dollar_balance: float | None
    current_tier_id: float | None


@dataclass(frozen=True)
class EvSessionInfo:
    status: str | None
    kwh: float | None
    total_cost: str | None
    start: str | None
    order_id: str | None


@dataclass(frozen=True)
class EvChargeInfo:
    kwh: float | None
    amount: float | None
    created_at: str | None
    status: str | None
    address: str | None


@dataclass(frozen=True)
class EvUnpaidInfo:
    count: int
    amount: float | None


@dataclass(frozen=True)
class FcuInfo:
    thing_name: str
    display_name: str | None
    is_on: bool | None
    is_online: bool | None
    room_temperature: float | None
    setpoint: float | None
    mode: str | None


@dataclass(frozen=True)
class BillDeliveryInfo:
    soft_copy: bool | None
    hard_copy: bool | None


@dataclass(frozen=True)
class TariffInfo:
    kwh_price: float | None
    monthly_price: float | None
    consumption: str | None


@dataclass(frozen=True)
class OptionalReads:
    greenup: GreenUpInfo | None = None
    ev_wallet: EvWalletInfo | None = None
    ev_session: EvSessionInfo | None = None
    ev_last_charge: EvChargeInfo | None = None
    ev_unpaid: EvUnpaidInfo | None = None
    unread_notifications: int | None = None
    bill_delivery: BillDeliveryInfo | None = None
    fcus: tuple[FcuInfo, ...] = ()
    tariff: TariffInfo | None = None


@dataclass(frozen=True)
class UsageReadings:
    premise: PremiseInfo
    electricity: UtilitySeries | None
    water: UtilitySeries | None
    gas: UtilitySeries | None
    meter_reading: MeterReadingInfo | None = None
    ppms_credit: float | None = None
    ppms_updated_at: str | None = None
    ami_hourly: tuple[PeriodReading, ...] = ()
    ami_daily: tuple[PeriodReading, ...] = ()
    last_bill: BillInfo | None = None
    bills: tuple[BillInfo, ...] = ()
    amount_due: PayableInfo | None = None
    meter_registers: tuple[MeterRegister, ...] = ()
    green_goals: tuple[GreenGoal, ...] = ()
    greenup: GreenUpInfo | None = None
    ev_wallet: EvWalletInfo | None = None
    ev_session: EvSessionInfo | None = None
    ev_last_charge: EvChargeInfo | None = None
    ev_unpaid: EvUnpaidInfo | None = None
    unread_notifications: int | None = None
    bill_delivery: BillDeliveryInfo | None = None
    fcus: tuple[FcuInfo, ...] = ()
    tariff: TariffInfo | None = None

    @property
    def premise_id(self) -> str:
        return self.premise.id

    @property
    def electricity_kwh(self) -> float:
        return self.electricity.total if self.electricity else 0.0

    @property
    def water_m3(self) -> float:
        return self.water.total if self.water else 0.0

    @property
    def electricity_periods(self) -> tuple[PeriodReading, ...]:
        return self.electricity.periods if self.electricity else ()

    @property
    def water_periods(self) -> tuple[PeriodReading, ...]:
        return self.water.periods if self.water else ()

    @property
    def gas_periods(self) -> tuple[PeriodReading, ...]:
        return self.gas.periods if self.gas else ()

    def meter(self, utility: str) -> MeterRegister | None:
        wanted = fold_text(utility)
        for item in self.meter_registers:
            if fold_text(item.utility) == wanted:
                return item
        return None

    def goal(self, kind: str) -> GreenGoal | None:
        wanted = fold_text(kind)
        for item in self.green_goals:
            if fold_text(item.kind) == wanted:
                return item
        return None
