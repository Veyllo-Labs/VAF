#!/bin/bash
#
# VAF - Veyllo Agentic Framework - Cross-Platform Installer
# Supports: macOS (Intel/Apple Silicon) and Linux (Debian/Ubuntu/Fedora/Arch)
#
# Usage:
#   ./install.sh                 # Full installation
#   ./install.sh --skip-docker   # Skip Docker setup
#   ./install.sh --help          # Show help
#
# Requirements:
#   - Python 3.10+
#   - Internet connection
#   - Docker (optional, for Memory System)
#

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================
MIN_PYTHON_VERSION="3.10"
MIN_NODE_VERSION="18"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Flags
SKIP_DOCKER=false
VERBOSE=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Functions
print_step() { echo -e "\n${CYAN}ÃƒÂ¢Ã¢â‚¬â€œÃ‚Â¶ $1${NC}"; }
print_success() { echo -e "  ${GREEN}ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ $1${NC}"; }
print_warning() { echo -e "  ${YELLOW}ÃƒÂ¢Ã…Â¡Ã‚Â ÃƒÂ¯Ã‚Â¸Ã‚Â  $1${NC}"; }
print_error() { echo -e "  ${RED}ÃƒÂ¢Ã‚ÂÃ…â€™ $1${NC}"; }
print_info() { echo -e "  ${NC}ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¹ÃƒÂ¯Ã‚Â¸Ã‚Â  $1${NC}"; }

# ============================================================================
# ARGUMENT PARSING
# ============================================================================
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-docker)
            SKIP_DOCKER=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            echo "VAF Installer - Cross-Platform Setup Script"
            echo ""
            echo "Usage: ./install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --skip-docker    Skip Docker installation/setup"
            echo "  --verbose, -v    Show verbose output"
            echo "  --help, -h       Show this help message"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# BANNER
# ============================================================================
echo -e "${MAGENTA}"
cat << 'EOF'

ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ                                                                   ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€    ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€    ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â    ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€  ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â    ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€      ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ       ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â      ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬â€ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ       ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ    ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ  ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ         ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬ËœÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬â€œÃ‹â€ ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ       ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ     ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â  ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â  ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â         ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â  ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â   ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â       ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ                                                                   ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   Veyllo Agentic Framework - Cross-Platform Installer             ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ   Python + FastAPI + Next.js + pgvector + Local LLM              ÃƒÂ¢Ã¢â‚¬Â¢Ã¢â‚¬Ëœ
ÃƒÂ¢Ã¢â‚¬Â¢Ã…Â¡ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â

EOF
echo -e "${NC}"

# ============================================================================
# SYSTEM DETECTION
# ============================================================================
print_step "Detecting System Configuration..."

OS_TYPE=""
OS_NAME=""
PKG_MANAGER=""
INSTALL_CMD=""
ARCH=$(uname -m)

if [[ "$OSTYPE" == "darwin"* ]]; then
    OS_TYPE="macos"
    OS_NAME="macOS"
    
    # Check for Apple Silicon
    if [[ "$ARCH" == "arm64" ]]; then
        print_info "macOS (Apple Silicon - $ARCH)"
    else
        print_info "macOS (Intel - $ARCH)"
    fi
    
    # Check for Homebrew
    if command -v brew &> /dev/null; then
        PKG_MANAGER="brew"
        INSTALL_CMD="brew install"
        print_success "Homebrew detected"
    else
        print_warning "Homebrew not found"
        print_info "Install from: https://brew.sh"
        echo ""
        echo -e "  Run: ${CYAN}/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${NC}"
        echo ""
        read -p "  Install Homebrew now? (Y/n) " response
        if [[ "$response" != "n" && "$response" != "N" ]]; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            PKG_MANAGER="brew"
            INSTALL_CMD="brew install"
        else
            print_error "Homebrew is required for macOS installation"
            exit 1
        fi
    fi

elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS_TYPE="linux"
    
    # Detect Linux distribution
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_NAME="$NAME"
        
        case "$ID" in
            ubuntu|debian|pop|linuxmint)
                PKG_MANAGER="apt"
                INSTALL_CMD="sudo apt-get install -y"
                ;;
            fedora|rhel|centos|rocky|almalinux)
                PKG_MANAGER="dnf"
                INSTALL_CMD="sudo dnf install -y"
                if ! command -v dnf &> /dev/null; then
                    PKG_MANAGER="yum"
                    INSTALL_CMD="sudo yum install -y"
                fi
                ;;
            arch|manjaro|endeavouros)
                PKG_MANAGER="pacman"
                INSTALL_CMD="sudo pacman -S --noconfirm"
                ;;
            opensuse*)
                PKG_MANAGER="zypper"
                INSTALL_CMD="sudo zypper install -y"
                ;;
            *)
                print_warning "Unknown Linux distribution: $ID"
                PKG_MANAGER="unknown"
                ;;
        esac
    else
        OS_NAME="Linux"
        print_warning "Could not detect Linux distribution"
    fi
    
    print_info "$OS_NAME ($ARCH)"
    if [[ "$PKG_MANAGER" != "unknown" ]]; then
        print_success "Package manager: $PKG_MANAGER"
    fi
else
    print_error "Unsupported operating system: $OSTYPE"
    exit 1
fi

# ============================================================================
# 1. PYTHON CHECK
# ============================================================================
print_step "Checking Python Installation..."

PYTHON_CMD=""
PYTHON_VERSION=""

# Try python3 first, then python
for cmd in python3 python; do
    if command -v $cmd &> /dev/null; then
        version=$($cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        if [[ $(echo "$version >= $MIN_PYTHON_VERSION" | bc -l 2>/dev/null || python3 -c "print(1 if $version >= $MIN_PYTHON_VERSION else 0)") == 1 ]]; then
            PYTHON_CMD=$cmd
            PYTHON_VERSION=$version
            break
        fi
    fi
done

if [[ -n "$PYTHON_CMD" ]]; then
    print_success "Python $PYTHON_VERSION found ($PYTHON_CMD)"
else
    print_error "Python $MIN_PYTHON_VERSION or higher not found!"
    echo ""
    if [[ "$OS_TYPE" == "macos" ]]; then
        echo -e "  Install with: ${CYAN}brew install python@3.12${NC}"
    elif [[ "$PKG_MANAGER" == "apt" ]]; then
        echo -e "  Install with: ${CYAN}sudo apt-get install python3 python3-pip python3-venv${NC}"
    elif [[ "$PKG_MANAGER" == "dnf" || "$PKG_MANAGER" == "yum" ]]; then
        echo -e "  Install with: ${CYAN}sudo dnf install python3 python3-pip${NC}"
    elif [[ "$PKG_MANAGER" == "pacman" ]]; then
        echo -e "  Install with: ${CYAN}sudo pacman -S python python-pip${NC}"
    fi
    exit 1
fi

# ============================================================================
# 2. SYSTEM DEPENDENCIES
# ============================================================================
print_step "Installing System Dependencies..."

if [[ "$OS_TYPE" == "macos" ]]; then
    # macOS dependencies via Homebrew
    DEPS="portaudio git ffmpeg"
    print_info "Installing: $DEPS"
    brew install $DEPS 2>/dev/null || print_warning "Some packages may already be installed"
    
elif [[ "$OS_TYPE" == "linux" ]]; then
    case "$PKG_MANAGER" in
        apt)
            print_info "Updating package lists..."
            sudo apt-get update -qq
            DEPS="portaudio19-dev python3-dev python3-venv build-essential git ffmpeg"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        dnf|yum)
            DEPS="portaudio-devel python3-devel gcc git ffmpeg"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        pacman)
            DEPS="portaudio python git ffmpeg base-devel"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        zypper)
            DEPS="portaudio-devel alsa-devel python3-devel gcc git ffmpeg nodejs-default npm-default docker-compose"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        *)
            print_warning "Please manually install: portaudio, git, ffmpeg, python dev headers"
            ;;
    esac
fi

print_success "System dependencies installed"

# ============================================================================
# 3. GPU DETECTION
# ============================================================================
print_step "Detecting GPU for LLM Acceleration..."

GPU_TYPE="cpu"
GPU_NAME="None"

if [[ "$OS_TYPE" == "macos" ]]; then
    # Check for Apple Silicon (Metal)
    if [[ "$ARCH" == "arm64" ]]; then
        GPU_TYPE="metal"
        GPU_NAME="Apple Silicon (Metal)"
        print_success "Apple Silicon detected - Metal GPU acceleration available"
    else
        print_info "Intel Mac - CPU mode (Metal not available)"
    fi
else
    # Linux GPU detection
    if command -v nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
        if [[ -n "$GPU_NAME" ]]; then
            GPU_TYPE="cuda"
            print_success "NVIDIA GPU detected: $GPU_NAME"
            print_success "CUDA acceleration available"
        fi
    elif command -v rocm-smi &> /dev/null; then
        GPU_TYPE="rocm"
        GPU_NAME="AMD (ROCm)"
        print_success "AMD GPU with ROCm detected"
    elif [[ -d /sys/class/drm/card0 ]]; then
        GPU_NAME=$(cat /sys/class/drm/card0/device/uevent 2>/dev/null | grep -oP 'DRIVER=\K.*' || echo "Unknown")
        print_info "GPU detected: $GPU_NAME (may support Vulkan)"
    else
        print_info "No dedicated GPU detected - will use CPU for LLM"
    fi
fi

# ============================================================================
# 4. DOCKER DETECTION
# ============================================================================
print_step "Checking Docker Installation (for Memory System)..."

DOCKER_INSTALLED=false
DOCKER_RUNNING=false
DOCKER_COMPOSE=false

if [[ "$SKIP_DOCKER" == "false" ]]; then
    if command -v docker &> /dev/null; then
        DOCKER_INSTALLED=true
        DOCKER_VERSION=$(docker --version | grep -oE '[0-9]+\.[0-9]+' | head -1)
        print_success "Docker $DOCKER_VERSION installed"
        
        # Check if Docker daemon is running
        if /usr/bin/docker info &> /dev/null; then
            DOCKER_RUNNING=true
            print_success "Docker daemon is running"
        else
            print_warning "Docker is installed but not running"
            if [[ "$OS_TYPE" == "macos" ]]; then
                print_info "Please start Docker Desktop"
            else
                print_info "Start with: sudo systemctl start docker"
            fi
        fi
        
        # Check Docker Compose
        if /usr/bin/docker compose version &> /dev/null || docker-compose --version &> /dev/null; then
            DOCKER_COMPOSE=true
            print_success "Docker Compose available"
        fi
    else
        print_warning "Docker not found"
        echo ""
        echo -e "  Docker is required for the Memory System (pgvector database)."
        echo ""
        
        # Attempt automatic Docker installation
        DOCKER_INSTALL_SUCCESS=false
        
        if [[ "$OS_TYPE" == "macos" ]]; then
            if [[ "$PKG_MANAGER" == "brew" ]]; then
                print_info "Attempting to install Docker Desktop via Homebrew..."
                echo -e "  (This may take several minutes)"
                echo ""
                
                if brew install --cask docker 2>&1; then
                    DOCKER_INSTALL_SUCCESS=true
                    print_success "Docker Desktop installed!"
                    echo ""
                    echo -e "  ${YELLOW}============================================================${NC}"
                    echo -e "  ${YELLOW}IMPORTANT: Please start Docker Desktop from Applications${NC}"
                    echo -e "  ${WHITE}After Docker starts, run the installer again:${NC}"
                    echo -e "  ${CYAN}./install.sh${NC}"
                    echo -e "  ${YELLOW}============================================================${NC}"
                    echo ""
                else
                    print_warning "Automatic installation failed"
                    echo -e "  Install manually: ${CYAN}brew install --cask docker${NC}"
                fi
            else
                echo -e "  Install Homebrew first, then: ${CYAN}brew install --cask docker${NC}"
                echo -e "  Or download from: https://www.docker.com/products/docker-desktop/"
            fi
            
        elif [[ "$PKG_MANAGER" == "apt" ]]; then
            print_info "Attempting to install Docker via official script..."
            echo -e "  (This requires sudo and may take a few minutes)"
            echo ""
            
            if curl -fsSL https://get.docker.com | sh 2>&1; then
                DOCKER_INSTALL_SUCCESS=true
                print_success "Docker installed!"
                
                # Add user to docker group
                if sudo usermod -aG docker "$USER" 2>/dev/null; then
                    print_success "User added to docker group"
                fi
                
                # Start Docker service
                if sudo systemctl start docker 2>/dev/null && sudo systemctl enable docker 2>/dev/null; then
                    print_success "Docker service started"
                    DOCKER_INSTALLED=true
                    DOCKER_RUNNING=true
                    DOCKER_COMPOSE=true
                fi
                
                echo ""
                echo -e "  ${YELLOW}============================================================${NC}"
                echo -e "  ${YELLOW}IMPORTANT: You may need to log out and back in${NC}"
                echo -e "  ${WHITE}for docker group permissions to take effect.${NC}"
                echo -e "  ${WHITE}Then run the installer again:${NC}"
                echo -e "  ${CYAN}./install.sh${NC}"
                echo -e "  ${YELLOW}============================================================${NC}"
                echo ""
            else
                print_warning "Automatic installation failed"
                echo -e "  Install manually: ${CYAN}curl -fsSL https://get.docker.com | sh${NC}"
            fi
            
        elif [[ "$PKG_MANAGER" == "dnf" ]]; then
            print_info "Attempting to install Docker via dnf..."
            
            if sudo dnf install -y docker docker-compose 2>&1; then
                DOCKER_INSTALL_SUCCESS=true
                sudo systemctl start docker 2>/dev/null
                sudo systemctl enable docker 2>/dev/null
                sudo usermod -aG docker "$USER" 2>/dev/null
                print_success "Docker installed!"
                DOCKER_INSTALLED=true
                DOCKER_RUNNING=true
                DOCKER_COMPOSE=true
            else
                print_warning "Automatic installation failed"
                echo -e "  Install manually: ${CYAN}sudo dnf install docker docker-compose${NC}"
            fi
            
        elif [[ "$PKG_MANAGER" == "pacman" ]]; then
            print_info "Attempting to install Docker via pacman..."

            if sudo pacman -S --noconfirm docker docker-compose 2>&1; then
                DOCKER_INSTALL_SUCCESS=true
                sudo systemctl start docker 2>/dev/null
                sudo systemctl enable docker 2>/dev/null
                sudo usermod -aG docker "$USER" 2>/dev/null
                print_success "Docker installed!"
                DOCKER_INSTALLED=true
                DOCKER_RUNNING=true
                DOCKER_COMPOSE=true
            else
                print_warning "Automatic installation failed"
                echo -e "  Install manually: ${CYAN}sudo pacman -S docker docker-compose${NC}"
            fi

        elif [[ "$PKG_MANAGER" == "zypper" ]]; then
            print_info "Attempting to install Docker via zypper..."

            if sudo zypper install -y docker docker-compose 2>&1; then
                DOCKER_INSTALL_SUCCESS=true
                sudo systemctl enable --now docker 2>/dev/null
                sudo usermod -aG docker "$USER" 2>/dev/null
                print_success "Docker installed!"
                print_warning "Log out and back in (or run 'newgrp docker') for group permissions to take effect"
                DOCKER_INSTALLED=true
                DOCKER_RUNNING=true
                DOCKER_COMPOSE=true
            else
                print_warning "Automatic installation failed"
                echo -e "  Install manually: ${CYAN}sudo zypper install docker docker-compose${NC}"
            fi
        fi
        
        if [[ "$DOCKER_INSTALL_SUCCESS" != "true" ]]; then
            echo ""
            print_warning "Continuing installation - Memory System will be unavailable until Docker is installed"
            echo -e "  After installing Docker, run the installer again: ${CYAN}./install.sh${NC}"
            echo ""
        fi
    fi
else
    print_info "Docker check skipped (--skip-docker flag)"
fi

# ============================================================================
# 5. NODE.JS CHECK
# ============================================================================
print_step "Checking Node.js Installation (for Web UI)..."

NODE_INSTALLED=false

if command -v node &> /dev/null; then
    NODE_VERSION=$(node --version | grep -oE '[0-9]+' | head -1)
    if [[ "$NODE_VERSION" -ge "$MIN_NODE_VERSION" ]]; then
        NODE_INSTALLED=true
        print_success "Node.js v$NODE_VERSION installed"
    else
        print_warning "Node.js v$NODE_VERSION is outdated (need v$MIN_NODE_VERSION+)"
    fi
else
    print_warning "Node.js not found"
fi

if [[ "$NODE_INSTALLED" == "false" ]]; then
    if [[ "$OS_TYPE" == "macos" ]]; then
        echo -e "  Install with: ${CYAN}brew install node${NC}"
    elif [[ "$PKG_MANAGER" == "apt" ]]; then
        echo -e "  Install with: ${CYAN}curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs${NC}"
    elif [[ "$PKG_MANAGER" == "dnf" || "$PKG_MANAGER" == "yum" ]]; then
        echo -e "  Install with: ${CYAN}sudo dnf install nodejs npm${NC}"
    elif [[ "$PKG_MANAGER" == "zypper" ]]; then
        echo -e "  Install with: ${CYAN}sudo zypper install nodejs-default npm-default${NC}"
    elif [[ "$PKG_MANAGER" == "pacman" ]]; then
        echo -e "  Install with: ${CYAN}sudo pacman -S nodejs npm${NC}"
    fi
    print_warning "Web UI will not be available without Node.js"
fi

# ============================================================================
# 6. VIRTUAL ENVIRONMENT
# ============================================================================
print_step "Setting up Python Virtual Environment..."

cd "$PROJECT_ROOT"

if [[ -d "venv" ]]; then
    # Detect Windows venv (has Scripts/ instead of bin/)
    if [[ ! -f "venv/bin/activate" && -f "venv/Scripts/activate" ]]; then
        print_warning "Windows virtual environment detected – recreating for Linux..."
        rm -rf venv
        $PYTHON_CMD -m venv venv
        print_success "Virtual environment recreated (Linux-compatible)"
    else
        print_success "Virtual environment already exists"
        read -p "  Recreate virtual environment? (y/N) " response
        if [[ "$response" == "y" || "$response" == "Y" ]]; then
            rm -rf venv
            $PYTHON_CMD -m venv venv
            print_success "Virtual environment recreated"
        fi
    fi
else
    $PYTHON_CMD -m venv venv
    print_success "Virtual environment created"
fi

# Activate venv
source venv/bin/activate
print_info "Python: $(python3 --version)"

# ============================================================================
# 7. PYTHON DEPENDENCIES
# ============================================================================
print_step "Installing Python Dependencies..."

# Set compiler flags for audio libraries
if [[ "$OS_TYPE" == "macos" ]]; then
    export LDFLAGS="-L$(brew --prefix portaudio)/lib"
    export CFLAGS="-I$(brew --prefix portaudio)/include"
fi

# Upgrade pip
print_info "Upgrading pip..."
python3 -m pip install --upgrade pip --quiet

# Install core dependencies
print_info "Installing core dependencies..."
python3 -m pip install -e . --quiet 2>/dev/null || python3 -m pip install -e .

# Install all requirements
print_info "Installing all requirements (this may take a few minutes)..."
python3 -m pip install -r requirements.txt --quiet 2>/dev/null || {
    print_warning "Some optional dependencies failed - core functionality should work"
}

print_success "Python dependencies installed"

# ============================================================================
# 8. WEB UI SETUP
# ============================================================================
if [[ "$NODE_INSTALLED" == "true" ]]; then
    print_step "Setting up Web UI (Next.js)..."
    
    if [[ -d "web" ]]; then
        cd web
        print_info "Installing/updating npm packages (Web UI dependencies from web/package.json)..."
        npm install --silent 2>/dev/null || npm install
        print_success "Web UI dependencies installed"
        cd "$PROJECT_ROOT"
    fi
fi

# ============================================================================
# 9. DOCKER SETUP (Memory System) – Smart Update
# ============================================================================
COMPOSE_FILE="docker-compose.memory.yml"
COMPOSE_CHANGED=false

# Check if docker-compose.memory.yml changed in the latest commit
if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q "$COMPOSE_FILE"; then
    COMPOSE_CHANGED=true
    print_info "docker-compose.memory.yml changed – will update Docker stack"
elif ! docker ps 2>/dev/null | grep -q "vaf-memory-db"; then
    # Stack not running at all – treat as needing startup
    COMPOSE_CHANGED=true
fi

if [[ "$DOCKER_INSTALLED" == "true" ]]; then
    print_step "Setting up Memory System Docker Stack..."

    # Auto-start Docker if compose changed but daemon is not running
    if [[ "$DOCKER_RUNNING" != "true" && "$COMPOSE_CHANGED" == "true" ]]; then
        print_warning "Docker not running – attempting to start automatically..."
        if [[ "$OS_TYPE" == "macos" ]]; then
            open -a Docker 2>/dev/null || true
        elif [[ "$OS_TYPE" == "linux" ]]; then
            sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true
        fi

        # Wait up to 60 seconds for Docker daemon to become ready
        for i in $(seq 1 12); do
            sleep 5
            if /usr/bin/docker info &>/dev/null; then
                DOCKER_RUNNING=true
                DOCKER_COMPOSE=true
                print_success "Docker daemon is now running"
                break
            fi
            printf "  ⏳ Waiting for Docker daemon... %ds/60s\n" $((i*5))
        done

        if [[ "$DOCKER_RUNNING" != "true" ]]; then
            print_warning "Docker did not start in time. Please start Docker manually."
            print_info "Then run: docker compose -f docker-compose.memory.yml up -d"
        fi
    fi

    if [[ "$DOCKER_RUNNING" == "true" && "$DOCKER_COMPOSE" == "true" ]]; then
        if [[ -f "$COMPOSE_FILE" ]]; then
            print_info "Running: docker compose up -d (adds new services, updates existing ones)..."
            /usr/bin/docker compose -f "$COMPOSE_FILE" up -d 2>/dev/null || docker-compose -f "$COMPOSE_FILE" up -d

            sleep 2
            if /usr/bin/docker ps | grep -q "vaf-memory-db"; then
                print_success "Docker stack is running"
                print_info "Database: postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory"
            else
                print_warning "Containers may still be starting – check with: docker ps"
            fi
        fi
    elif [[ "$COMPOSE_CHANGED" == "true" && "$DOCKER_RUNNING" != "true" ]]; then
        print_warning "Docker stack has changes but Docker is offline."
        print_info "Start Docker, then run: docker compose -f docker-compose.memory.yml up -d"
    fi
elif [[ "$DOCKER_INSTALLED" != "true" ]]; then
    print_info "Docker not installed – skipping stack setup"
fi

# ============================================================================
# 10. CREATE SHORTCUTS/ALIASES
# ============================================================================
print_step "Creating Shortcuts..."

# Make run script executable
chmod +x run_vaf.sh 2>/dev/null
chmod +x start_vaf.sh 2>/dev/null

# Add shell alias
if [[ "$OS_TYPE" == "macos" ]]; then
    SHELL_CONFIG="$HOME/.zshrc"
else
    SHELL_CONFIG="$HOME/.bashrc"
    [[ -f "$HOME/.zshrc" ]] && SHELL_CONFIG="$HOME/.zshrc"
fi

RUN_SCRIPT="$PROJECT_ROOT/run_vaf.sh"

if grep -q "alias vaf=" "$SHELL_CONFIG" 2>/dev/null; then
    sed -i.bak "s|alias vaf=.*|alias vaf='$RUN_SCRIPT'|" "$SHELL_CONFIG"
    print_success "Shell alias updated in $SHELL_CONFIG"
else
    echo "" >> "$SHELL_CONFIG"
    echo "# VAF - Veyllo Agentic Framework" >> "$SHELL_CONFIG"
    echo "alias vaf='$RUN_SCRIPT'" >> "$SHELL_CONFIG"
    print_success "Shell alias added to $SHELL_CONFIG"
fi

# Create application bundle (macOS)
if [[ "$OS_TYPE" == "macos" ]]; then
    print_info "Creating macOS application bundle..."
    python3 scripts/create_app_shortcut.py 2>/dev/null || print_warning "Could not create app bundle"
fi

# Create desktop entry (Linux)
if [[ "$OS_TYPE" == "linux" ]]; then
    DESKTOP_FILE="$HOME/.local/share/applications/vaf.desktop"
    mkdir -p "$(dirname "$DESKTOP_FILE")"
    
    cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=VAF
GenericName=Veyllo Agentic Framework
Comment=AI-powered local assistant
Exec=$PROJECT_ROOT/run_vaf.sh
Icon=$PROJECT_ROOT/vaf/media/vaf_icon_v6.ico
Terminal=true
Categories=Development;Utility;
Keywords=ai;assistant;llm;
EOF
    
    chmod +x "$DESKTOP_FILE"
    print_success "Linux desktop entry created"
fi

# ============================================================================
# 11. VERIFICATION
# ============================================================================
print_step "Verifying Installation..."

# Activate venv for verification
source "$PROJECT_ROOT/venv/bin/activate"

verify_module() {
    if python3 -c "import $1" 2>/dev/null; then
        print_success "$2"
        return 0
    else
        print_warning "$2 - not available"
        return 1
    fi
}

verify_module "vaf" "VAF Module"
verify_module "fastapi" "FastAPI"
verify_module "pyttsx3" "TTS Engine"
verify_module "speech_recognition" "Speech Recognition"

# ============================================================================
# SUMMARY
# ============================================================================
echo ""
echo -e "${GREEN}ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â${NC}"
echo -e "${GREEN}                    ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ INSTALLATION COMPLETE!                      ${NC}"
echo -e "${GREEN}ÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚ÂÃƒÂ¢Ã¢â‚¬Â¢Ã‚Â${NC}"
echo ""

echo -e "  ${CYAN}Quick Start:${NC}"
echo -e "    ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¢ Restart your terminal (or run: source $SHELL_CONFIG)"
echo -e "    ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¢ Then just type: ${CYAN}vaf${NC}"
echo -e "    ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¢ Or run: ${CYAN}./run_vaf.sh${NC}"
echo ""

if [[ "$DOCKER_RUNNING" == "true" ]]; then
    echo -e "  ${CYAN}Memory System:${NC}"
    echo -e "    ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¢ Database: postgresql://localhost:5432/vaf_memory"
    echo -e "    ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¢ Stop: docker compose -f docker-compose.memory.yml down"
    echo ""
fi

echo -e "  ${CYAN}GPU Acceleration:${NC} $GPU_TYPE ($GPU_NAME)"
echo ""
echo -e "  ${NC}Documentation: https://github.com/Veyllo/VAF${NC}"
echo ""
