#!/bin/bash
# Install crontab entry to run cron-grow.sh every 15 minutes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Ensure results directory exists
mkdir -p "$PROJECT_DIR/results"

# Initialize state file if it doesn't exist
STATE_FILE="$SCRIPT_DIR/cron-state.json"
if [[ ! -f "$STATE_FILE" ]]; then
    cat > "$STATE_FILE" <<EOF
{
  "batch_num": 1,
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "total_objects_added": 0,
  "last_object_count": 0,
  "status": "running"
}
EOF
    echo "Initialized state file: $STATE_FILE"
fi

# Remove any existing cron-grow.sh entry
crontab -l 2>/dev/null | grep -v "cron-grow.sh" | crontab - 2>/dev/null || true

# Add new entry: every 15 minutes
(crontab -l 2>/dev/null; echo "*/15 * * * * cd $PROJECT_DIR && bash scripts/cron-grow.sh >> results/cron-stdout.log 2>&1") | crontab -

echo "Cron installed. Verify with: crontab -l"
echo ""
echo "  Logs:     results/cron-stdout.log"
echo "  Tracking: results/cron-log.json"
echo "  State:    scripts/cron-state.json"
echo "  Status:   bash scripts/cron-status.sh"
echo "  Stop:     bash scripts/stop-cron.sh"
