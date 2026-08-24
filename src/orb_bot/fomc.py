"""FOMC decision-day loader (optional/toggleable feature).

Dates come from a maintained YAML file (data/fomc_dates.yaml), never from
scraping or inference, per the project spec.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml


def load_fomc_dates(path: str | Path) -> set[dt.date]:
    with open(path, "r", encoding="utf-8") as f:
        raw: dict[int, list[str]] = yaml.safe_load(f) or {}
    dates: set[dt.date] = set()
    for _year, entries in raw.items():
        for entry in entries:
            dates.add(dt.date.fromisoformat(entry))
    return dates


def is_fomc_day(date: dt.date, fomc_dates: set[dt.date]) -> bool:
    return date in fomc_dates
