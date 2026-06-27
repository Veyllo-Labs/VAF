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
USE_UV=false

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

# Prefer uv: it provisions Python without sudo, so a bare machine needs nothing
# pre-installed. Install uv when neither a suitable Python nor uv is present.
if [[ -z "$PYTHON_CMD" ]] && ! command -v uv &> /dev/null; then
    print_warning "No suitable Python found - installing uv (provisions Python, no sudo)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null || print_warning "uv install failed"
    export PATH="$HOME/.local/bin:$PATH"
fi

if command -v uv &> /dev/null; then
    USE_UV=true
    print_success "Using uv to manage Python ($(command -v uv))"
elif [[ -n "$PYTHON_CMD" ]]; then
    print_success "Python $PYTHON_VERSION found ($PYTHON_CMD)"
else
    print_error "Python $MIN_PYTHON_VERSION or higher not found and uv could not be installed!"
    echo ""
    if [[ "$OS_TYPE" == "macos" ]]; then
        echo -e "  Install with: ${CYAN}brew install python@3.12${NC}  or  ${CYAN}curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
    else
        echo -e "  Install Python via your package manager, or: ${CYAN}curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
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
            DEPS="portaudio19-dev python3-dev python3-venv build-essential git ffmpeg python3-gi gir1.2-webkit2-4.0 gir1.2-ayatanaappindicator3-0.1 libgirepository1.0-dev libcairo2-dev"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        dnf|yum)
            DEPS="portaudio-devel python3-devel gcc git ffmpeg python3-gobject3 webkit2gtk4.0 libappindicator-gtk3 gobject-introspection-devel cairo-devel"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        pacman)
            DEPS="portaudio python git ffmpeg base-devel python-gobject webkit2gtk libappindicator-gtk3 gobject-introspection cairo"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        zypper)
            DEPS="portaudio-devel alsa-devel python3-devel gcc git ffmpeg nodejs-default npm-default docker-compose typelib-1_0-WebKit2-4_1 libwebkit2gtk-4_1-0 typelib-1_0-AyatanaAppIndicator3-0_1 gobject-introspection-devel cairo-devel"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        *)
            print_warning "Please manually install: portaudio, git, ffmpeg, python dev headers"
            ;;
    esac

    # (PyGObject is installed into the venv AFTER it is created — see the venv step.
    #  It used to run here, but the venv does not exist yet on a first install, so the
    #  guard silently skipped it and the GTK desktop window never worked.)
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
        print_warning "Docker not found - the Memory System (pgvector) needs a container runtime."
        print_info "VAF runs fine without Docker; long-term memory just stays off (enable it later)."
        if [[ "$OS_TYPE" == "macos" ]]; then
            print_info "To enable memory WITHOUT Docker Desktop (no license): ${CYAN}brew install colima docker && colima start${NC}"
        else
            print_info "To enable memory: install Docker Engine (free), e.g. your distro's ${CYAN}docker${NC} package, then ${CYAN}sudo systemctl enable --now docker${NC}"
        fi
        print_info "Then run: ${CYAN}docker compose -f docker-compose.memory.yml up -d${NC}"
        echo ""
        # No automatic Docker install on purpose: Docker Desktop is licensed for larger orgs
        # and heavy; Engine/Colima are the free path and a deliberate opt-in. If the user
        # already has ANY Docker the detection above uses it. Default install works without it.
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
    print_info "Node.js not found - downloading a portable Node (user-scoped, no sudo)..."
    # Fetched from the official nodejs.org dist (NOT bundled in the repo). Node core is MIT.
    NARCH=$(uname -m)
    case "$NARCH" in x86_64|amd64) NARCH=x64;; aarch64|arm64) NARCH=arm64;; esac
    if [[ "$OS_TYPE" == "macos" ]]; then NPLAT=darwin; NEXT=tar.gz; else NPLAT=linux; NEXT=tar.xz; fi
    NODE_BASE="https://nodejs.org/dist/latest-v22.x"
    NFILE=$(curl -fsSL "$NODE_BASE/SHASUMS256.txt" 2>/dev/null | grep -oE "node-v[0-9.]+-$NPLAT-$NARCH\.$NEXT" | head -1)
    if [[ -n "$NFILE" ]] && curl -fsSL "$NODE_BASE/$NFILE" -o "/tmp/$NFILE" 2>/dev/null; then
        NODE_DIR="$HOME/.vaf/node"
        rm -rf "$NODE_DIR" && mkdir -p "$NODE_DIR"
        tar -xf "/tmp/$NFILE" -C "$NODE_DIR" --strip-components=1 2>/dev/null
        export PATH="$NODE_DIR/bin:$PATH"
        if command -v node &> /dev/null; then
            NODE_INSTALLED=true
            print_success "Portable Node.js $(node --version) installed ($NODE_DIR)"
            # Persist for future launches (run_vaf.sh starts a fresh shell).
            for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
                if [[ -f "$rc" ]] && ! grep -q '.vaf/node/bin' "$rc"; then
                    echo 'export PATH="$HOME/.vaf/node/bin:$PATH"' >> "$rc"
                fi
            done
        fi
    fi
    if [[ "$NODE_INSTALLED" == "false" ]]; then
        print_warning "Portable Node download failed — the Web UI needs Node $MIN_NODE_VERSION+."
        if [[ "$OS_TYPE" == "macos" ]]; then
            echo -e "  Install with: ${CYAN}brew install node${NC}"
        else
            echo -e "  Install Node $MIN_NODE_VERSION+ via your package manager."
        fi
    fi
fi

# ============================================================================
# 6. VIRTUAL ENVIRONMENT
# ============================================================================
print_step "Setting up Python Virtual Environment..."

cd "$PROJECT_ROOT"

# Drop a Windows-style venv (Scripts/ instead of bin/) so it gets recreated for this OS.
if [[ -d "venv" && ! -f "venv/bin/activate" && -f "venv/Scripts/activate" ]]; then
    print_warning "Windows virtual environment detected – recreating for this OS..."
    rm -rf venv
fi

if [[ -d "venv/bin" ]]; then
    print_success "Virtual environment already exists"
elif [[ "$USE_UV" == "true" ]]; then
    # uv creates the venv (and downloads Python 3.12 if needed). --seed adds pip so the
    # `python3 -m pip install` steps below keep working inside a uv venv.
    uv venv venv --python 3.12 --seed
    print_success "Virtual environment created (uv, Python 3.12)"
else
    $PYTHON_CMD -m venv venv
    print_success "Virtual environment created"
fi

# Activate venv
source venv/bin/activate
print_info "Python: $(python3 --version)"

# PyGObject into the venv (Linux desktop window / pywebview GTK backend). Needs the
# gobject-introspection + cairo dev headers from the system-deps step. Done AFTER the venv
# exists — fixes the old ordering bug where it ran before venv creation and was silently skipped.
if [[ "$OS_TYPE" == "linux" ]]; then
    print_info "Installing PyGObject into venv (desktop window)..."
    pip install PyGObject 2>/dev/null || print_warning "PyGObject install failed — desktop window may not work"
fi

# ============================================================================
# 7. PYTHON DEPENDENCIES
# ============================================================================
print_step "Installing Python Dependencies..."

# Set compiler flags for audio libraries
if [[ "$OS_TYPE" == "macos" ]]; then
    export LDFLAGS="-L$(brew --prefix portaudio)/lib"
    export CFLAGS="-I$(brew --prefix portaudio)/include"
fi

# Don't let `pip install -e .` re-trigger setup.py's platform post-install (setup_mac.sh),
# which would redo brew/venv/alias/.app work install.sh already did (macOS double-path).
export VAF_SKIP_POSTINSTALL=1

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

# Create desktop entry (Linux) — works the same on Arch/Debian/Fedora (freedesktop std)
if [[ "$OS_TYPE" == "linux" ]]; then
    DESKTOP_FILE="$HOME/.local/share/applications/vaf.desktop"
    mkdir -p "$(dirname "$DESKTOP_FILE")"

    # Prefer the PNG icon (renders reliably across GNOME/KDE/XFCE); fall back to the .ico
    # if the PNG is missing (e.g. an older checkout). Many Linux DEs don't render .ico well.
    ICON_PATH="$PROJECT_ROOT/vaf/media/vaf_icon.png"
    [[ -f "$ICON_PATH" ]] || ICON_PATH="$PROJECT_ROOT/vaf/media/vaf_icon_v6.ico"

    cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=VAF
GenericName=Veyllo Agentic Framework
Comment=AI-powered local assistant
Exec=$PROJECT_ROOT/run_vaf.sh
Icon=$ICON_PATH
Terminal=false
Categories=Development;
Keywords=ai;assistant;llm;
StartupNotify=true
EOF

    chmod +x "$DESKTOP_FILE"

    # Refresh the application menu so the entry shows up immediately (no re-login).
    # update-desktop-database ships in desktop-file-utils on all of Arch/Debian/Fedora;
    # it's optional, so guard it and never fail the install if it's absent.
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
    fi
    print_success "Linux desktop entry created (icon: $(basename "$ICON_PATH"), no terminal window)"
fi

# ============================================================================
# 11. SERVER SETUP (Linux only)
# ============================================================================

SETUP_AUTOSTART=false
SETUP_LAN=false
INSTALL_MODE="desktop"

if [[ "$OS_TYPE" == "linux" ]] && [[ -t 0 ]]; then
    echo ""
    print_step "Installation Mode..."
    echo ""
    echo -e "  ${CYAN}[1] Desktop${NC}  — personal use, local only, system tray (default)"
    echo -e "  ${CYAN}[2] Server${NC}   — always-on service, LAN accessible via HTTPS, starts at boot"
    echo ""
    read -p "  Choose [1/2, default 1]: " _mode_response
    if [[ "$_mode_response" == "2" ]]; then
        INSTALL_MODE="server"
        SETUP_AUTOSTART=true
        SETUP_LAN=true
        print_success "Server mode selected"
    else
        INSTALL_MODE="desktop"
        print_success "Desktop mode selected"
    fi
fi

# --- Server mode: write config ---
if [[ "$SETUP_LAN" == "true" ]]; then
    print_info "Writing server mode config..."
    mkdir -p "$HOME/.vaf"
    INSTALL_MODE_VAR="$INSTALL_MODE"
    "$PROJECT_ROOT/venv/bin/python3" - << PYEOF
import json, os
p = os.path.expanduser("~/.vaf/config.json")
try:
    cfg = json.loads(open(p).read()) if os.path.exists(p) else {}
except Exception:
    cfg = {}
cfg["server_mode"] = True
cfg["local_network_enabled"] = True
cfg["local_network_tls_enabled"] = True
open(p, "w").write(json.dumps(cfg, indent=2))
PYEOF
    print_success "Server mode enabled in config"
    print_success "LAN access enabled (HTTPS, port 8443)"
    print_info "A self-signed TLS certificate is auto-generated on first start."
    print_warning "Browsers will show a certificate warning — expected for local networks."
fi

# --- Autostart: install systemd user service ---
if [[ "$SETUP_AUTOSTART" == "true" ]]; then
    if command -v systemctl &>/dev/null && systemctl --user daemon-reload &>/dev/null 2>&1; then
        print_info "Installing systemd user service..."

        SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_USER_DIR"

        # Write the unit file with current user's paths baked in
        cat > "$SYSTEMD_USER_DIR/vaf.service" << EOF
[Unit]
Description=VAF - Veyllo Agentic Framework
Documentation=https://github.com/Veyllo-Labs/VAF
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
Environment=PYTHONPATH=$PROJECT_ROOT
Environment=VAF_NATIVE_WRAPPER=1
ExecStart=$PROJECT_ROOT/venv/bin/python3 -m vaf.main tray
ExecStop=/bin/kill -s TERM \$MAINPID
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

StandardOutput=journal
StandardError=journal
SyslogIdentifier=vaf

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable vaf

        # Enable linger so the service starts at boot even with no active login session
        if sudo loginctl enable-linger "$USER" 2>/dev/null; then
            print_success "Boot autostart enabled (loginctl linger)"
        else
            print_warning "Could not enable linger (sudo required)"
            print_info "To enable boot start: sudo loginctl enable-linger $USER"
        fi

        # Start the service immediately
        if systemctl --user start vaf 2>/dev/null; then
            print_success "VAF service started"
        else
            print_warning "Service will start on next boot/login"
        fi

        print_success "Service installed: $SYSTEMD_USER_DIR/vaf.service"
        print_info "Manage: systemctl --user {start|stop|restart|status} vaf"
        print_info "Logs:   journalctl --user -u vaf -f"

    else
        print_warning "systemd user session not available — skipping autostart"
        print_info "Manual start: ./vaf.sh start"
    fi
fi

# ============================================================================
# 12. VERIFICATION
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
# pyttsx3 removed — caused 1-4GB RAM explosion on Windows via SAPI/comtypes.
# TTS is now handled by Docker (Piper). See docs/web-ui/SPEECH_FEATURES.md.
# verify_module "pyttsx3" "TTS Engine"
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

if [[ "$SETUP_AUTOSTART" == "true" ]]; then
    echo -e "  ${CYAN}Service (autostart enabled):${NC}"
    echo -e "    - Status:  systemctl --user status vaf"
    echo -e "    - Logs:    journalctl --user -u vaf -f"
    echo -e "    - Stop:    systemctl --user stop vaf"
    echo ""
fi

if [[ "$SETUP_LAN" == "true" ]]; then
    _LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || hostname -I 2>/dev/null | awk '{print $1}')
    echo -e "  ${CYAN}LAN Access (HTTPS):${NC}"
    echo -e "    - https://${_LAN_IP:-<your-ip>}:8443"
    echo -e "    - localhost: https://127.0.0.1:8443"
    echo -e "    - Accept the self-signed certificate warning on first visit."
    echo ""
fi

if [[ "$DOCKER_RUNNING" == "true" ]]; then
    echo -e "  ${CYAN}Memory System:${NC}"
    echo -e "    - Database: postgresql://localhost:5432/vaf_memory"
    echo -e "    - Stop: docker compose -f docker-compose.memory.yml down"
    echo ""
fi

echo -e "  ${CYAN}GPU Acceleration:${NC} $GPU_TYPE ($GPU_NAME)"
echo ""
echo -e "  ${NC}Documentation: https://github.com/Veyllo-Labs/VAF${NC}"
echo ""
