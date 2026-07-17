"""
Nightly newsletter -- the end-of-day report Ryan actually reads.

Built by src/nightly.py after the close, from pieces that already exist by
the time it runs: the day's candidate_trades rows, tonight's reflection
result (src/memory/reflection.py), the current scorecard, and the running
equity_snapshots history for the performance-vs-SPY comparison. Saved to
the vault (Newsletters/YYYY-MM-DD.md -- readable in Obsidian alongside the
journal it summarizes) and emailed via Gmail SMTP.

The benchmark comparison is the honest core of the whole exercise: this
system only earns the right to be taken seriously if the equity curve
beats just holding SPY. Both are tracked from the same snapshot series so
neither side gets a flattering start date.

Split:
  1. PURE, unit-tested (tests/test_newsletter.py): compute_performance()
     and render_newsletter() -- deterministic math/markdown from typed
     inputs.
  2. Thin glue: send_newsletter_email() (smtplib against Gmail; requires
     GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env -- an App Password from
     https://myaccount.google.com/apppasswords, NOT the account password).
     Email failure is caught by the caller and must never lose the
     newsletter itself -- the vault copy is written first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EquitySnapshot:
    snapshot_date: date
    equity: float
    spy_close: float | None


@dataclass(frozen=True)
class PerformanceSummary:
    day_return_pct: float | None            # None on the first snapshot ever
    since_inception_return_pct: float | None
    spy_day_return_pct: float | None
    spy_since_inception_return_pct: float | None
    inception_date: date | None
    latest_equity: float


def compute_performance(snapshots: list[EquitySnapshot]) -> PerformanceSummary:
    """Day and since-inception returns for the account and for SPY, from
    the same snapshot series. Pure, unit-tested. SPY legs are None
    whenever the needed SPY closes are missing (best-effort column) --
    a missing benchmark renders as 'n/a', never as a made-up number."""
    if not snapshots:
        raise ValueError("compute_performance requires at least one snapshot")

    ordered = sorted(snapshots, key=lambda s: s.snapshot_date)
    first, latest = ordered[0], ordered[-1]
    previous = ordered[-2] if len(ordered) >= 2 else None

    def pct(new: float | None, old: float | None) -> float | None:
        if new is None or old is None or old == 0:
            return None
        return (new - old) / old

    return PerformanceSummary(
        day_return_pct=pct(latest.equity, previous.equity if previous else None),
        since_inception_return_pct=pct(latest.equity, first.equity) if len(ordered) >= 2 else None,
        spy_day_return_pct=pct(latest.spy_close, previous.spy_close if previous else None),
        spy_since_inception_return_pct=pct(latest.spy_close, first.spy_close) if len(ordered) >= 2 else None,
        inception_date=first.snapshot_date if len(ordered) >= 2 else None,
        latest_equity=latest.equity,
    )


@dataclass(frozen=True)
class TradeLine:
    """One candidate_trades row from today, as rendered in the newsletter."""

    symbol: str
    side: str
    quantity: float
    status: str
    reasoning: str


# ============================================================
# Ops metrics (2026-07-16): what did today's intelligence cost, and what
# did the discipline layer do? Aggregated from llm_calls + audit_log rows
# by the pure builder below; rendered as its own newsletter section.
# ============================================================

# audit_log decisions that count as "the discipline layer intervened",
# with the labels the newsletter shows for them.
GUARD_DECISION_LABELS = {
    "churn_suppressed": "churn guard suppressions",
    "sector_cap_blocked": "sector cap blocks",
    "liquidity_floor_blocked": "liquidity floor blocks",
    "quantity_scaled": "buy quantities scaled down",
}

_LESSON_CITATION_MARKER = "lesson"


@dataclass(frozen=True)
class OpsMetrics:
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_by_type: dict[str, float]           # call_type -> $ (only types seen today)
    guard_interventions: dict[str, int]      # label -> count (only guards that fired)
    trades_citing_lessons: int               # today's proposals whose reasoning references a lesson
    total_trades: int


def build_ops_metrics(
    llm_rows: list[dict],
    audit_rows: list[dict],
    trade_reasonings: list[str],
) -> OpsMetrics:
    """Aggregate the day's telemetry. Pure, unit-tested.

    llm_rows: llm_calls rows (call_type, input_tokens, output_tokens,
    cost_usd). audit_rows: audit_log rows (decision). trade_reasonings:
    today's candidate_trades portfolio_manager_reasoning strings --
    lesson citation is detected by the word 'lesson' appearing in the
    reasoning, which the system prompt explicitly asks for when a lesson
    changes a decision. Crude but honest: it measures whether the model is
    doing the citing it was told to do, not semantic influence."""
    cost_by_type: dict[str, float] = {}
    input_tokens = output_tokens = 0
    cost_usd = 0.0
    for row in llm_rows:
        call_type = row.get("call_type", "unknown")
        row_cost = float(row.get("cost_usd", 0) or 0)
        cost_by_type[call_type] = cost_by_type.get(call_type, 0.0) + row_cost
        cost_usd += row_cost
        input_tokens += int(row.get("input_tokens", 0) or 0)
        output_tokens += int(row.get("output_tokens", 0) or 0)

    guard_interventions: dict[str, int] = {}
    for row in audit_rows:
        label = GUARD_DECISION_LABELS.get(row.get("decision", ""))
        if label:
            guard_interventions[label] = guard_interventions.get(label, 0) + 1

    trades_citing = sum(1 for reasoning in trade_reasonings if _LESSON_CITATION_MARKER in (reasoning or "").lower())

    return OpsMetrics(
        llm_calls=len(llm_rows),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        cost_by_type=cost_by_type,
        guard_interventions=guard_interventions,
        trades_citing_lessons=trades_citing,
        total_trades=len(trade_reasonings),
    )


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2%}" if value is not None else "n/a"


_NO_LESSONS_YET = (
    "No realized-outcome lessons yet. Lessons are distilled only from closed "
    "positions (entry reasoning vs. actual P&L), so this section fills in once "
    "the first round trips complete -- not from hunches or unrealized moves."
)


def render_newsletter(
    day: date,
    performance: PerformanceSummary,
    todays_trades: list[TradeLine],
    journal_summary: str,
    lessons: list[str],
    scorecard_markdown: str,
    regime_line: str | None,
    cumulative_lessons: list[str] | None = None,
    ops_metrics: OpsMetrics | None = None,
) -> str:
    """The full newsletter markdown. Pure, unit-tested."""
    lines: list[str] = [
        f"# Autonomous Trader — Daily Letter, {day.isoformat()}",
        "",
        "## Performance",
        "",
        f"Equity: ${performance.latest_equity:,.2f}",
        "",
        "| | Portfolio | SPY (benchmark) |",
        "|---|---|---|",
        f"| Today | {_fmt_pct(performance.day_return_pct)} | {_fmt_pct(performance.spy_day_return_pct)} |",
    ]
    if performance.inception_date is not None:
        lines.append(
            f"| Since {performance.inception_date.isoformat()} | "
            f"{_fmt_pct(performance.since_inception_return_pct)} | "
            f"{_fmt_pct(performance.spy_since_inception_return_pct)} |"
        )
    lines += ["", "## The day in trades", ""]

    if todays_trades:
        for trade in todays_trades:
            lines.append(f"- **{trade.symbol} {trade.side.upper()} x{trade.quantity:g}** [{trade.status}] — {trade.reasoning}")
    else:
        lines.append("No trades today.")

    lines += ["", "## Reflection", "", journal_summary.strip()]

    # Lessons learned: always present (2026-07-16, by request) -- tonight's
    # new distillations first, then the standing rules the trader is
    # actually operating under (the top of vault Lessons.md, which recall
    # injects into every trading prompt), with an honest placeholder before
    # the first position ever closes.
    lines += ["", "## Lessons learned", ""]
    if lessons:
        lines += ["**New tonight:**", ""]
        lines += [f"- {lesson}" for lesson in lessons]
        lines += [""]
    standing = [l for l in (cumulative_lessons or []) if l not in (lessons or [])]
    if standing:
        lines += ["**Standing lessons (in effect every cycle):**", ""]
        lines += [f"- {lesson}" for lesson in standing]
    elif not lessons:
        lines += [_NO_LESSONS_YET]

    if regime_line:
        lines += ["", "## Market regime", "", regime_line]

    if scorecard_markdown.strip():
        lines += ["", "## Signal scorecard", ""]
        # Drop the scorecard's own H1 so the newsletter has one title.
        lines += [line for line in scorecard_markdown.splitlines() if not line.startswith("# ")]

    if ops_metrics is not None:
        lines += ["", "## Under the hood", ""]
        lines.append(
            f"LLM spend today: **${ops_metrics.cost_usd:.2f}** across {ops_metrics.llm_calls} call(s) "
            f"({ops_metrics.input_tokens:,} in / {ops_metrics.output_tokens:,} out tokens)."
        )
        if ops_metrics.cost_by_type:
            breakdown = ", ".join(f"{t}: ${c:.2f}" for t, c in sorted(ops_metrics.cost_by_type.items()))
            lines.append(f"By stage — {breakdown}.")
        if ops_metrics.guard_interventions:
            guards = "; ".join(f"{count} {label}" for label, count in sorted(ops_metrics.guard_interventions.items()))
            lines.append(f"Discipline layer: {guards}.")
        else:
            lines.append("Discipline layer: no interventions needed today.")
        if ops_metrics.total_trades:
            lines.append(
                f"Memory in action: {ops_metrics.trades_citing_lessons} of {ops_metrics.total_trades} "
                f"proposal(s) cited a learned lesson in their reasoning."
            )

    lines += [
        "",
        "---",
        "_Automated paper trading. Not financial advice. Full decision trail in the vault and audit log._",
        "",
    ]
    return "\n".join(lines)


# ============================================================
# HTML rendering -- what actually lands in the inbox (2026-07-16: the
# first real delivery showed Gmail rendering the markdown as literal
# text, pipes and asterisks included). Built from the SAME typed inputs
# as render_newsletter, never by converting the markdown, so the two
# versions can't drift. All styles inline -- email clients strip
# <style> blocks. Pure, unit-tested (tests/test_newsletter.py).
# ============================================================

_POSITIVE_COLOR = "#1e7e34"
_NEGATIVE_COLOR = "#b02a37"
_MUTED_COLOR = "#6c757d"
_BORDER = "1px solid #e3e6ea"

_STATUS_COLORS = {
    "executed": _POSITIVE_COLOR,
    "auto_approved": _POSITIVE_COLOR,
    "blocked": _NEGATIVE_COLOR,
    "execution_failed": _NEGATIVE_COLOR,
    "rejected": _NEGATIVE_COLOR,
    "queued_for_approval": "#b8860b",
}

_BOLD_MD_RE = None  # set lazily in _inline_md_to_html to keep re import local


def _inline_md_to_html(text: str) -> str:
    """HTML-escape a string, then honor the one bit of inline markdown the
    pipeline actually produces (**bold**, e.g. the regime label). Pure."""
    import html
    import re

    escaped = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def _pct_cell(value: float | None) -> str:
    if value is None:
        return f'<td style="padding:6px 12px; border:{_BORDER}; color:{_MUTED_COLOR};">n/a</td>'
    color = _POSITIVE_COLOR if value >= 0 else _NEGATIVE_COLOR
    return f'<td style="padding:6px 12px; border:{_BORDER}; color:{color}; font-weight:600;">{value:+.2%}</td>'


def _markdown_table_to_html(markdown: str) -> str:
    """Convert the scorecard's simple pipe table to an HTML table. Only
    handles the exact shape render_scorecard produces (header row, |---|
    separator, data rows) -- not a general markdown parser. Pure,
    unit-tested."""
    rows = [line for line in markdown.splitlines() if line.strip().startswith("|")]
    rows = [row for row in rows if not set(row.replace("|", "").strip()) <= {"-", " ", ":"}]
    if not rows:
        return ""

    def cells(row: str, tag: str) -> str:
        import html

        parts = [c.strip() for c in row.strip().strip("|").split("|")]
        style = f"padding:6px 12px; border:{_BORDER}; text-align:left;"
        if tag == "th":
            style += " background:#f6f8fa;"
        return "".join(f"<{tag} style=\"{style}\">{html.escape(c)}</{tag}>" for c in parts)

    header = f"<tr>{cells(rows[0], 'th')}</tr>"
    body = "".join(f"<tr>{cells(row, 'td')}</tr>" for row in rows[1:])
    return f'<table style="border-collapse:collapse; font-size:13px; margin:8px 0;">{header}{body}</table>'


def _section_heading(text: str) -> str:
    return (
        f'<h2 style="font-size:15px; margin:24px 0 8px; color:#1a1d21; '
        f'border-bottom:{_BORDER}; padding-bottom:4px;">{text}</h2>'
    )


def render_newsletter_html(
    day: date,
    performance: PerformanceSummary,
    todays_trades: list[TradeLine],
    journal_summary: str,
    lessons: list[str],
    scorecard_markdown: str,
    regime_line: str | None,
    cumulative_lessons: list[str] | None = None,
    ops_metrics: OpsMetrics | None = None,
) -> str:
    """The HTML twin of render_newsletter -- same inputs, same sections,
    inbox-ready. Pure, unit-tested."""
    parts: list[str] = [
        '<div style="max-width:640px; margin:0 auto; font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#1a1d21; font-size:14px; line-height:1.55; "
        'padding:8px 4px;">',
        f'<h1 style="font-size:19px; margin:0 0 2px;">Autonomous Trader</h1>',
        f'<p style="margin:0 0 16px; color:{_MUTED_COLOR};">Daily letter — {day.strftime("%A, %B %-d, %Y")}</p>',
        _section_heading("Performance"),
        f'<p style="font-size:24px; font-weight:700; margin:8px 0;">${performance.latest_equity:,.2f}</p>',
        '<table style="border-collapse:collapse; font-size:13px;">',
        f'<tr><th style="padding:6px 12px; border:{_BORDER}; background:#f6f8fa;"></th>'
        f'<th style="padding:6px 12px; border:{_BORDER}; background:#f6f8fa; text-align:left;">Portfolio</th>'
        f'<th style="padding:6px 12px; border:{_BORDER}; background:#f6f8fa; text-align:left;">SPY (benchmark)</th></tr>',
        f'<tr><td style="padding:6px 12px; border:{_BORDER};">Today</td>'
        f"{_pct_cell(performance.day_return_pct)}{_pct_cell(performance.spy_day_return_pct)}</tr>",
    ]
    if performance.inception_date is not None:
        parts.append(
            f'<tr><td style="padding:6px 12px; border:{_BORDER};">Since {performance.inception_date.isoformat()}</td>'
            f"{_pct_cell(performance.since_inception_return_pct)}{_pct_cell(performance.spy_since_inception_return_pct)}</tr>"
        )
    parts.append("</table>")

    parts.append(_section_heading("The day in trades"))
    if todays_trades:
        for trade in todays_trades:
            status_color = _STATUS_COLORS.get(trade.status, _MUTED_COLOR)
            parts.append(
                f'<p style="margin:10px 0; padding:10px 12px; background:#f8f9fb; border-left:3px solid {status_color}; border-radius:0 4px 4px 0;">'
                # &times; (not a literal multiplication-sign character): the
                # entity renders correctly regardless of what charset the
                # viewer assumes -- a raw U+00D7 showed up as "Ã—" the first
                # time this HTML was opened without a UTF-8 declaration.
                f"<strong>{_inline_md_to_html(trade.symbol)} {trade.side.upper()} &times;{trade.quantity:g}</strong> "
                f'<span style="color:{status_color}; font-size:12px; font-weight:600; text-transform:uppercase;">{_inline_md_to_html(trade.status.replace("_", " "))}</span>'
                f'<br><span style="color:#3f4650;">{_inline_md_to_html(trade.reasoning)}</span></p>'
            )
    else:
        parts.append(f'<p style="color:{_MUTED_COLOR};">No trades today.</p>')

    parts.append(_section_heading("Reflection"))
    parts.append(f"<p>{_inline_md_to_html(journal_summary.strip())}</p>")

    # Lessons learned: always present -- same structure and reasoning as
    # the markdown twin above.
    parts.append(_section_heading("Lessons learned"))
    standing = [l for l in (cumulative_lessons or []) if l not in (lessons or [])]
    if lessons:
        parts.append(f'<p style="margin:8px 0 4px; font-weight:600; color:{_POSITIVE_COLOR};">New tonight</p>')
        parts.append('<ul style="margin:4px 0; padding-left:20px;">')
        parts.extend(f'<li style="margin:4px 0;">{_inline_md_to_html(lesson)}</li>' for lesson in lessons)
        parts.append("</ul>")
    if standing:
        parts.append('<p style="margin:12px 0 4px; font-weight:600;">Standing lessons (in effect every cycle)</p>')
        parts.append('<ul style="margin:4px 0; padding-left:20px;">')
        parts.extend(
            f'<li style="margin:4px 0; color:#3f4650;">{_inline_md_to_html(lesson)}</li>' for lesson in standing
        )
        parts.append("</ul>")
    elif not lessons:
        parts.append(f'<p style="color:{_MUTED_COLOR}; font-style:italic;">{_inline_md_to_html(_NO_LESSONS_YET)}</p>')

    if regime_line:
        parts.append(_section_heading("Market regime"))
        parts.append(f"<p>{_inline_md_to_html(regime_line)}</p>")

    scorecard_table = _markdown_table_to_html(scorecard_markdown)
    if scorecard_table:
        parts.append(_section_heading("Signal scorecard"))
        parts.append(scorecard_table)

    if ops_metrics is not None:
        parts.append(_section_heading("Under the hood"))
        parts.append(
            f'<p style="margin:8px 0;">LLM spend today: <strong>${ops_metrics.cost_usd:.2f}</strong> '
            f"across {ops_metrics.llm_calls} call(s) "
            f'<span style="color:{_MUTED_COLOR};">({ops_metrics.input_tokens:,} in / {ops_metrics.output_tokens:,} out tokens)</span></p>'
        )
        if ops_metrics.cost_by_type:
            breakdown = ", ".join(
                f"{_inline_md_to_html(t)}: ${c:.2f}" for t, c in sorted(ops_metrics.cost_by_type.items())
            )
            parts.append(f'<p style="margin:4px 0; color:#3f4650;">By stage — {breakdown}</p>')
        if ops_metrics.guard_interventions:
            guards = "; ".join(
                f"{count} {_inline_md_to_html(label)}"
                for label, count in sorted(ops_metrics.guard_interventions.items())
            )
            parts.append(f'<p style="margin:4px 0;">Discipline layer: {guards}</p>')
        else:
            parts.append(
                f'<p style="margin:4px 0; color:{_MUTED_COLOR};">Discipline layer: no interventions needed today.</p>'
            )
        if ops_metrics.total_trades:
            parts.append(
                f'<p style="margin:4px 0;">Memory in action: {ops_metrics.trades_citing_lessons} of '
                f"{ops_metrics.total_trades} proposal(s) cited a learned lesson in their reasoning.</p>"
            )

    parts.append(
        f'<p style="margin-top:28px; padding-top:12px; border-top:{_BORDER}; color:{_MUTED_COLOR}; font-size:12px;">'
        "Automated paper trading. Not financial advice. Full decision trail in the vault and audit log.</p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


# ============================================================
# Thin glue -- Gmail SMTP.
# ============================================================


def send_newsletter_email(
    subject: str,
    markdown_body: str,
    gmail_address: str,
    gmail_app_password: str,
    to_address: str,
    html_body: str | None = None,
) -> None:
    """Send the newsletter. With html_body given (the normal path since
    2026-07-16), sends multipart/alternative -- HTML for the inbox, the
    markdown as the plain-text fallback; without it, plain text only.
    Raises on failure -- the CALLER decides that email failure is
    survivable (the vault copy already exists), this function doesn't
    pre-swallow."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if html_body is not None:
        message = MIMEMultipart("alternative")
        # Order matters: last part is the one clients prefer, so plain first.
        message.attach(MIMEText(markdown_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))
    else:
        message = MIMEText(markdown_body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = gmail_address
    message["To"] = to_address

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(gmail_address, gmail_app_password)
        smtp.sendmail(gmail_address, [to_address], message.as_string())
