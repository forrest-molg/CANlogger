#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  CAN Stream Monitor
#  Queries the canwaves TimescaleDB and reports, per bus/channel:
#    • windows received in the look-back window
#    • capture rate  (windows/sec  — target: ~1000 for 1 ms cadence)
#    • average inter-window gap (ms)
#    • maximum inter-window gap (ms)
#    • dropout count  (gaps > 2 ms)
#
#  Usage:
#    ./stream_monitor.sh [look_back_sec [refresh_sec]]
#
#  Defaults:  look_back_sec = 30,  refresh_sec = 5
#
#  Ctrl-C to exit.
# ─────────────────────────────────────────────────────────────────────────────

LOOK_BACK=${1:-30}
REFRESH=${2:-5}
DROPOUT_THRESHOLD_MS=2

SQL=$(cat <<ENDSQL
WITH recent AS (
    SELECT time, bus_id, channel
    FROM   waveform_chunks
    WHERE  time > NOW() - INTERVAL '${LOOK_BACK} seconds'
),
gapped AS (
    SELECT
        bus_id,
        channel,
        EXTRACT(EPOCH FROM
            (time - LAG(time) OVER (PARTITION BY bus_id, channel ORDER BY time))
        ) * 1000.0  AS gap_ms
    FROM recent
),
stats AS (
    SELECT
        bus_id,
        channel,
        COUNT(gap_ms)                                              AS windows,
        ROUND((COUNT(gap_ms) / ${LOOK_BACK}.0)::numeric, 1)       AS win_per_sec,
        ROUND(AVG(gap_ms)::numeric,  3)                            AS avg_gap_ms,
        ROUND(MAX(gap_ms)::numeric,  3)                            AS max_gap_ms,
        COUNT(*) FILTER (WHERE gap_ms > ${DROPOUT_THRESHOLD_MS})   AS dropouts
    FROM gapped
    WHERE gap_ms IS NOT NULL
    GROUP BY bus_id, channel
)
SELECT
    bus_id,
    channel,
    windows,
    win_per_sec   AS "win/s",
    avg_gap_ms    AS "avg_gap(ms)",
    max_gap_ms    AS "max_gap(ms)",
    dropouts      AS "drops>2ms"
FROM stats
ORDER BY bus_id, channel;
ENDSQL
)

TOTAL_SQL=$(cat <<ENDSQL
SELECT
    COUNT(*)                                              AS total_rows,
    NOW() - MAX(time)                                     AS data_age,
    pg_size_pretty(pg_total_relation_size('waveform_chunks')) AS table_size
FROM waveform_chunks;
ENDSQL
)

while true; do
    clear
    echo "═══════════════════════════════════════════════════════════════════"
    printf "  CAN Stream Monitor  — last %ds  │  refresh every %ds  │  %s\n" \
        "$LOOK_BACK" "$REFRESH" "$(date +'%Y-%m-%d %H:%M:%S')"
    echo "═══════════════════════════════════════════════════════════════════"
    echo ""

    result=$(docker exec canwaves-db psql -U canops -d canwaves \
        --no-psqlrc -q -c "$SQL" 2>&1)
    exit_code=$?

    if [[ $exit_code -ne 0 ]]; then
        echo "  ERROR: could not query canwaves-db (exit $exit_code)"
        echo "  $result"
    elif echo "$result" | grep -q "(0 rows)"; then
        echo "  No data in the last ${LOOK_BACK}s — capture may be stopped or"
        echo "  PicoScopes not yet connected."
    else
        echo "$result"
        echo ""
        # Flag any bus with dropouts
        dropout_line=$(echo "$result" | awk -F'|' 'NR>2 && $7+0 > 0 {
            gsub(/ /,"",$1); gsub(/ /,"",$2); gsub(/ /,"",$7);
            printf "  ⚠  BUS %s / CH %s : %s dropout(s) > 2ms detected\n", $1, $2, $7
        }')
        if [[ -n "$dropout_line" ]]; then
            echo "─── DROPOUT ALERT ──────────────────────────────────────────────"
            echo "$dropout_line"
            echo "────────────────────────────────────────────────────────────────"
        else
            echo "  All gaps within 2ms threshold — no dropouts detected."
        fi
    fi

    echo ""
    echo "─── Database totals ────────────────────────────────────────────────"
    docker exec canwaves-db psql -U canops -d canwaves \
        --no-psqlrc -q -c "$TOTAL_SQL" 2>&1
    echo ""
    echo "  Press Ctrl-C to exit"
    sleep "$REFRESH"
done
