"""Pure message-formatting helpers, kept separate from the Telegram client
itself so they're testable without a network connection or a bot token."""
from __future__ import annotations

from orb_bot.strategy.models import Alert, Notify


def format_notify(n: Notify) -> str:
    p = n.payload
    if n.event == "entry_orders_placed":
        return (
            f"\U0001F4CB Entry orders placed\n"
            f"OR: {p['or_low']:.2f}-{p['or_high']:.2f} (width {p['or_width']:.2f})\n"
            f"Buy-stop {p['buy_stop']:.2f} / Sell-stop {p['sell_stop']:.2f}\n"
            f"Qty {p['qty']} (risking ${p['risk_dollars']:.2f})"
        )
    if n.event == "entry":
        return (
            f"✅ ENTRY {p['side'].upper()} x{p['qty']} @ {p['price']:.2f}\n"
            f"Stop {p['stop']:.2f} (1R = {p['r_unit_points']:.2f}pts)"
        )
    if n.event == "tp1":
        return (
            f"\U0001F3AF TP1 hit @ {p['price']:.2f} ({p['r_multiple']:.2f}R)\n"
            f"Closed {p['closed_qty']}, runner {p['runner_qty']}"
        )
    if n.event == "exit":
        return (
            f"\U0001F3C1 EXIT {p['side'].upper()} x{p['qty']} @ {p['exit_price']:.2f}\n"
            f"Reason: {p['reason']} | {p['r_multiple']:.2f}R | P&L ${p['pnl_usd']:.2f}"
        )
    if n.event == "no_trade":
        return f"⛔ No trade today ({p.get('reason', 'unknown')})"
    if n.event == "halt":
        return f"\U0001F6D1 TRADING HALTED for the day. Realized P&L ${p['realized_pnl_usd']:.2f}"
    if n.event == "runner_phase_change":
        return f"\U0001F504 Runner switched to {p['phase']}"
    return f"Notify[{n.event}]: {p}"


def format_alert(a: Alert) -> str:
    icon = "\U0001F6A8" if a.level == "critical" else "⚠️"
    return f"{icon} [{a.level.upper()}] {a.message}"
