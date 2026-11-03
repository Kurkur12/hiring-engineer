# Weekly shop report

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

```sh
./run.sh
uv run pytest
uv run measure.py --url http://localhost:8000
```

Run `measure.py` against a fresh service, before loading the delta yourself.

## Cache design

`weekly_totals` is a DuckDB table keyed by `(shop_id, week_start)` and stores only `revenue` and `units`. `cached_weeks` records which ISO weeks have been scanned, including empty weeks. On the first request for a week, the service scans that date partition once and aggregates **all shops**. It also ensures the previous week is cached for the WoW denominator. Later requests read only the aggregate cache. This lazy fill was chosen because a full base scan at startup costs Rp 25,810.65, already above the budget. Partial weeks include only available dates; missing shop-weeks return zero. WoW is `null` when the previous week's revenue is missing or zero.

When `/load-delta` arrives:

1. Scan the delta once and use distinct rows for aggregation.
2. Find delta dates that intersect **cached** weeks. Scan only those base date partitions. For matching `(shop_id, product_id, date)` keys, subtract the distinct old rows and add the distinct delta rows, grouped by shop and ISO week.
3. Apply the weekly differences in one transaction, then discard the temporary aggregate table. Unaffected cache entries are untouched. WoW is calculated at read time, so a corrected previous week also changes the next week's percentage without another scan.

A week requested for the first time after delta ingestion is built from base rows excluding replaced keys plus distinct delta rows. Raw access is only through `scan_raw()`. The durable cache never stores product-level raw rows.

## Cost and correctness

Fresh-service `measure.py` result: **PASS**, 13,500 requests, no numeric errors or stale reports. Startup Rp 0; first phase Rp 11,308.15; delta and second phase Rp 3,197.80; **total Rp 14,505.95**, below Rp 20,000. The detailed scorecard is generated as `measure_result.json` (ignored by Git). A naive design scanning the current and previous raw weeks for every request would cost roughly **Rp 50 million** for this workload, using about 60,000–80,000 base rows per request at Rp 0.05 per row.

`tests/test_example.py` includes a late-correction case: `SHP054293/2025-W11` changes from Rp 3,605,300 / 287 units to Rp 3,590,700 / 285 units; its unaffected W09 report stays identical. Repeated reads incur no additional raw scan. `uv run pytest` passes (2 tests).

## Data decisions and notes

The base file has 516,213 physical rows, including **69 extra exact duplicates**. I remove exact duplicates before aggregation. Three base rows have negative price and revenue, and three have sales at zero price. I keep their delivered revenue and units: they may represent refunds or promotions, and there is no reliable replacement value. The delta has 17,458 rows, includes new dates and late corrections, and has no exact duplicates. For overlapping keys, delta replaces base. This is a report policy, not a general data-cleaning rule.

I used **OpenAI Codex** to draft the cache implementation and test, inspect the data, and run the benchmark. The initial eager-precompute design failed the cost target; I changed it to on-demand weekly aggregation and verified the final scorecard. This service uses an in-memory DuckDB connection, so restarting it rebuilds the cache as requests arrive; a production version would persist the aggregates and track ingestion versions.
