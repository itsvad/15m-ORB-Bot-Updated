"""Typed, validated configuration for the ORB bot.

Every strategy parameter mentioned in the spec is a config field - nothing
is hardcoded. Config is loaded from a YAML file (config/config.yaml by
default) and secrets/environment-specific values come from environment
variables (see .env.example).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Known Tradovate contract point values, used to sanity-check the configured
# point_value at startup (see safety.risk requirement: never trade on a
# misconfigured $/point). Extend as needed.
KNOWN_POINT_VALUES: dict[str, float] = {
    "ES": 50.0,
    "MES": 5.0,
}


def _parse_hhmm(value: str) -> dt.time:
    h, m = value.split(":")
    return dt.time(hour=int(h), minute=int(m))


class InstrumentConfig(BaseModel):
    symbol: str
    point_value: float = Field(gt=0)
    tick_size: float = Field(gt=0)

    @model_validator(mode="after")
    def _validate_point_value(self) -> "InstrumentConfig":
        root = "".join(ch for ch in self.symbol if ch.isalpha()).upper()
        known = KNOWN_POINT_VALUES.get(root)
        if known is not None and abs(known - self.point_value) > 1e-9:
            raise ValueError(
                f"Configured point_value={self.point_value} for symbol "
                f"'{self.symbol}' does not match the known contract spec "
                f"({known}/pt for {root}). Refusing to start: a wrong "
                f"$/point silently produces wildly wrong position sizes."
            )
        return self


class SessionConfig(BaseModel):
    opening_range_start: dt.time
    opening_range_minutes: int = Field(gt=0)
    entry_cutoff: dt.time
    hard_close: dt.time
    timezone: Literal["America/New_York"] = "America/New_York"

    @field_validator("opening_range_start", "entry_cutoff", "hard_close", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str):
            return _parse_hhmm(v)
        return v

    @model_validator(mode="after")
    def _validate_ordering(self) -> "SessionConfig":
        or_end = (
            dt.datetime.combine(dt.date.today(), self.opening_range_start)
            + dt.timedelta(minutes=self.opening_range_minutes)
        ).time()
        if or_end > self.entry_cutoff:
            raise ValueError("opening range end must be <= entry_cutoff")
        if self.entry_cutoff > self.hard_close:
            raise ValueError("entry_cutoff must be <= hard_close")
        return self


class OpeningRangeFilterConfig(BaseModel):
    min_width_points: float = Field(gt=0)
    max_width_points: float = Field(gt=0)

    @model_validator(mode="after")
    def _validate_range(self) -> "OpeningRangeFilterConfig":
        if self.min_width_points >= self.max_width_points:
            raise ValueError("min_width_points must be < max_width_points")
        return self


class EntryConfig(BaseModel):
    buffer_points: float = Field(ge=0)


class RiskConfig(BaseModel):
    risk_pct_of_equity: float = Field(gt=0, le=0.1)
    min_contracts: int = Field(ge=1)
    max_contracts_sanity_cap: int = Field(ge=1)


class OldRunnerConfig(BaseModel):
    sma_period: int = Field(gt=1)
    sma_timeframe_minutes: int = Field(gt=0)
    cap_r_multiple: float = Field(gt=0)


class NewRunnerConfig(BaseModel):
    sma_period: int = Field(gt=1)
    sma_timeframe_minutes: int = Field(gt=0)
    candle_lookback: int = Field(ge=1)


class RunnerConfig(BaseModel):
    mode: Literal["old", "new"]
    old: OldRunnerConfig
    new: NewRunnerConfig


class WhipsawGuardConfig(BaseModel):
    time: dt.time

    @field_validator("time", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str):
            return _parse_hhmm(v)
        return v


class ExitsConfig(BaseModel):
    breakeven_r_multiple: float = Field(gt=0)
    tp1_r_multiple: float = Field(gt=0)
    tp1_close_pct: float = Field(gt=0, le=1.0)
    runner: RunnerConfig
    whipsaw_guard: WhipsawGuardConfig

    @model_validator(mode="after")
    def _validate_ordering(self) -> "ExitsConfig":
        if self.tp1_r_multiple <= self.breakeven_r_multiple:
            raise ValueError("tp1_r_multiple must be > breakeven_r_multiple")
        return self


class FomcConfig(BaseModel):
    enabled: bool = False
    flatten_before: dt.time
    dates_file: str

    @field_validator("flatten_before", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str):
            return _parse_hhmm(v)
        return v


class SafetyConfig(BaseModel):
    daily_loss_limit_usd: float = Field(gt=0)
    daily_loss_limit_pct_of_equity: float | None = Field(default=None, gt=0, le=1.0)
    max_orders_per_day: int = Field(ge=1, le=1)  # spec: one trade per day, hard-coded


class LoggingConfig(BaseModel):
    log_dir: str
    decision_log_file: str
    trade_log_file: str
    level: str = "INFO"


class AppConfig(BaseModel):
    instrument: InstrumentConfig
    session: SessionConfig
    opening_range_filter: OpeningRangeFilterConfig
    entry: EntryConfig
    risk: RiskConfig
    exits: ExitsConfig
    fomc: FomcConfig
    safety: SafetyConfig
    logging: LoggingConfig


class RuntimeSecrets(BaseModel):
    """Environment-sourced values that must never live in the YAML config."""

    mode: Literal["dry_run", "live"] = "dry_run"
    tradovate_env: Literal["demo", "live"] = "demo"
    tradovate_username: str = ""
    tradovate_password: str = ""
    tradovate_cid: str = ""
    tradovate_secret: str = ""
    tradovate_app_id: str = "orb-bot"
    tradovate_app_version: str = "0.1.0"
    tradovate_device_id: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    state_db_path: str = "state.db"

    @model_validator(mode="after")
    def _validate_live_safety(self) -> "RuntimeSecrets":
        if self.mode == "live" and self.tradovate_env != "live":
            raise ValueError(
                "ORB_MODE=live requires TRADOVATE_ENV=live; refusing to run "
                "live order placement against a demo account credential set "
                "mismatch."
            )
        return self

    @classmethod
    def from_env(cls) -> "RuntimeSecrets":
        chat_ids_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        chat_ids = [int(x) for x in chat_ids_raw.split(",") if x.strip()]
        return cls(
            mode=os.environ.get("ORB_MODE", "dry_run"),  # type: ignore[arg-type]
            tradovate_env=os.environ.get("TRADOVATE_ENV", "demo"),  # type: ignore[arg-type]
            tradovate_username=os.environ.get("TRADOVATE_USERNAME", ""),
            tradovate_password=os.environ.get("TRADOVATE_PASSWORD", ""),
            tradovate_cid=os.environ.get("TRADOVATE_CID", ""),
            tradovate_secret=os.environ.get("TRADOVATE_SECRET", ""),
            tradovate_app_id=os.environ.get("TRADOVATE_APP_ID", "orb-bot"),
            tradovate_app_version=os.environ.get("TRADOVATE_APP_VERSION", "0.1.0"),
            tradovate_device_id=os.environ.get("TRADOVATE_DEVICE_ID", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_chat_ids=chat_ids,
            state_db_path=os.environ.get("ORB_STATE_DB_PATH", "state.db"),
        )


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
