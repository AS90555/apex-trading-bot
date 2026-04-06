#!/bin/bash
# APEX Dashboard API + Cloudflare Tunnel Startup Script
# Startet api_server.py (Port 8889) + cloudflared Tunnel

APEX_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$APEX_DIR/venv/bin/python3"
LOG_DIR="$APEX_DIR/logs"
API_PID="$APEX_DIR/api_server.pid"
CLOUDFLARED_PID="$APEX_DIR/cloudflared_api.pid"
API_LOG="$LOG_DIR/api_server.log"
CLOUDFLARED_LOG="$LOG_DIR/cloudflared_api.log"

mkdir -p "$LOG_DIR"

# ── Hilfsfunktion: Prozess starten falls nicht läuft ──────────────────────────
start_if_not_running() {
    local name=$1
    local pidfile=$2
    local logfile=$3
    shift 3
    local command=("$@")

    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "✅ $name bereits aktiv (PID $(cat "$pidfile"))"
        return 0
    fi

    echo "🚀 Starte $name ..."
    cd "$APEX_DIR"
    nohup "${command[@]}" >> "$logfile" 2>&1 &
    echo $! > "$pidfile"
    sleep 1
    if kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "✅ $name gestartet (PID $(cat "$pidfile"))"
    else
        echo "❌ $name konnte nicht gestartet werden – prüfe $logfile"
    fi
}

# ── API Server starten ────────────────────────────────────────────────────────
start_if_not_running \
    "API Server (Port 8889)" \
    "$API_PID" \
    "$API_LOG" \
    "$VENV_PYTHON" "$APEX_DIR/api_server.py"

# ── Cloudflare Tunnel starten ─────────────────────────────────────────────────
start_if_not_running \
    "Cloudflare Tunnel (→ 8889)" \
    "$CLOUDFLARED_PID" \
    "$CLOUDFLARED_LOG" \
    cloudflared tunnel --url http://localhost:8889

# ── Tunnel-URL aus Log lesen (max. 15s warten) ────────────────────────────────
echo ""
echo "⏳ Warte auf Cloudflare Tunnel-URL ..."
TUNNEL_URL=""
for i in $(seq 1 15); do
    TUNNEL_URL=$(grep -oP 'https://[a-z0-9\-]+\.trycloudflare\.com' "$CLOUDFLARED_LOG" 2>/dev/null | tail -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 1
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  APEX Dashboard API – Startup komplett"
echo "══════════════════════════════════════════════════════"
if [ -n "$TUNNEL_URL" ]; then
    echo "  Tunnel-URL:  $TUNNEL_URL"
    echo "  Dashboard:   $TUNNEL_URL/api/dashboard"
    echo "  Health:      $TUNNEL_URL/api/health"
else
    echo "  ⚠️  Tunnel-URL noch nicht verfügbar – prüfe:"
    echo "     tail -f $CLOUDFLARED_LOG"
fi
echo ""
echo "  API_TOKEN: $(grep 'API_TOKEN' "$APEX_DIR/api_server.py" | head -1 | cut -d'"' -f2)"
echo "══════════════════════════════════════════════════════"
echo ""
echo "📊 Prozess-Status:"
ps aux | grep -E "(api_server|cloudflared)" | grep -v grep
