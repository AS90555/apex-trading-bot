#!/bin/bash
# APEX Session Context Hook
# Läuft automatisch beim ersten Prompt jeder neuen Session.
# Marker verhindert mehrfachen Aufruf pro Tag.

MARKER="/tmp/apex_session_$(date +%Y-%m-%d).marker"

if [ ! -f "$MARKER" ]; then
    touch "$MARKER"
    /root/apex-trading-bot/venv/bin/python3 \
        /root/apex-trading-bot/scripts/session_context.py 2>/dev/null
fi
