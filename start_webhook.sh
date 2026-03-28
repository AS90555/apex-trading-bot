#!/bin/bash
# APEX Webhook Server Startup Script

WEBHOOK_DIR="/data/.openclaw/workspace/projects/apex-trading"
WEBHOOK_PID="$WEBHOOK_DIR/webhook.pid"
CLOUDFLARED_PID="$WEBHOOK_DIR/cloudflared.pid"

# Funktion: Prozess starten wenn nicht läuft
start_if_not_running() {
    local name=$1
    local pidfile=$2
    local command=$3
    local logfile=$4
    
    if [ -f "$pidfile" ] && kill -0 $(cat "$pidfile") 2>/dev/null; then
        echo "✅ $name already running (PID $(cat $pidfile))"
        return 0
    fi
    
    echo "🚀 Starting $name..."
    cd "$WEBHOOK_DIR"
    nohup $command > "$logfile" 2>&1 &
    echo $! > "$pidfile"
    echo "✅ $name started (PID $(cat $pidfile))"
}

# Webhook Server starten
start_if_not_running \
    "Webhook Server" \
    "$WEBHOOK_PID" \
    "python3 $WEBHOOK_DIR/webhook_server.py" \
    "$WEBHOOK_DIR/webhook.log"

# Cloudflared Tunnel starten
start_if_not_running \
    "Cloudflare Tunnel" \
    "$CLOUDFLARED_PID" \
    "cloudflared tunnel --url http://localhost:8888" \
    "$WEBHOOK_DIR/cloudflared.log"

echo ""
echo "📊 Status:"
ps aux | grep -E "(webhook_server|cloudflared)" | grep -v grep
