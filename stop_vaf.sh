#!/bin/bash
# VAF Cleanup Script - Kill all VAF processes and stop Docker services
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "🛑 Stopping all VAF processes..."

# Kill all Node.js processes in VAF directory
pkill -9 -f "node.*VAF" 2>/dev/null
echo "   ✓ Node.js processes killed"

# Kill all Python VAF processes  
pkill -9 -f "python.*vaf" 2>/dev/null
echo "   ✓ Python processes killed"

# Kill any remaining processes on VAF ports
lsof -ti:3000 2>/dev/null | xargs kill -9 2>/dev/null
lsof -ti:3001 2>/dev/null | xargs kill -9 2>/dev/null
lsof -ti:8001 2>/dev/null | xargs kill -9 2>/dev/null
echo "   ✓ Port processes killed"

sleep 1

# Stop Docker services
if command -v docker &>/dev/null && [ -f "$DIR/docker-compose.memory.yml" ]; then
    echo "Stopping Docker services..."
    docker compose -f "$DIR/docker-compose.memory.yml" stop 2>/dev/null \
        && echo "   ✓ Docker services stopped" \
        || echo "   ⚠ Could not stop Docker services"
fi

# Verify cleanup
REMAINING=$(ps aux | grep -E "node.*VAF|python.*vaf" | grep -v grep | wc -l)
if [ "$REMAINING" -eq 0 ]; then
    echo "✅ All VAF processes stopped successfully!"
else
    echo "⚠️  Warning: $REMAINING processes still running"
    ps aux | grep -E "node.*VAF|python.*vaf" | grep -v grep
fi
