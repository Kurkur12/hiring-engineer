"""An example to start from. Run with: uv run pytest

The raw data and the cost counters live in module-level state, so all tests in
one run share them. Compare /stats before and after a call to see what it cost.

Each `TestClient(app)` block runs `startup()` again, but the delta stays loaded:
only the first POST /load-delta in a run returns {"loaded": true}. If your
startup() rebuilds the cache, make sure it still accounts for an already loaded
delta, or keep all delta tests in one TestClient block.
"""

from fastapi.testclient import TestClient

from app.main import app


def test_stats_reports_rows_and_cost():
    with TestClient(app) as client:
        stats = client.get("/stats").json()
    assert stats["cost_rp"] == stats["rows_scanned"] * 0.05


def test_late_correction_refreshes_only_affected_cached_report():
    with TestClient(app) as client:
        old_week = "/shops/SHP054293/weekly-report?week=2025-W09"
        corrected_week = "/shops/SHP054293/weekly-report?week=2025-W11"

        unchanged_before = client.get(old_week).json()
        corrected_before = client.get(corrected_week).json()
        assert corrected_before["revenue"] == 3_605_300
        assert corrected_before["units"] == 287

        assert client.post("/load-delta").json() == {"loaded": True}
        cost_after_load = client.get("/stats").json()["cost_rp"]

        unchanged_after = client.get(old_week).json()
        corrected_after = client.get(corrected_week).json()
        assert unchanged_after == unchanged_before
        assert corrected_after["revenue"] == 3_590_700
        assert corrected_after["units"] == 285
        assert corrected_after["revenue_wow_pct"] != corrected_before["revenue_wow_pct"]
        assert client.get("/stats").json()["cost_rp"] == cost_after_load
        assert client.post("/load-delta").json()["loaded"] is False

        # W12 was never requested before ingestion: its cold fill must merge
        # new days and corrections, instead of using only the base extract.
        newly_requested = client.get(
            "/shops/SHP054293/weekly-report?week=2025-W12"
        ).json()
        assert newly_requested["revenue"] == 3_075_000
        assert newly_requested["units"] == 278
