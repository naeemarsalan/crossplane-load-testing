#!/bin/bash
# Remove the cron-grow.sh crontab entry and mark state as paused.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/cron-state.json"

# Remove cron entry
crontab -l 2>/dev/null | grep -v "cron-grow.sh" | crontab - 2>/dev/null || true

echo "Cron entry removed."

# Update state to paused if currently running
if [[ -f "$STATE_FILE" ]]; then
    CURRENT_STATUS=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['status'])" 2>/dev/null || echo "unknown")
    if [[ "$CURRENT_STATUS" == "running" ]]; then
        python3 -c "
import json
with open('$STATE_FILE', 'r') as f:
    state = json.load(f)
state['status'] = 'paused'
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
        echo "State updated to 'paused'."
    fi
fi

echo "Done. To resume: bash scripts/install-cron.sh"
