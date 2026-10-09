# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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
import threading
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


def _sources(*, narrow_lan: bool = False) -> list:
    """The source networks to open, from the one admission decision (binding.inbound_policy)."""
    from vaf.network.binding import firewall_sources
    return firewall_sources(narrow_lan=narrow_lan)


# One elevation attempt per (port, port_frontend, sources) per PROCESS. Deliberate: in
# TLS mode the same app runs two uvicorn lifespans (8001 + 8005) and both spawn the
# firewall setup within milliseconds - without this claim the user can face TWO
# password dialogs for one start, and a CANCELLED dialog chains straight into the
# twin's dialog. The claim is taken at ENTRY (not on success) so the racing twin
# is deduplicated even while the first attempt still sits on the open dialog.
# Failures are deliberately not retried in-process for the same sources: a second
# unprompted dialog is exactly the annoyance this guards against. A change of the
# admitted networks is a new key, so an admin's change is applied without a restart.
_attempted_ports: dict = {}   # {(port, frontend, sources): "present"|"created"|True|False|None}
_attempt_lock = threading.Lock()


def apply_lan_firewall(log=None):
    """Open the access port for the admitted networks, if network mode and the firewall
    step are on. The one place both callers go through: the server's startup and a
    change of the admitted networks (vaf/tray.py). Blocking (it may wait for a password
    dialog), so callers run it in a thread. Returns what `setup_firewall` returned, or
    None when it was not asked to run."""
    from vaf.core.config import Config
    say = log or (lambda msg: logger.info(msg))
    if not (Config.get_bool("local_network_enabled", False)
            and Config.get_bool("local_network_firewall_enabled", True)):
        return None
    from vaf.network.binding import resolve_lan_access_ports
    # In-process caller: wait for the proxy to report the port it ACTUALLY bound
    # (443->8443 fallback) instead of trusting the configured value.
    port, port_frontend = resolve_lan_access_ports(wait_for_proxy=True)
    result = setup_firewall(port, port_frontend)
    if result == "present":
        # No elevation ran: the rules were already in place. Kept apart from "created"
        # so a password dialog can be attributed from the log.
        say(f"Firewall rule already in place for port {port} - no password dialog needed")
    elif result == "in_flight":
        # The twin lifespan of TLS mode is running this very setup; it reports the outcome.
        say(f"Firewall setup for port {port} is already running in this process")
    elif result:
        register_cleanup_on_exit()
        say(f"Firewall rules created for ports {port}, {port_frontend}")
    else:
        say(f"Firewall setup skipped for ports {port}, {port_frontend} - needs elevated "
            "privileges (no passwordless sudo). Open the port manually or use the in-app firewall step.")
    return result


def setup_firewall(port: int, port_frontend: int = 3000):
    """
    Setup OS firewall rules for network access.

    Creates rules that:
    - Allow connections from the admitted networks (binding.firewall_sources: the
      local networks or, with "VPN only", the VPN networks, plus an admin's additions)
    - Allow localhost connections
    - Block all other incoming connections on the specified ports

    Args:
        port: Backend port (default 8001)
        port_frontend: Frontend port (default 3000)

    Returns:
        Truthy if the rules are in place: "present" when nothing had to run
        (the marker says this install already set the rule up), "created" when
        the Linux firewalld path actually elevated, True from the other
        platform paths, "in_flight" when a twin lifespan is still on the
        dialog. False on failure - INCLUDING a repeat call after this process
        already failed, so a cancelled dialog can never be logged as success.
        Callers that only check truthiness keep working.
    """
    # Both lists: firewalld opens the narrow one (the LAN subnet, a WireGuard /24), the
    # other backends the wide one, and a change can show in one only - a network an
    # admin adds inside 10.0.0.0/8 leaves the wide list as it was.
    # Unreadable admitted networks: nothing is applied, and nothing is recorded, so the
    # next call (the next start, the next admission change) tries again. No platform
    # setup runs either - every backend reads the same lists and would fail the same way.
    try:
        sources = (tuple(_sources()), tuple(_sources(narrow_lan=True)))
    except Exception as e:
        logger.warning("firewall: admitted networks unreadable (%s); no firewall rule was "
                       "applied, the next setup tries again", e)
        return False
    key = (int(port), int(port_frontend), sources)
    with _attempt_lock:
        if key in _attempted_ports:
            # Report what the first attempt ACTUALLY did, never a blanket
            # "present": if the user cancelled the dialog, the twin lifespan
            # would otherwise make the log say the rule is in place when the
            # port is closed. "in_flight" means the first attempt has not
            # answered yet (the dialog is still open).
            prior = _attempted_ports[key]
            logger.info("firewall: setup for ports %s already attempted in this "
                        "process (result: %s) - not asking again", key, prior)
            return prior if prior is not None else "in_flight"
        _attempted_ports[key] = None
    result = False
    try:
        if Platform.is_windows():
            result = _setup_firewall_windows(port, port_frontend)
        elif Platform.is_macos():
            result = _setup_firewall_macos(port, port_frontend)
        elif Platform.is_linux():
            result = _setup_firewall_linux(port, port_frontend)
        else:
            logger.warning(f"Unsupported platform for firewall: {Platform.current()}")
    except Exception as e:
        logger.error(f"Failed to setup firewall: {e}")
        result = False
    with _attempt_lock:
        _attempted_ports[key] = result
    return result


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
    global _windows_firewall_skip
    if _windows_firewall_skip:
        logger.info("Skipping Windows Firewall setup - a previous netsh attempt in this process failed")
        return False
    logger.info("Setting up Windows Firewall rules for local network access")
    
    # First, remove any existing rules
    _cleanup_firewall_windows()
    
    # The admitted networks plus this machine, comma separated.
    remote_ips = ",".join([*_sources(), "127.0.0.1"])
    
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
            f'remoteip={remote_ips}'
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
    ports_spec = f"{{{port}, {port_frontend}}}"
    allow = "".join(f"pass in quick proto tcp from {cidr} to any port {ports_spec}\n"
                    for cidr in _sources())
    rules = (
        "# VAF Local Network Rules - Auto-generated\n"
        "# Allow localhost\n"
        f"pass in quick on lo0 proto tcp to any port {ports_spec}\n"
        "\n"
        "# Allow the admitted networks (vaf/network/binding.py inbound_policy)\n"
        f"{allow}"
        "\n"
        "# Block everything else on these ports\n"
        f"block in quick proto tcp to any port {ports_spec}\n"
    )
    
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

def _setup_firewall_linux(port: int, port_frontend: int):
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
    """True only if firewalld is installed AND running (so we don't try rich rules on an iptables-only box).

    Asks systemd, NOT `firewall-cmd --state`. Measured live (openSUSE): even
    `--state` is polkit action org.fedoraproject.FirewallD1.config, i.e. a root
    password dialog for an unprivileged caller - it was the last remaining
    prompt after the query was replaced by the marker file, firing on every
    start before the marker was even consulted. `systemctl is-active` is a
    plain status read with no polkit gate. Without systemctl (non-systemd box)
    this returns False and the iptables/ufw fallback takes over - `sudo -n`
    there fails fast and never prompts, which is the acceptable direction."""
    try:
        if subprocess.run(['which', 'firewall-cmd'], capture_output=True).returncode != 0:
            return False
        r = subprocess.run(['systemctl', 'is-active', '--quiet', 'firewalld'],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _firewalld_zone_of(interface: str) -> Optional[str]:
    """The zone an interface is in, or None when it is in none. A free lookup for an
    unprivileged caller (only firewalld's CONFIG reads are polkit-gated)."""
    try:
        r = subprocess.run(['firewall-cmd', '--get-zone-of-interface', interface],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _firewalld_zones() -> list:
    """Every zone a client's packet can be judged in: the zone of each LAN and VPN
    interface, and the default zone, which takes the interfaces in no zone (a fresh
    WireGuard or Tailscale interface usually is). A rule is restricted to its source
    either way, so putting it in one more zone admits nobody else."""
    zones = set()
    try:
        from vaf.network.binding import local_interfaces
        for iface in local_interfaces():
            zone = _firewalld_zone_of(iface.name)
            if zone:
                zones.add(zone)
    except Exception as e:
        logger.debug("firewalld: interface zones unreadable: %s", e)
    try:
        r = subprocess.run(['firewall-cmd', '--get-default-zone'], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            zones.add(r.stdout.strip())
    except Exception:
        pass
    return sorted(zones) or ['public']


def _firewalld_rich_rule(subnet: str, port: int) -> str:
    return (f'rule family="ipv4" source address="{subnet}" '
            f'port port="{port}" protocol="tcp" accept')


def _firewalld_rules(port: int) -> list:
    """The (zone, rich rule) pairs that admit the admitted networks to `port`: the LAN
    subnets this machine sits on (never all of RFC 1918), the admitted VPN networks
    and an admin's additions, each in every zone of `_firewalld_zones`."""
    sources = _sources(narrow_lan=True)
    return sorted((zone, _firewalld_rich_rule(src, port))
                  for zone in _firewalld_zones() for src in sources)


def _firewalld_marker_path() -> Path:
    from vaf.core.config import Config
    return Config.APP_DIR / "firewalld_lan.json"


def _firewalld_marker_read() -> Optional[list]:
    """The (zone, rule) pairs this install put in place, or None when it has no record.

    The marker REPLACES asking firewalld: an unprivileged
    `firewall-cmd --query-rich-rule` is a CONFIG read, and distros ship that
    polkit action as auth_admin_keep (measured live on openSUSE:
    `org.fedoraproject.FirewallD1.config.info` = auth_admin_keep, only the
    runtime `.info` action is free) - so the presence CHECK itself raised the
    root password dialog on every app start, which is exactly what this
    function exists to avoid. The marker can go stale if a rule is removed
    behind our back; the failure direction is then a CLOSED port (safe), and
    deleting the marker file or toggling Local Network re-runs the setup.
    A marker from before the rule sets ({"zone", "rule"}) reads as a set of one."""
    try:
        import json
        data = json.loads(_firewalld_marker_path().read_bytes().decode("utf-8"))
        if isinstance(data.get("rules"), list):
            return sorted((str(z), str(r)) for z, r in data["rules"])
        if data.get("zone") and data.get("rule"):
            return [(str(data["zone"]), str(data["rule"]))]
    except Exception:
        pass
    return None


def _firewalld_marker_matches(rules: list) -> bool:
    """True when this install already put exactly these rules in place."""
    return _firewalld_marker_read() == sorted(rules)


def _firewalld_marker_write(rules: list) -> None:
    try:
        import json
        p = _firewalld_marker_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(json.dumps({"rules": [list(r) for r in sorted(rules)]}).encode("utf-8"))
    except Exception as e:
        logger.debug("firewalld: could not write the marker file: %s", e)


def elevation_argv() -> list:
    """How to gain root for a host change: pkexec in desktop mode (NATIVE polkit password dialog),
    otherwise non-interactive sudo (`sudo -n`) so a headless/server run fails fast instead of hanging on
    a TTY password prompt. The one elevation lane of the process: the firewall setup here and the
    service repair (vaf/core/service_health.py, the host's IP forwarding switch) both use it, so a
    platform that needs a different dialog changes one function."""
    if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and \
       subprocess.run(['which', 'pkexec'], capture_output=True).returncode == 0:
        return ['pkexec']
    return ['sudo', '-n']


# The older private name; the callers and tests that grew up with it keep working.
_elevation_argv = elevation_argv


def _setup_firewall_linux_firewalld(port: int, port_frontend: int):
    """Open ONLY the access port (the integrated HTTPS proxy port, e.g. 8443) for the admitted networks,
    via firewalld rich rules. The backend (8001) and frontend (3000) bind 127.0.0.1 and are unreachable
    from the network, so they are deliberately NOT opened. Idempotent without asking firewalld: a local
    marker file remembers the rules this install set up, so the normal start runs zero firewall-cmd
    config reads and can never raise a password dialog. Only a change (first run, another subnet, port
    or zone, an admin admitting or dropping a network) elevates - once, covering the checks, the adds
    and the removal of the rules this install had set up and no longer wants."""
    rules = _firewalld_rules(port)
    previous = _firewalld_marker_read() or []
    if previous == rules:
        logger.info("firewalld: access already set up by this install (%d rule(s), port %s)", len(rules), port)
        return "present"
    stale = [r for r in previous if r not in rules]
    if not rules:
        logger.warning("firewalld: no network is admitted besides this machine; opening nothing")
    logger.warning("firewalld: the rules changed, requesting elevation (%d to ensure, %d to remove)",
                   len(rules), len(stale))
    # ONE elevation covers everything: each query rides INSIDE it together with
    # the runtime and permanent adds. Deliberate: running the query unprivileged
    # is itself an auth_admin polkit action on common distros - the check would
    # cost the very password dialog it tries to avoid (live incident: a root
    # dialog on every start for weeks while the permanent rule existed the whole
    # time; every one of those passwords went into the CHECK, never into a change).
    # A removal asks first: a rule that is already gone (in the runtime or the permanent
    # set) is not an error, but a removal that FAILS fails the whole elevation, so the marker
    # keeps the rule and the next setup tries again. Swallowing it forgot the rule while its
    # permanent copy came back at the next boot.
    steps = []
    for zone, rule in stale:
        z, q = shlex.quote(zone), shlex.quote(rule)
        steps.append(f"{{ ! firewall-cmd --zone={z} --query-rich-rule={q} >/dev/null 2>&1 || "
                     f"firewall-cmd --zone={z} --remove-rich-rule={q}; }} && "
                     f"{{ ! firewall-cmd --permanent --zone={z} --query-rich-rule={q} >/dev/null 2>&1 || "
                     f"firewall-cmd --permanent --zone={z} --remove-rich-rule={q}; }}")
    for zone, rule in rules:
        z, q = shlex.quote(zone), shlex.quote(rule)
        steps.append(f"{{ firewall-cmd --zone={z} --query-rich-rule={q} || "
                     f"{{ firewall-cmd --zone={z} --add-rich-rule={q} && "
                     f"firewall-cmd --permanent --zone={z} --add-rich-rule={q}; }}; }}")
    inner = " && ".join(steps) if steps else "true"
    argv = _elevation_argv() + ['sh', '-c', inner]
    try:
        logger.info("firewalld: applying %d rule(s) for port %s via %s", len(rules), port, argv[0])
        subprocess.run(argv, check=True, timeout=120)
        logger.info("firewalld: access ensured (port %s, sources %s)", port,
                    sorted({r.split('source address="')[1].split('"')[0] for _, r in rules}))
        _firewalld_marker_write(rules)
        return "created"
    except subprocess.TimeoutExpired:
        logger.error("firewalld: elevation timed out (password dialog dismissed?)")
        return False
    except subprocess.CalledProcessError as e:
        logger.error("firewalld: could not apply the rich rules (dialog cancelled / no privileges?): %s", e)
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
            
            # Allow the admitted networks
            for cidr in _sources():
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


def _ufw_marker_path() -> Path:
    from vaf.core.config import Config
    return Config.APP_DIR / "ufw_sources.json"


def _ufw_previous(ports: list) -> list:
    """The (port, source) pairs this install allowed through ufw last time.

    ufw only ever adds, so a network an admin dropped would stay open there. The marker
    lets the next setup delete exactly what it added, by the same rule spec, without
    parsing ufw's (translated) output. With no marker, the rules every version before
    the admitted networks added are assumed: RFC 1918 on both ports. Deleting a rule
    that is not there is harmless."""
    try:
        import json
        data = json.loads(_ufw_marker_path().read_bytes().decode("utf-8"))
        return [(int(p), str(s)) for p, s in data.get("rules", [])]
    except FileNotFoundError:
        from vaf.network.binding import PRIVATE_RANGES
        return [(p, str(n)) for p in ports for n in PRIVATE_RANGES]
    except Exception:
        return []


def _setup_firewall_linux_ufw(port: int, port_frontend: int) -> bool:
    """Setup using ufw (Uncomplicated Firewall): allow the admitted networks on both ports,
    and delete the allows this install made for networks that are no longer admitted."""
    
    ports = [port, port_frontend]
    wanted = [(p, cidr) for p in ports for cidr in _sources()]
    
    try:
        for p, cidr in _ufw_previous(ports):
            if (p, cidr) not in wanted:
                subprocess.run([
                    'sudo', '-n', 'ufw', 'delete', 'allow',
                    'from', cidr, 'to', 'any', 'port', str(p), 'proto', 'tcp',
                ], capture_output=True)
        for p, cidr in wanted:
            # Allow from the admitted networks (ufw skips a rule it already has)
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
        try:
            import json
            marker = _ufw_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_bytes(json.dumps({"rules": [list(r) for r in wanted]}).encode("utf-8"))
        except Exception as e:
            logger.debug("ufw: could not write the marker file: %s", e)
        
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
