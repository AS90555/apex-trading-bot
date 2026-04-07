#!/bin/bash
# APEX Session Start Hook
# Läuft automatisch beim ersten Prompt einer neuen Session.
# Marker-Datei verhindert mehrfachen Aufruf pro Tag.

MARKER="/tmp/apex_session_$(date +%Y-%m-%d).marker"

if [ ! -f "$MARKER" ]; then
    touch "$MARKER"
    echo ""
    echo "━━━ APEX AUTO-CONTEXT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    /root/apex-trading-bot/venv/bin/python3 /root/apex-trading-bot/scripts/apex_status.py 2>/dev/null
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi
