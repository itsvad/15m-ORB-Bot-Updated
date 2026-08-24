from __future__ import annotations

from orb_bot.notify.formatting import format_alert, format_notify
from orb_bot.strategy.models import Alert, Notify


def test_format_entry_notify_includes_key_fields():
    n = Notify("entry", {"side": "long", "price": 5020.5, "qty": 4, "stop": 4999.5, "r_unit_points": 21.0})
    text = format_notify(n)
    assert "LONG" in text
    assert "5020.50" in text
    assert "4" in text


def test_format_unknown_event_falls_back_gracefully():
    n = Notify("something_new", {"a": 1})
    text = format_notify(n)
    assert "something_new" in text


def test_format_critical_alert():
    a = Alert("critical", "daily loss limit hit")
    text = format_alert(a)
    assert "CRITICAL" in text
    assert "daily loss limit hit" in text
