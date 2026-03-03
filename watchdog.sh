#\!/bin/bash
# Pollen Health Watchdog
# Runs every 5 minutes via cron to monitor and restart services as needed.

LOG="/var/log/pollen-watchdog.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

log() {
    echo "[$TIMESTAMP] $1" >> "$LOG"
}

# --- 1) Check Chat API ---
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:5000/api/status 2>/dev/null)

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 400 ] 2>/dev/null; then
    log "OK: Chat API responded with HTTP $HTTP_CODE"
else
    log "FAIL: Chat API unresponsive (HTTP $HTTP_CODE). Restarting services..."
    sudo systemctl restart petals-server
    PETALS_SERVER_EXIT=$?
    sudo systemctl restart petals-chat
    PETALS_CHAT_EXIT=$?
    if [ $PETALS_SERVER_EXIT -eq 0 ] && [ $PETALS_CHAT_EXIT -eq 0 ]; then
        log "RESTART: petals-server and petals-chat restarted successfully"
    else
        log "ERROR: Restart failed (petals-server=$PETALS_SERVER_EXIT, petals-chat=$PETALS_CHAT_EXIT)"
    fi
fi

# --- 2) Check IRC Bot ---
# The IRC bot runs as: /data/petals-env/bin/python3 /data/chat-ui/irc_bot.py
IRC_PID=$(pgrep -f "irc_bot\.py" 2>/dev/null)
if [ -n "$IRC_PID" ]; then
    log "OK: IRC bot is running (PID $IRC_PID)"
else
    log "WARN: IRC bot does not appear to be running"
fi

log "--- Health check complete ---"
