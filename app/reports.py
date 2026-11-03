"""Aggregate-only weekly cache and precise delta updates."""

from datetime import date, timedelta

from app.raw import db, scan_raw

_delta_loaded = False


def startup() -> None:
    """Create cache tables without scanning the expensive source."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS weekly_totals (
            shop_id VARCHAR NOT NULL,
            week_start DATE NOT NULL,
            revenue BIGINT NOT NULL,
            units BIGINT NOT NULL,
            PRIMARY KEY (shop_id, week_start)
        )
    """)
    db.execute("CREATE TABLE IF NOT EXISTS cached_weeks (week_start DATE PRIMARY KEY)")


def _ensure_week(week_start: date) -> None:
    if db.execute("SELECT 1 FROM cached_weeks WHERE week_start = ?", [week_start]).fetchone():
        return
    dates = [week_start + timedelta(days=i) for i in range(7)]
    with scan_raw("base", dates) as base:
        if _delta_loaded:
            with scan_raw("delta", dates) as delta:
                rows = f"""
                    SELECT DISTINCT b.* FROM {base} b
                    ANTI JOIN {delta} d USING (shop_id, product_id, date)
                    UNION
                    SELECT DISTINCT * FROM {delta}
                """
                _fill_week(week_start, rows)
        else:
            _fill_week(week_start, f"SELECT DISTINCT * FROM {base}")


def _fill_week(week_start: date, rows: str) -> None:
    db.execute(f"""
        INSERT INTO weekly_totals
        SELECT shop_id, ?::DATE, sum(revenue)::BIGINT, sum(units_sold)::BIGINT
        FROM ({rows}) GROUP BY shop_id
    """, [week_start])
    # A week with no source rows still needs a cache marker.
    db.execute("INSERT INTO cached_weeks VALUES (?)", [week_start])


def weekly_report(shop_id: str, week_start: date) -> dict:
    """Read this week and its predecessor from the aggregate cache."""
    _ensure_week(week_start)
    _ensure_week(week_start - timedelta(days=7))
    rows = db.execute("""
        SELECT week_start, revenue, units FROM weekly_totals
        WHERE shop_id = ? AND week_start IN (?, ?)
    """, [shop_id, week_start, week_start - timedelta(days=7)]).fetchall()
    by_week = {week: (revenue, units) for week, revenue, units in rows}
    revenue, units = by_week.get(week_start, (0, 0))
    previous = by_week.get(week_start - timedelta(days=7))
    previous_revenue = previous[0] if previous else 0
    wow = 100.0 * (revenue - previous_revenue) / previous_revenue if previous_revenue else None
    return {"revenue": int(revenue), "units": int(units), "revenue_wow_pct": wow}


def on_delta_loaded() -> None:
    """Replace affected product-days and apply their net weekly changes."""
    global _delta_loaded
    with scan_raw("delta") as delta:
        dates = [row[0] for row in db.execute(f"""
            SELECT DISTINCT d.date FROM {delta} d
            JOIN cached_weeks w ON date_trunc('week', d.date)::DATE = w.week_start
        """).fetchall()]
        if dates:
            with scan_raw("base", dates) as base:
                db.execute(f"""
                    CREATE TEMP TABLE weekly_changes AS
                    WITH old_rows AS (
                        SELECT DISTINCT b.* FROM {base} b
                        SEMI JOIN {delta} d
                        ON b.shop_id = d.shop_id
                        AND b.product_id = d.product_id
                        AND b.date = d.date
                    ), changes AS (
                        SELECT d.shop_id, d.date, d.revenue, d.units_sold
                        FROM (SELECT DISTINCT * FROM {delta}) d
                        JOIN cached_weeks w
                        ON date_trunc('week', d.date)::DATE = w.week_start
                        UNION ALL
                        SELECT shop_id, date, -revenue, -units_sold FROM old_rows
                    )
                    SELECT shop_id, date_trunc('week', date)::DATE AS week_start,
                           sum(revenue)::BIGINT AS revenue,
                           sum(units_sold)::BIGINT AS units
                    FROM changes
                    GROUP BY shop_id, date_trunc('week', date)::DATE
                """)

            try:
                db.execute("BEGIN TRANSACTION")
                try:
                    db.execute("""
                        INSERT INTO weekly_totals
                        SELECT shop_id, week_start, revenue, units FROM weekly_changes
                        ON CONFLICT (shop_id, week_start) DO UPDATE SET
                            revenue = weekly_totals.revenue + excluded.revenue,
                            units = weekly_totals.units + excluded.units
                    """)
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
            finally:
                db.execute("DROP TABLE weekly_changes")
    _delta_loaded = True
