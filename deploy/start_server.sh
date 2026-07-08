#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# DevOps Intelligence Platform — Production start script
# For 10-20 concurrent users on Adobe Eris server
# ─────────────────────────────────────────────────────────────────────────────

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=${PORT:-8501}
NUM_WORKERS=${NUM_WORKERS:-3}   # 3 Streamlit processes behind nginx

cd "$APP_DIR"
source .env 2>/dev/null || true

echo "=== DevOps Intelligence Platform ==="
echo "App dir: $APP_DIR"
echo "Workers: $NUM_WORKERS Streamlit processes"
echo ""

# Option 1: Single Streamlit (development / small team < 5 users)
if [ "${NUM_WORKERS}" = "1" ]; then
    echo "Starting single Streamlit on port $PORT..."
    streamlit run dashboard/app.py \
        --server.port "$PORT" \
        --server.address 0.0.0.0 \
        --server.headless true \
        --server.maxUploadSize 50 \
        --browser.gatherUsageStats false
    exit 0
fi

# Option 2: Multiple Streamlit processes behind nginx (10-20 users)
# Each process handles its own sessions independently
# nginx load-balances with sticky sessions (ip_hash)
echo "Starting $NUM_WORKERS Streamlit workers..."

PIDS=()
for i in $(seq 1 "$NUM_WORKERS"); do
    WORKER_PORT=$((PORT + i - 1))
    echo "  Worker $i on port $WORKER_PORT"
    streamlit run dashboard/app.py \
        --server.port "$WORKER_PORT" \
        --server.address 127.0.0.1 \
        --server.headless true \
        --server.maxUploadSize 50 \
        --browser.gatherUsageStats false \
        >> "logs/worker_${i}.log" 2>&1 &
    PIDS+=($!)
done

echo ""
echo "Workers started: ${PIDS[*]}"
echo "Now start nginx with: nginx -c $APP_DIR/deploy/nginx.conf"
echo ""
echo "To stop all workers:"
echo "  kill ${PIDS[*]}"

# Keep running, restart workers if they die
trap "kill ${PIDS[*]}; exit 0" SIGTERM SIGINT

while true; do
    for i in "${!PIDS[@]}"; do
        if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
            WORKER_PORT=$((PORT + i))
            echo "Worker $((i+1)) died, restarting on port $WORKER_PORT..."
            streamlit run dashboard/app.py \
                --server.port "$WORKER_PORT" \
                --server.address 127.0.0.1 \
                --server.headless true \
                --server.headless true \
                >> "logs/worker_$((i+1)).log" 2>&1 &
            PIDS[$i]=$!
        fi
    done
    sleep 10
done
