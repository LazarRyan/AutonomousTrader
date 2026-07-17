"""
Nightly job -- runs once per weekday after the close (5:30pm ET via
launchd, see scripts/launchd/), strictly separate from the intraday
trading cycles. Steps, in order:

  1. Equity snapshot: today's account equity + cash + SPY close into
     equity_snapshots -- the raw series behind every performance-vs-SPY
     number this system ever reports. Written FIRST so a failure in any
     later step never costs the day's data point.
  2. Reflection (src/memory/reflection.py): realized outcomes -> journal,
     lessons, refreshed position theses. This is the learning step.
  3. Weekly weight retune (src/signals/weight_tuner.py), Mondays only:
     signal history -> hit rates -> new blend weights + scorecard.
  4. Newsletter (src/newsletter.py): performance, the day's trades,
     tonight's reflection, the scorecard -- written to the vault, then
     emailed (email is best-effort; the vault copy is the record).

Steps 2-4 are each wrapped so one failing doesn't take down the others --
they're independent products of the day, not a dependent chain (the
newsletter of a night whose reflection crashed still carries the trades
and performance). Non-trading days skip everything after a cheap calendar
check: no trades happened, no close printed, nothing to learn or report.

Thin glue throughout, by design -- every piece of logic it calls is
unit-tested in its own module. This file is scheduling + wiring.
"""

from __future__ import annotations


def run_nightly() -> None:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    from src.config import load_settings
    from src.db import get_client, write_audit_log
    from src.memory.vault import Vault, read_scorecard, write_newsletter
    from src.notify import send_macos_notification

    settings = load_settings()
    supabase_client = get_client(settings)
    alpaca_trading_client = TradingClient(
        settings.alpaca_api_key, settings.alpaca_secret_key, paper=not settings.is_live_mode
    )
    vault = Vault()

    today = alpaca_trading_client.get_clock().timestamp.date()
    if not alpaca_trading_client.get_calendar(GetCalendarRequest(start=today, end=today)):
        print(f"Nightly skipped: {today} is not a trading day")
        return

    # ---- 1. Equity snapshot (first, always -- see module docstring).
    account = alpaca_trading_client.get_account()
    spy_close = _fetch_spy_close(settings)
    supabase_client.table("equity_snapshots").upsert(
        {
            "snapshot_date": today.isoformat(),
            "equity": float(account.equity),
            "cash": float(account.cash),
            "spy_close": spy_close,
        },
        on_conflict="snapshot_date",
    ).execute()
    print(f"Equity snapshot written: ${float(account.equity):,.2f} (SPY close: {spy_close})")

    # ---- 2. Reflection.
    reflection = None
    try:
        from src.memory.reflection import run_reflection

        reflection = run_reflection(supabase_client, alpaca_trading_client, settings, vault, today=today)
        print(f"Reflection complete: {len(reflection.lessons)} lesson(s), {len(reflection.theses_by_symbol)} thesis update(s)")
    except Exception as exc:  # noqa: BLE001 -- independent step, see module docstring
        print(f"Nightly reflection failed (continuing with the rest of the nightly job): {exc}")
        write_audit_log(
            supabase_client,
            event_type="nightly",
            decision="reflection_failed",
            reasoning=f"nightly reflection raised: {exc}",
        )

    # ---- 3. Weekly retune (Mondays -- weekday() == 0).
    if today.weekday() == 0:
        try:
            from src.signals.weight_tuner import retune_weights

            weights = retune_weights(supabase_client, settings, vault)
            print(f"Weekly weight retune complete: {weights}")
        except Exception as exc:  # noqa: BLE001
            print(f"Weekly weight retune failed (weights unchanged): {exc}")
            write_audit_log(
                supabase_client,
                event_type="nightly",
                decision="weight_retune_failed",
                reasoning=f"weekly retune raised: {exc}",
            )

    # ---- 4. Newsletter: vault copy first, then best-effort email.
    try:
        from src.newsletter import EquitySnapshot, TradeLine, compute_performance, render_newsletter

        snapshot_rows = (
            supabase_client.table("equity_snapshots").select("snapshot_date, equity, spy_close").order("snapshot_date").execute().data
            or []
        )
        from datetime import date as date_type

        snapshots = [
            EquitySnapshot(
                snapshot_date=date_type.fromisoformat(row["snapshot_date"]),
                equity=float(row["equity"]),
                spy_close=float(row["spy_close"]) if row.get("spy_close") is not None else None,
            )
            for row in snapshot_rows
        ]
        performance = compute_performance(snapshots)

        trade_rows = (
            supabase_client.table("candidate_trades")
            .select("symbol, side, quantity, status, portfolio_manager_reasoning, created_at")
            .gte("created_at", f"{today.isoformat()}T00:00:00Z")
            .order("created_at")
            .execute()
            .data
            or []
        )
        todays_trades = [
            TradeLine(
                symbol=row["symbol"],
                side=row["side"],
                quantity=float(row["quantity"]),
                status=row["status"],
                reasoning=row.get("portfolio_manager_reasoning") or "",
            )
            for row in trade_rows
        ]

        regime_line = _todays_regime_line(settings)

        # Standing lessons for the always-present "Lessons learned" section:
        # the newest entries of vault Lessons.md (the same text recall
        # injects into every trading prompt), parsed from its "- [date] rule"
        # lines. Capped so the letter shows what's most current rather than
        # the full 30-lesson memory.
        from src.memory.vault import read_lessons

        cumulative_lessons = [
            line[2:] for line in read_lessons(vault).splitlines() if line.startswith("- [")
        ][:8]

        # Ops metrics: today's LLM spend (llm_calls), discipline-layer
        # interventions (audit_log), and lesson-citation rate (the trade
        # reasonings already fetched above). Best-effort -- a telemetry
        # query failing should cost the section, not the letter.
        ops_metrics = None
        try:
            from src.newsletter import build_ops_metrics

            day_start = f"{today.isoformat()}T00:00:00Z"
            llm_rows = (
                supabase_client.table("llm_calls")
                .select("call_type, input_tokens, output_tokens, cost_usd")
                .gte("created_at", day_start)
                .execute()
                .data
                or []
            )
            audit_rows = (
                supabase_client.table("audit_log")
                .select("decision")
                .gte("created_at", day_start)
                .execute()
                .data
                or []
            )
            ops_metrics = build_ops_metrics(
                llm_rows, audit_rows, [row.get("portfolio_manager_reasoning") or "" for row in trade_rows]
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ops metrics unavailable for tonight's letter: {exc}")

        newsletter_kwargs = dict(
            day=today,
            performance=performance,
            todays_trades=todays_trades,
            journal_summary=(
                reflection.journal_summary if reflection is not None else "_Reflection unavailable tonight -- see launchd log._"
            ),
            lessons=reflection.lessons if reflection is not None else [],
            scorecard_markdown=read_scorecard(vault),
            regime_line=regime_line,
            cumulative_lessons=cumulative_lessons,
            ops_metrics=ops_metrics,
        )
        newsletter_md = render_newsletter(**newsletter_kwargs)
        write_newsletter(vault, today, newsletter_md)
        print(f"Newsletter written to vault: Newsletters/{today.isoformat()}.md")

        if settings.gmail_address and settings.gmail_app_password:
            from src.newsletter import render_newsletter_html, send_newsletter_email

            try:
                send_newsletter_email(
                    subject=f"Autonomous Trader — {today.isoformat()}",
                    markdown_body=newsletter_md,
                    gmail_address=settings.gmail_address,
                    gmail_app_password=settings.gmail_app_password,
                    to_address=settings.newsletter_to or settings.gmail_address,
                    html_body=render_newsletter_html(**newsletter_kwargs),
                )
                print(f"Newsletter emailed to {settings.newsletter_to or settings.gmail_address}")
            except Exception as exc:  # noqa: BLE001 -- vault copy already written, email is delivery convenience
                print(f"Newsletter email failed (vault copy is written): {exc}")
        else:
            print("GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set -- newsletter saved to vault only")
    except Exception as exc:  # noqa: BLE001
        print(f"Newsletter build failed: {exc}")
        write_audit_log(
            supabase_client,
            event_type="nightly",
            decision="newsletter_failed",
            reasoning=f"newsletter build raised: {exc}",
        )

    send_macos_notification(
        title="Autonomous Trader: nightly complete",
        message=f"Reflection, snapshot and newsletter done for {today.isoformat()}.",
    )


def _fetch_spy_close(settings) -> float | None:
    """Latest SPY daily close, best-effort -- the snapshot row is written
    with spy_close NULL rather than lost when this fails."""
    try:
        from src.signals.momentum import fetch_daily_closes

        closes = fetch_daily_closes("SPY", settings.alpaca_api_key, settings.alpaca_secret_key, lookback_days=7)
        return closes[-1] if closes else None
    except Exception as exc:  # noqa: BLE001
        print(f"SPY close fetch failed, snapshot will have no benchmark point today: {exc}")
        return None


def _todays_regime_line(settings) -> str | None:
    """Recompute the regime read for the newsletter, best-effort."""
    try:
        from src.risk.regime import assess_market_regime, fetch_vix_level
        from src.signals.momentum import fetch_daily_closes

        spy_closes = fetch_daily_closes("SPY", settings.alpaca_api_key, settings.alpaca_secret_key, lookback_days=120)
        regime = assess_market_regime(spy_closes, fetch_vix_level())
        return f"**{regime.label}** (sizing multiplier {regime.sizing_multiplier:g}) — {regime.reasoning}"
    except Exception as exc:  # noqa: BLE001
        print(f"regime line unavailable for newsletter: {exc}")
        return None


if __name__ == "__main__":
    run_nightly()
