"""
VAF Network Firewall - Cross-Platform Firewall Rules

Creates OS-level firewall rules to ensure VAF is only accessible from local network.
Supports Windows (netsh), macOS (pf), and Linux (iptables/ufw).

SECURITY: This is Layer 2 of the three-layer defense against internet exposure.
"""

import os
import shlex
import subprocess
import logging
import tempfile
import atexit
from pathlib import Path
from typing import Optional

from vaf.core.platform import Platform

logger = logging.getLogger(__name__)

# Windows: avoid flashing CMD windows when run from pythonw/tray
_WIN_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Skip further netsh attempts in this process after first failure (avoids repeated 0xc0000142 dialogs)
_windows_firewall_skip: bool = False

# Rule/anchor names for identification
FIREWALL_RULE_NAME = "VAF-LocalNetwork"

# RFC 1918 Private IP ranges
PRIVATE_CIDRS = [
    "192.168.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
]


def setup_firewall(port: int, port_frontend: int = 3000) -> bool:
    """
    Setup OS firewall rules for LAN-only access.
    
    Creates rules that:
    - Allow connections from RFC 1918 private IP ranges
    - Allow localhost connections
    - Block all other incoming connections on the specified ports
    
    Args:
        port: Backend port (default 8001)
        port_frontend: Frontend port (default 3000)
        
    Returns:
        True if firewall rules were successfully created
    """
    try:
        if Platform.is_windows():
            return _setup_firewall_windows(port, port_frontend)
        elif Platform.is_macos():
            return _setup_firewall_macos(port, port_frontend)
        elif Platform.is_linux():
            return _setup_firewall_linux(port, port_frontend)
        else:
            logger.warning(f"Unsupported platform for firewall: {Platform.current()}")
            return False
    except Exception as e:
        logger.error(f"Failed to setup firewall: {e}")
        return False


def cleanup_firewall() -> bool:
    """
    Remove VAF firewall rules.
    
    Should be called when:
    - Local Network mode is disabled
    - Application exits
    
    Returns:
        True if cleanup was successful
    """
    try:
        if Platform.is_windows():
            return _cleanup_firewall_windows()
        elif Platform.is_macos():
            return _cleanup_firewall_macos()
        elif Platform.is_linux():
            return _cleanup_firewall_linux()
        else:
            return False
    except Exception as e:
        logger.error(f"Failed to cleanup firewall: {e}")
        return False


def is_firewall_configured() -> bool:
    """
    Check if VAF firewall rules are currently active.
    
    Returns:
        True if firewall rules exist
    """
    try:
        if Platform.is_windows():
            result = subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={FIREWALL_RULE_NAME}'],
                capture_output=True,
                text=True,
                creationflags=_WIN_CREATE_NO_WINDOW,
            )
            return result.returncode == 0 and FIREWALL_RULE_NAME in result.stdout
        elif Platform.is_macos():
            anchor_path = Path("/etc/pf.anchors/vaf")
            return anchor_path.exists()
        elif Platform.is_linux():
            result = subprocess.run(
                ['iptables', '-L', 'INPUT', '-n', '--line-numbers'],
                capture_output=True,
                text=True
            )
            return 'VAF' in result.stdout or 'vaf' in result.stdout.lower()
        return False
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# WINDOWS IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_firewall_windows(port: int, port_frontend: int) -> bool:
    """
    Create Windows Firewall rules for LAN-only access.
    
    Uses netsh advfirewall to create inbound rules.
    """
    logger.info("Setting up Windows Firewall rules for local network access")
    
    # First, remove any existing rules
    _cleanup_firewall_windows()
    
    # Combine private ranges with comma separator
    private_ranges = ",".join(PRIVATE_CIDRS)
    
    ports = [port, port_frontend]
    
    for p in ports:
        # Create allow rule for private IPs
        allow_cmd = [
            'netsh', 'advfirewall', 'firewall', 'add', 'rule',
            f'name={FIREWALL_RULE_NAME}-Allow-{p}',
            'dir=in',
            'action=allow',
            f'localport={p}',
            'protocol=tcp',
            f'remoteip={private_ranges},127.0.0.1'
        ]
        
        try:
            result = subprocess.run(
                allow_cmd, capture_output=True, text=True, creationflags=_WIN_CREATE_NO_WINDOW
            )
        except Exception as e:
            logger.error(f"Failed to run netsh (firewall): {e}")
            _windows_firewall_skip = True
            return False
        if result.returncode != 0:
            err_detail = (result.stderr or result.stdout or "").strip()
            logger.error(f"Failed to create allow rule on port {p}: {err_detail}")
            _windows_firewall_skip = True
            return False
    logger.info(f"Windows Firewall allow rules created for ports {ports}")
    return True


def _cleanup_firewall_windows() -> bool:
    """Remove Windows Firewall rules."""
    logger.info("Cleaning up Windows Firewall rules")
    
    # Delete known rule names with our prefix (legacy + current ports)
    for rule_type in ['Allow', 'Block']:
        for port in [443, 8443, 8001, 8005, 3000]:
            subprocess.run(
                ['netsh', 'advfirewall', 'firewall', 'delete', 'rule',
                 f'name={FIREWALL_RULE_NAME}-{rule_type}-{port}'],
                capture_output=True,
                creationflags=_WIN_CREATE_NO_WINDOW,
            )
    
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# MACOS IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_firewall_macos(port: int, port_frontend: int) -> bool:
    """
    Create macOS pf firewall rules for LAN-only access.
    
    Uses pf (packet filter) via pfctl.
    Note: Requires root privileges to modify pf rules.
    """
    logger.info("Setting up macOS pf rules for local network access")
    
    # Build pf rules
    rules = f"""# VAF Local Network Rules - Auto-generated
# Allow localhost
pass in quick on lo0 proto tcp to any port {{{port}, {port_frontend}}}

# Allow private networks (RFC 1918)
pass in quick proto tcp from 192.168.0.0/16 to any port {{{port}, {port_frontend}}}
pass in quick proto tcp from 10.0.0.0/8 to any port {{{port}, {port_frontend}}}
pass in quick proto tcp from 172.16.0.0/12 to any port {{{port}, {port_frontend}}}

# Block everything else on these ports
block in quick proto tcp to any port {{{port}, {port_frontend}}}
"""
    
    try:
        # Write anchor file
        anchor_path = Path("/etc/pf.anchors/vaf")
        
        # Need to use sudo for /etc
        with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
            f.write(rules)
            temp_path = f.name
        
        # Copy to /etc/pf.anchors (requires sudo)
        result = subprocess.run(
            ['sudo', '-n','cp', temp_path, str(anchor_path)],
            capture_output=True,
            text=True
        )
        
        Path(temp_path).unlink()  # Clean up temp file
        
        if result.returncode != 0:
            logger.warning(f"Failed to create pf anchor (may need sudo): {result.stderr}")
            return False
        
        # Load the anchor
        subprocess.run(['sudo', '-n','pfctl', '-a', 'vaf', '-f', str(anchor_path)], capture_output=True)
        
        # Enable pf if not already enabled
        subprocess.run(['sudo', '-n','pfctl', '-e'], capture_output=True)
        
        logger.info("macOS pf rules created successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to setup macOS firewall: {e}")
        return False


def _cleanup_firewall_macos() -> bool:
    """Remove macOS pf rules."""
    logger.info("Cleaning up macOS pf rules")
    
    try:
        # Flush the vaf anchor
        subprocess.run(['sudo', '-n','pfctl', '-a', 'vaf', '-F', 'all'], capture_output=True)
        
        # Remove anchor file
        anchor_path = Path("/etc/pf.anchors/vaf")
        if anchor_path.exists():
            subprocess.run(['sudo', '-n','rm', str(anchor_path)], capture_output=True)
        
        return True
    except Exception as e:
        logger.error(f"Failed to cleanup macOS firewall: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# LINUX IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_firewall_linux(port: int, port_frontend: int) -> bool:
    """
    Create Linux iptables rules for LAN-only access.
    
    Uses iptables directly. Also checks for ufw as an alternative.
    Note: Requires root privileges.
    """
    logger.info("Setting up Linux firewall rules for local network access")

    # Prefer firewalld when it's the active firewall (most modern Linux desktops). It supports a clean,
    # LAN-subnet-scoped rich rule and — crucially — pkexec elevation, which pops a NATIVE password dialog
    # in desktop mode instead of a dead `sudo` TTY prompt. iptables/ufw stay as the fallback.
    if _firewalld_running():
        return _setup_firewall_linux_firewalld(port, port_frontend)

    # Check if ufw is available and active
    ufw_available = subprocess.run(
        ['which', 'ufw'],
        capture_output=True
    ).returncode == 0

    if ufw_available:
        return _setup_firewall_linux_ufw(port, port_frontend)

    # Use iptables directly
    return _setup_firewall_linux_iptables(port, port_frontend)


# ── firewalld backend (modern Linux): LAN-subnet rich rule + pkexec GUI elevation ────────────────────

def _firewalld_running() -> bool:
    """True only if firewalld is installed AND running (so we don't try rich rules on an iptables-only box)."""
    try:
        if subprocess.run(['which', 'firewall-cmd'], capture_output=True).returncode != 0:
            return False
        r = subprocess.run(['firewall-cmd', '--state'], capture_output=True, text=True, timeout=5)
        return 'running' in ((r.stdout or '') + (r.stderr or '')).lower()
    except Exception:
        return False


def _lan_subnet_cidr() -> Optional[str]:
    """CIDR of the network the LAN IP sits on (e.g. 192.168.2.0/24), so the opening is scoped to the LAN
    only (RFC1918) — never the whole interface or the internet. Uses the interface's real netmask."""
    try:
        import ipaddress
        import socket as _socket
        import psutil
        from vaf.network.binding import get_local_network_ip
        lan_ip = get_local_network_ip()
        if not lan_ip:
            return None
        for _iface, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if getattr(a, 'family', None) == _socket.AF_INET and a.address == lan_ip and a.netmask:
                    return str(ipaddress.ip_network(f"{lan_ip}/{a.netmask}", strict=False))
    except Exception as e:
        logger.debug("firewalld: LAN subnet detection failed: %s", e)
    return None


def _firewalld_zone() -> str:
    """Zone of the interface that carries the LAN IP (a rule only affects traffic on interfaces in that
    zone). Falls back to the default zone, then 'public'."""
    try:
        import socket as _socket
        import psutil
        from vaf.network.binding import get_local_network_ip
        lan_ip = get_local_network_ip()
        if lan_ip:
            for iface, addrs in psutil.net_if_addrs().items():
                if any(getattr(a, 'family', None) == _socket.AF_INET and a.address == lan_ip for a in addrs):
                    r = subprocess.run(['firewall-cmd', '--get-zone-of-interface', iface],
                                       capture_output=True, text=True, timeout=5)
                    if r.returncode == 0 and r.stdout.strip():
                        return r.stdout.strip()
                    break
        r = subprocess.run(['firewall-cmd', '--get-default-zone'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return 'public'


def _firewalld_rich_rule(subnet: str, port: int) -> str:
    return (f'rule family="ipv4" source address="{subnet}" '
            f'port port="{port}" protocol="tcp" accept')


def _firewalld_rule_present(zone: str, rule: str) -> bool:
    try:
        r = subprocess.run(['firewall-cmd', '--zone', zone, '--query-rich-rule', rule],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and 'yes' in (r.stdout or '').lower()
    except Exception:
        return False


def _elevation_argv() -> list:
    """How to gain root for the firewall change: pkexec in desktop mode (NATIVE polkit password dialog),
    otherwise non-interactive sudo (`sudo -n`) so a headless/server run fails fast instead of hanging on
    a TTY password prompt."""
    if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and \
       subprocess.run(['which', 'pkexec'], capture_output=True).returncode == 0:
        return ['pkexec']
    return ['sudo', '-n']


def _setup_firewall_linux_firewalld(port: int, port_frontend: int) -> bool:
    """Open ONLY the LAN access port (the integrated HTTPS proxy port, e.g. 8443) for the LAN subnet, via
    a firewalld rich rule in the interface's zone. The backend (8001) and frontend (3000) bind 127.0.0.1
    and are unreachable from the LAN, so they are deliberately NOT opened. Idempotent: if the rule already
    exists, nothing runs and no password dialog appears."""
    subnet = _lan_subnet_cidr()
    if not subnet:
        logger.warning("firewalld: could not determine the LAN subnet; not opening any port")
        return False
    zone = _firewalld_zone()
    rule = _firewalld_rich_rule(subnet, port)
    if _firewalld_rule_present(zone, rule):
        logger.info("firewalld: LAN access already open (zone=%s, source=%s, port=%s)", zone, subnet, port)
        return True
    # Add to BOTH runtime (effective immediately) and permanent (survives reboot) in a SINGLE elevation
    # so the user sees at most one password dialog.
    q = shlex.quote(rule)
    inner = (f"firewall-cmd --zone={shlex.quote(zone)} --add-rich-rule={q} && "
             f"firewall-cmd --permanent --zone={shlex.quote(zone)} --add-rich-rule={q}")
    argv = _elevation_argv() + ['sh', '-c', inner]
    try:
        logger.info("firewalld: opening port %s for %s in zone %s via %s", port, subnet, zone, argv[0])
        subprocess.run(argv, check=True, timeout=120)
        logger.info("firewalld: LAN access opened (zone=%s, source=%s, port=%s)", zone, subnet, port)
        return True
    except subprocess.TimeoutExpired:
        logger.error("firewalld: elevation timed out (password dialog dismissed?)")
        return False
    except subprocess.CalledProcessError as e:
        logger.error("firewalld: could not add rich rule (dialog cancelled / no privileges?): %s", e)
        return False
    except Exception as e:
        logger.error("firewalld: setup error: %s", e)
        return False


def _setup_firewall_linux_iptables(port: int, port_frontend: int) -> bool:
    """Setup using iptables."""
    
    # First cleanup any existing rules
    _cleanup_firewall_linux()
    
    ports = [port, port_frontend]
    
    try:
        for p in ports:
            # Allow localhost
            subprocess.run([
                'sudo', '-n','iptables', '-A', 'INPUT',
                '-i', 'lo',
                '-p', 'tcp', '--dport', str(p),
                '-j', 'ACCEPT',
                '-m', 'comment', '--comment', f'VAF-localhost-{p}'
            ], check=True)
            
            # Allow private ranges
            for cidr in PRIVATE_CIDRS:
                subprocess.run([
                    'sudo', '-n','iptables', '-A', 'INPUT',
                    '-p', 'tcp', '--dport', str(p),
                    '-s', cidr,
                    '-j', 'ACCEPT',
                    '-m', 'comment', '--comment', f'VAF-private-{p}'
                ], check=True)
            
            # Block all other incoming on this port
            subprocess.run([
                'sudo', '-n','iptables', '-A', 'INPUT',
                '-p', 'tcp', '--dport', str(p),
                '-j', 'DROP',
                '-m', 'comment', '--comment', f'VAF-block-{p}'
            ], check=True)
        
        logger.info(f"Linux iptables rules created for ports {ports}")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to setup iptables: {e}")
        return False


def _setup_firewall_linux_ufw(port: int, port_frontend: int) -> bool:
    """Setup using ufw (Uncomplicated Firewall)."""
    
    ports = [port, port_frontend]
    
    try:
        for p in ports:
            # Allow from private networks
            for cidr in PRIVATE_CIDRS:
                subprocess.run([
                    'sudo', '-n','ufw', 'allow',
                    'from', cidr,
                    'to', 'any',
                    'port', str(p),
                    'proto', 'tcp',
                    'comment', f'VAF-{p}'
                ], check=True)
            
            # Deny from anywhere else (ufw default deny handles this)
        
        # Reload ufw
        subprocess.run(['sudo', '-n','ufw', 'reload'], capture_output=True)
        
        logger.info(f"Linux ufw rules created for ports {ports}")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to setup ufw: {e}")
        return False


def _cleanup_firewall_linux() -> bool:
    """Remove Linux firewall rules."""
    logger.info("Cleaning up Linux firewall rules")
    
    try:
        # Try to find and delete VAF rules from iptables
        # List rules with line numbers
        result = subprocess.run(
            ['sudo', '-n','iptables', '-L', 'INPUT', '-n', '--line-numbers'],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            # Find lines with VAF comment and delete them (in reverse order)
            lines = result.stdout.split('\n')
            vaf_rules = []
            for line in lines:
                if 'VAF' in line:
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        vaf_rules.append(int(parts[0]))
            
            # Delete in reverse order to preserve line numbers
            for rule_num in sorted(vaf_rules, reverse=True):
                subprocess.run(
                    ['sudo', '-n','iptables', '-D', 'INPUT', str(rule_num)],
                    capture_output=True
                )
        
        # Also try ufw cleanup
        subprocess.run(
            ['sudo', '-n','ufw', 'delete', 'allow', 'proto', 'tcp', 'to', 'any', 'port', '8001'],
            capture_output=True
        )
        subprocess.run(
            ['sudo', '-n','ufw', 'delete', 'allow', 'proto', 'tcp', 'to', 'any', 'port', '3000'],
            capture_output=True
        )
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to cleanup Linux firewall: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-CLEANUP ON EXIT
# ═══════════════════════════════════════════════════════════════════════════════

_cleanup_registered = False

def register_cleanup_on_exit():
    """
    Register cleanup function to run on application exit.
    
    This ensures firewall rules are removed when VAF shuts down.
    """
    global _cleanup_registered
    if not _cleanup_registered:
        atexit.register(cleanup_firewall)
        _cleanup_registered = True
        logger.debug("Firewall cleanup registered for application exit")
