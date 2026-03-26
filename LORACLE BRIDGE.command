#!/usr/bin/env bash
# LORACLE BRIDGE — Double-click to launch
# macOS treats .command files as clickable terminal scripts

# cd to the directory where this script lives (the project root)
cd "$(dirname "$0")"

echo ""
echo "  Starting LORACLE BRIDGE..."
echo "  (This window will show logs. Close it to stop the bridge.)"
echo ""

# Launch the bridge
./mesh-llm.sh

# Keep window open if mesh-llm.sh exits with error
if [ $? -ne 0 ]; then
  echo ""
  echo "  Bridge exited with an error. Press any key to close."
  read -n 1
fi
