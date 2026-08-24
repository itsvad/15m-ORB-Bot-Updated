from __future__ import annotations

import shutil

import yaml

from orb_bot.app import build_app_context
from orb_bot.broker.dry_run_broker import DryRunBroker
from orb_bot.broker.tradovate_client import TradovateMarketData


def _prepare_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    with open("config/config.yaml") as f:
        raw = yaml.safe_load(f)
    raw["logging"]["log_dir"] = str(tmp_path / "logs")
    raw["logging"]["decision_log_file"] = str(tmp_path / "logs" / "decisions.jsonl")
    raw["logging"]["trade_log_file"] = str(tmp_path / "logs" / "trade_log.csv")
    with open(config_path, "w") as f:
        yaml.safe_dump(raw, f)
    return config_path


def test_dry_run_with_credentials_gets_a_standalone_live_market_data_source(tmp_path, monkeypatch):
    config_path = _prepare_config(tmp_path)
    monkeypatch.setenv("ORB_MODE", "dry_run")
    monkeypatch.setenv("ORB_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("TRADOVATE_USERNAME", "demo_user")
    monkeypatch.setenv("TRADOVATE_PASSWORD", "demo_pass")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    ctx = build_app_context(str(config_path))

    assert isinstance(ctx.broker, DryRunBroker)
    assert isinstance(ctx.market_data, TradovateMarketData)
    assert ctx._owns_market_data is True
    ctx.store.close()


def test_dry_run_without_credentials_has_no_live_market_data_source(tmp_path, monkeypatch):
    config_path = _prepare_config(tmp_path)
    monkeypatch.setenv("ORB_MODE", "dry_run")
    monkeypatch.setenv("ORB_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.delenv("TRADOVATE_USERNAME", raising=False)
    monkeypatch.delenv("TRADOVATE_PASSWORD", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    ctx = build_app_context(str(config_path))

    assert ctx.market_data is None
    ctx.store.close()
