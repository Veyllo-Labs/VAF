# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import platform
from typing import Optional

import typer
from vaf.core.config import Config
from vaf.cli.ui import UI

app = typer.Typer(help="Manage local network server mode (Hosting/SSL)")
networks_app = typer.Typer(help="Networks admitted besides the local ones, e.g. a VPN")
app.add_typer(networks_app, name="networks")

# Tailscale, Headscale and NetBird hand out addresses from the shared address space.
_MESH_VPN_NETWORK = "100.64.0.0/10"


def _enable_lan_keys() -> None:
    """Enable LAN hosting with mandatory TLS through Config (coercion + observers)."""
    Config.set("local_network_enabled", True)
    Config.set("local_network_tls_enabled", True)


def _access_port() -> int:
    """The LAN access port, for what is printed.

    Cosmetic by definition, and its callers print it AFTER changing config, so
    it must never be the reason a command ends in a traceback with no
    confirmation. An unresolvable port falls back to the usual effective one.
    """
    try:
        from vaf.network.binding import resolve_lan_access_ports
        access_port, _ = resolve_lan_access_ports(wait_for_proxy=False)
    except Exception:
        access_port = 8443
    return access_port


def _access_port_suffix() -> str:
    """URL port suffix for the LAN access port, empty when it is plain 443."""
    access_port = _access_port()
    return "" if access_port == 443 else f":{access_port}"


def _public_addresses() -> list:
    """This machine's public IPv4 addresses as (interface, ip); empty when they cannot be read."""
    try:
        from vaf.network.binding import public_addresses
        return public_addresses()
    except Exception:
        return []


def _named(addresses) -> str:
    """The words for the public addresses: the public address 9.9.9.9 (eth0), or several."""
    plural = "addresses" if len(addresses) > 1 else "address"
    return f"the public {plural} " + ", ".join(f"{ip} ({iface})" for iface, ip in addresses)


@app.command(name="on")
def server_on():
    """Enable local network hosting with mandatory TLS (HTTPS/WSS)."""
    _enable_lan_keys()
    suffix = _access_port_suffix()
    UI.success("✓ Local network hosting enabled (HTTPS/TLS).")
    UI.info(f"VAF serves encrypted LAN access via https://<this-PC-IP>{suffix}.")
    UI.info("")
    UI.info("Der Tray erkennt die Änderung innerhalb von ~30 Sekunden und startet neu. Für sofortige Wirkung: Tray beenden und neu starten (z. B. 'vaf tray').")

@app.command(name="off")
def server_off():
    """Disable local network hosting and SSL encryption."""
    Config.set("local_network_enabled", False)
    Config.set("local_network_tls_enabled", False)
    UI.success("✓ Local network hosting and SSL disabled.")
    UI.info("VAF will now listen on 127.0.0.1 (localhost only) via HTTP.")
    UI.info("Tray neu starten, damit die Änderung wirkt (oder in der Web-UI umschalten).")

@app.command(name="status")
def server_status():
    """Show current server mode status. With network on, access via integrated HTTPS proxy (https://IP:port)."""
    enabled = Config.get("local_network_enabled", False)
    tls = Config.get("local_network_tls_enabled", False)
    port = Config.get("local_network_port", 8001)

    UI.print("\n[bold]Server Mode Status:[/bold]")
    UI.print(f"  Hosting Enabled: {'[green]YES[/green]' if enabled else '[red]NO (Localhost only)[/red]'}")
    UI.print(f"  SSL/TLS Active:  {'[green]YES[/green]' if tls else '[red]NO (Plain HTTP)[/red]'}")
    UI.print(f"  Primary Port:    [cyan]{port}[/cyan]")
    from vaf.core.inference_profile import current as inference_profile
    UI.print(f"  Profile:         [cyan]{inference_profile()}[/cyan] (`vaf profile` shows what it runs)")
    if tls:
        try:
            _print_certificate_line()
        except Exception as e:
            UI.warning(f"Could not read the certificate: {e}")

    if enabled:
        try:
            _print_access(_access_port())
        except Exception as e:
            UI.warning(f"Could not read the network interfaces: {e}")
    UI.print()


def _print_certificate_line() -> None:
    """One line on the certificate VAF serves, and its problem when it has one."""
    from vaf.network.ssl_utils import certificate_state
    state = certificate_state()
    facts = state.get("certificate")
    if facts is None:
        validity = "issued at the next start"
    elif facts["days_left"] < 0:
        validity = "[red]EXPIRED[/red]"
    elif facts["days_left"] == 1:
        validity = "valid for 1 more day"
    else:
        validity = f"valid for {facts['days_left']} more days"
    UI.print(f"  Certificate:     [cyan]{_source_words(state['source'])}[/cyan], {validity} "
             "(`vaf server cert show`)")
    if state.get("problem"):
        UI.warning(f"Your certificate cannot be used: {state['problem']['text']} VAF serves its "
                   "own instead (`vaf server cert show`).")


def _print_access(access_port: int) -> None:
    """Interfaces with their access URL, the admitted networks, and what was refused. The one
    access list: `vaf server status` prints it, and provision closes with it."""
    from vaf.network.binding import (REFUSAL_REASONS, access_addresses, inbound_policy,
                                     local_interfaces)
    suffix = "" if access_port == 443 else f":{access_port}"
    policy = inbound_policy(detect_vpn=True)
    admitted = {(i.name, i.ip) for i in access_addresses()}
    interfaces = local_interfaces()
    UI.print("\n[bold]Access (integrated HTTPS proxy):[/bold]")
    if not interfaces:
        UI.print("  [dim]no LAN or VPN interface found[/dim]")
    for iface in interfaces:
        kind = "VPN" if iface.kind == "vpn" else "LAN"
        if (iface.name, iface.ip) in admitted:
            UI.print(f"  {iface.name:<14} {kind}  https://{iface.ip}{suffix}")
        else:
            hint = ""
            if iface.network == _MESH_VPN_NETWORK:
                hint = " - vaf server networks tailscale on"
            elif policy.vpn_only and iface.kind == "lan":
                hint = " - locked out by VPN only"
            UI.print(f"  {iface.name:<14} {kind}  [dim]{iface.ip} not admitted{hint}[/dim]")
        if policy.vpn_only and iface.kind == "vpn" and iface.network.endswith("/32"):
            UI.print(f"  {'':<14}      [yellow]{iface.network} names no peer: add the VPN's "
                     "network with vaf server networks allow[/yellow]")
    local = "the VPN networks" if policy.vpn_only else "the local networks"
    UI.print(f"\n[bold]Admitted:[/bold] this machine, {local}"
             + (f", {', '.join(policy.allowed)}" if policy.allowed else ""))
    UI.print(f"[bold]VPN only:[/bold] {'[yellow]YES[/yellow]' if policy.vpn_only else 'no'}")
    for value, code in policy.refused:
        UI.warning(f"Ignored entry {value}: {REFUSAL_REASONS.get(code, code)}")
    public = _public_addresses()
    if public:
        UI.warning(f"This machine has {_named(public)}: keep port {access_port} closed in the "
                   "hosting provider's firewall or the router and reach VAF over a VPN.")
    if policy.vpn_only and not admitted:
        # A VPN that comes up later is admitted at once, but its address enters the
        # certificate only at the next start (ssl_utils re-issues for a missing address).
        UI.warning("No other device can connect yet. Once the VPN is up, `vaf server status` "
                   "lists the address to open; restart VAF (`systemctl --user restart vaf`) "
                   "so the certificate names it.")
    UI.print("Reach VAF from this machine, your local network or a VPN only. Never forward the "
             "access port or publish it to the internet.")


@app.command(name="provision")
def server_provision(
    open_firewall: bool = typer.Option(
        True,
        "--firewall/--no-firewall",
        help="Open the OS firewall for the LAN access port (subnet-scoped)",
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile",
        help="local (models and speech run here) or api (none; chat and speech via providers)",
    ),
):
    """One-shot server-mode provisioning (idempotent; install.sh server mode calls this).

    Enables server mode and LAN hosting with mandatory TLS, prepares the
    self-signed certificates, opens the OS firewall for the effective access
    port, and warns when the LAN address looks DHCP-assigned. Firewall and
    certificate problems degrade to warnings with manual instructions; only a
    non-Linux platform exits nonzero.
    """
    if platform.system() != "Linux":
        UI.error("Server provisioning is Linux-only (systemd service plus firewall automation).")
        raise typer.Exit(1)

    from vaf.core import inference_profile
    if profile is not None and profile.strip().lower() not in inference_profile.PROFILES:
        UI.error(f"Unknown profile {profile!r}: use local or api.")
        raise typer.Exit(2)

    # Read before it is set: "VPN only" is chosen for a server set up for the first time, and
    # never flipped on one that already serves its users (a re-run is the documented step
    # after every admission change, and would lock them out).
    was_server = Config.get_bool("server_mode", False)
    _enable_lan_keys()
    Config.set("server_mode", True)
    UI.success("Server mode enabled (LAN hosting with TLS, locked on).")
    if profile is not None:
        # The one switch (vaf profile, the web settings): on an installation that already
        # runs, the speech containers follow the profile. The installers do not pass
        # --profile here; they set it with `vaf profile` before the stack starts.
        # A switch that fails must not end the provisioning: the steps below (the database
        # passwords, the certificates, the firewall) protect the server either way.
        try:
            switched = inference_profile.apply(profile.strip().lower())
        except Exception as e:
            UI.warning(f"The inference profile could not be switched ({e}); run "
                       f"`vaf profile {profile.strip().lower()}` once the rest is done.")
        else:
            UI.success(f"Inference profile: {switched['profile']} (`vaf profile` shows what it runs).")
            for problem in switched["errors"]:
                UI.warning(problem)

    # A server must not keep the database passwords published in this repository: whoever has
    # one writes every table (vaf/core/db_credentials.py). Replaced here when the database
    # answers; otherwise said loudly, with the command that does it.
    from vaf.core.db_credentials import (RotationIncomplete, RotationUnsaved,
                                         keys_on_shipped_password, rotate_shipped_passwords,
                                         shipped_password_in_use)
    if shipped_password_in_use():
        try:
            rotated = rotate_shipped_passwords()
            UI.success("Database passwords replaced with random ones and verified "
                       f"({', '.join(rotated)}). Restart VAF so every worker uses them.")
        except RotationIncomplete as e:
            # Each part as it is: what is done, a password that WAS changed but not saved (the
            # error carries its only copy), and what really still has the published one.
            if e.done:
                UI.success(f"Replaced and verified: {', '.join(e.done)}. Restart VAF so every "
                           "worker uses them.")
            unsaved = isinstance(e.cause, RotationUnsaved)
            if unsaved:
                UI.error(str(e.cause))
            left = [k for k in keys_on_shipped_password() if not (unsaved and k == e.key)]
            if left:
                cause = "" if unsaved else f" ({type(e.cause).__name__}: {e.cause})"
                then = " once that connection string is saved" if unsaved else ""
                UI.warning("The memory database still uses the published default password for "
                           f"{', '.join(left)}, and it could not be replaced now{cause}. Start the "
                           f"service stack and run `vaf secure rotate-db`{then}, before anyone can "
                           "reach this server.")
    else:
        UI.info("The database passwords are not the published defaults.")

    # A server backs itself up: daily, unless somebody already chose (an "off" stays off).
    # vaf/core/backup_schedule.py runs it while the service runs.
    if not str(Config.get("backup_schedule", "") or "").strip():
        Config.set("backup_schedule", "daily")
        UI.success("Backups scheduled daily at " + str(Config.get("backup_time", "03:00"))
                   + " (`vaf backup schedule` changes it; copy them off the server too).")

    try:
        from vaf.network.ssl_utils import ensure_ssl_certificates
        cert_path, _key_path = ensure_ssl_certificates()
        if cert_path:
            from vaf.network.ssl_utils import served_certificate
            UI.info(f"TLS certificate ready: {cert_path} "
                    f"({_source_words(served_certificate().source)})")
    except Exception as e:
        UI.warning(f"TLS certificate preparation failed ({e}); it is retried on service start.")

    from vaf.network.binding import resolve_lan_access_ports
    access_port, frontend_port = resolve_lan_access_ports(wait_for_proxy=False)

    # A machine with a public address sits on the internet, where the private ranges VAF
    # admits by default are the hosting provider's network and the container bridges, not
    # anybody's home LAN. Before the firewall step, so it opens the port for the VPN only.
    public = _public_addresses()
    if public:
        if not Config.get_bool("local_network_vpn_only", False):
            if not was_server:
                Config.set("local_network_vpn_only", True)
                UI.warning(f"This machine has {_named(public)}, so VAF admits only devices in a "
                           "VPN (\"VPN only\" is on). Set up WireGuard, Tailscale or NetBird and "
                           "open VAF at its VPN address; `vaf server vpn-only off` admits the "
                           "local networks again.")
            else:
                UI.warning(f"This machine has {_named(public)}, and \"VPN only\" is off, so "
                           "every private network it reaches is admitted, a hosting provider's "
                           "own network and the container bridges included: "
                           "`vaf server vpn-only on`.")
        UI.info(f"Keep port {access_port} closed in the hosting provider's firewall; open only "
                "SSH and the VPN's own port there.")

    if open_firewall:
        # Which firewall the setup takes, read once: every report below asks about this
        # answer. A later read of a ufw that went off after its rule was added says
        # "ufw_off", and the check for an unconfirmed ufw would then stay silent.
        engine = None
        try:
            from vaf.network.firewall import host_firewall, setup_firewall
            engine = host_firewall()
            outcome = setup_firewall(access_port, frontend_port)
        except Exception as e:
            outcome = False
            UI.warning(f"Firewall setup error: {e}")
        from vaf.network.firewall import FIREWALL_INACTIVE, ufw_off_advice, ufw_switch_unconfirmed
        if outcome == FIREWALL_INACTIVE:
            # Rules added to a switched-off ufw do nothing; reporting the port as opened was
            # the measured mistake (vaf/network/firewall.py, host_firewall).
            UI.warning(f"ufw is installed but switched off, so it does not guard port "
                       f"{access_port}. {ufw_off_advice()}")
        elif outcome and _opened_sources(engine) == []:
            # "VPN only" with no VPN up admits nobody: the setup succeeded by opening nothing
            # (and closing what it had opened before), which "opened" or "already in place"
            # would misreport. After the VPN is up the service cannot re-open it (Layer 1).
            UI.warning(f"Firewall not opened for port {access_port}: no network besides this "
                       "machine is admitted yet. Once the VPN is up, run "
                       "`sudo -v && vaf server provision` again so it opens the port for it.")
        elif outcome and (unconfirmed := ufw_switch_unconfirmed(engine)):
            # The rule is in ufw either way, but whether ufw runs is not known, so "opened"
            # may not stand alone (vaf/network/firewall.py, host_firewall). The ufw path
            # answers True or False only; "present" comes from firewalld alone.
            UI.warning(f"Firewall rule added for port {access_port}, but VAF could not confirm "
                       f"that ufw is switched on: {unconfirmed}. Check with `sudo ufw status` "
                       "that it is active.")
        elif outcome == "present":
            UI.success(f"Firewall rule already in place for port {access_port}.")
        elif outcome:
            UI.success(f"Firewall opened for port {access_port} (admitted networks only).")
        else:
            # Run from a login shell this is a refused or missing sudo; run inside the
            # service (the agent's shell, for one) no setuid helper can work at all.
            try:
                from vaf.network.firewall import elevation_unavailable
                no_root = elevation_unavailable()
            except Exception:
                no_root = None
            if no_root:
                UI.warning("Could not open the OS firewall automatically (this process "
                           f"cannot gain root: {no_root}).")
            else:
                UI.warning("Could not open the OS firewall automatically (needs elevation).")
            try:
                from vaf.network.binding import firewall_sources
                sources = firewall_sources(narrow_lan=True)
            except Exception:
                sources = []
            if not sources:
                # No example network instead: a rule for a network that is not admitted (or not
                # even this machine's) would open the port to the wrong devices.
                UI.warning("No admitted network could be determined, so there is no safe rule to "
                           "suggest. Check `vaf server status` and `vaf server networks list`.")
            else:
                UI.info("Open the access port manually for the admitted networks, e.g. with firewalld:")
                for source in sources:
                    UI.info(
                        "  sudo firewall-cmd --permanent --zone=public --add-rich-rule="
                        f"'rule family=\"ipv4\" source address=\"{source}\" port port=\"{access_port}\" protocol=\"tcp\" accept'"
                    )
                UI.info("  sudo firewall-cmd --reload")
                UI.info("  or with ufw:")
                for source in sources:
                    UI.info(f"  sudo ufw allow from {source} to any port {access_port} proto tcp")
    else:
        UI.info("Firewall step skipped (--no-firewall).")

    from vaf.network.binding import lan_ip_is_dhcp
    dhcp = lan_ip_is_dhcp()
    if dhcp is True:
        UI.warning("The LAN IP looks DHCP-assigned. If it changes, the access URL and the TLS certificate change with it.")
        UI.info("Give this machine a static LAN IP, or reserve its address in the router's DHCP settings.")
    elif dhcp is False:
        UI.info("LAN IP looks statically configured.")

    try:
        _print_access(access_port)
    except Exception as e:
        UI.warning(f"Could not read the network interfaces: {e}")
    hint = _first_admin_hint()
    if hint:
        UI.info(hint)


def _opened_sources(engine):
    """The networks the firewall step opens the access port for, picked the way setup_firewall
    picks them for `engine`, `host_firewall()`'s answer (firewalld only the LAN subnets this
    machine sits on), or None when that cannot be read."""
    try:
        from vaf.network.binding import firewall_sources
        return firewall_sources(narrow_lan=engine == "firewalld")
    except Exception:
        return None


def _first_admin_hint():
    """What to do while this installation has no admin: a browser on another device needs the
    setup code, which only this machine can show. None once an admin exists (or when the key
    store cannot say, which counts as yes)."""
    try:
        from vaf.auth import account_seal
        if account_seal.admin_established():
            return None
    except Exception:
        return None
    return ("No admin account exists yet. Create it here with `vaf setup`, or open an address "
            "above in a browser and enter the code `vaf setup code` shows on this machine.")


# ── who is admitted besides the local networks ─────────────────────────────
#
# The CLI half of the "Remote access (VPN)" settings. Both write the same two keys and
# go through the same check (binding.normalize_allowed_networks). The access check reads
# the file on every request, without a restart. A VAF in a desktop session tries to apply
# it to the OS firewall within about 25 seconds (the tray polls the file); the service the
# server installer sets up cannot gain root (vaf.network.firewall.elevation_unavailable),
# so there the operator runs `vaf server provision`, which _applied_note says on a server
# only. Named boundary: like the rest of this group there is no
# admin-password door, because install.sh runs it without a terminal, and the config
# file is writable only by the same OS user anyway.

def _allowed_entries() -> list:
    from vaf.network.binding import normalize_allowed_networks
    taken, _refused = normalize_allowed_networks(Config.get("local_network_allowed_networks"))
    return taken


def _change_entries(change) -> list:
    """Read the admitted networks, apply `change` and store the result under one config
    lock, so an entry another admin or the web settings wrote in between is not lost.
    `change` returns the new list, or None to store nothing. Returns what is stored now."""
    with Config._locked():
        entries = _allowed_entries()
        updated = change(list(entries))
        if updated is not None and updated != entries:
            Config.set("local_network_allowed_networks", updated)
            return updated
        return entries


def _normalized_or_exit(value: str) -> str:
    from vaf.network.binding import REFUSAL_REASONS, normalize_allowed_networks
    taken, refused = normalize_allowed_networks([value])
    if refused:
        UI.error(f"{value}: {REFUSAL_REASONS.get(refused[0][1], refused[0][1])}")
        raise typer.Exit(1)
    if not taken:
        UI.error("Name a network, e.g. 10.8.0.0/24 or 100.64.0.0/10.")
        raise typer.Exit(1)
    return taken[0]


def _on_off(state: str) -> bool:
    lowered = (state or "").strip().lower()
    if lowered not in ("on", "off"):
        UI.error("Say on or off.")
        raise typer.Exit(2)
    return lowered == "on"


def _still_admitted(entry: str) -> list:
    """The admitted networks that still cover `entry` (a network inside the local networks,
    a detected VPN or another entry stays admitted when its own entry goes)."""
    import ipaddress
    from vaf.network.binding import inbound_policy
    net = ipaddress.ip_network(entry)
    return [str(a) for a in inbound_policy(detect_vpn=True).networks
            if not a.is_loopback and net.subnet_of(a)]


def _applied_note() -> None:
    # This command runs in the operator's shell, not in the running VAF, so it cannot measure
    # whether that process may gain root (vaf.network.firewall.elevation_unavailable answers
    # it inside the service, which the web note uses); it judges the install by the config
    # both read and says "usually", never as a fact. `vaf server provision` is named on a
    # server only: it switches server mode on, replaces the database passwords and opens the
    # firewall, which a desktop, or an admin who switched the firewall step off, did not ask
    # for. Every variant says what to do when the change does not reach the OS firewall,
    # including closing what a removed network had open.
    UI.info("The access check uses this at once; nothing restarts.")
    if not Config.get_bool("local_network_firewall_enabled", True):
        UI.info("VAF's firewall step is switched off (local_network_firewall_enabled), so the "
                "OS firewall stays as it is: if it guards the access port, bring its rules in "
                "line with the admitted networks by hand (open new ones, close removed ones).")
    elif Config.get_bool("server_mode", False):
        UI.info("A server install's service usually cannot change the OS firewall (it cannot "
                "gain root): if this server's own firewall guards the access port, run "
                "`sudo -v && vaf server provision`, which opens new networks and closes removed ones.")
    else:
        UI.info("A running VAF tries to apply this to the OS firewall within about 25 seconds "
                "(with firewalld, a password dialog asks for it; ufw and iptables need "
                "passwordless sudo). Where it cannot, bring the firewall's rules in line with "
                "the admitted networks by hand (open new ones, close removed ones).")


@networks_app.command("list")
def networks_list():
    """Show the admitted networks and the entries that are ignored."""
    from vaf.network.binding import REFUSAL_REASONS, inbound_policy
    policy = inbound_policy(detect_vpn=True)
    UI.print("\n[bold]Admitted networks:[/bold]")
    UI.print("  this machine (127.0.0.0/8)")
    if policy.vpn_only:
        UI.print("  the VPN networks: " + (", ".join(policy.vpn) if policy.vpn else "[yellow]none up[/yellow]"))
    else:
        UI.print("  the local networks (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)")
    for entry in policy.allowed:
        UI.print(f"  {entry}")
    UI.print(f"\n[bold]VPN only:[/bold] {'yes' if policy.vpn_only else 'no'}")
    for value, code in policy.refused:
        UI.warning(f"Ignored entry {value}: {REFUSAL_REASONS.get(code, code)}")
    UI.print()


@networks_app.command("allow")
def networks_allow(network: str = typer.Argument(..., help="A private network or address, e.g. 10.8.0.0/24")):
    """Admit a private network besides the local ones."""
    entry = _normalized_or_exit(network)
    already = False

    def change(entries):
        nonlocal already
        already = entry in entries
        return None if already else entries + [entry]

    _change_entries(change)
    if already:
        UI.info(f"{entry} is already admitted.")
        return
    UI.success(f"Admitted {entry}.")
    _applied_note()


@networks_app.command("remove")
def networks_remove(network: str = typer.Argument(..., help="The network to drop")):
    """Stop admitting a network that was added."""
    entry = _normalized_or_exit(network)
    found = False

    def change(entries):
        nonlocal found
        found = entry in entries
        return [e for e in entries if e != entry] if found else None

    _change_entries(change)
    if not found:
        UI.error(f"{entry} is not in the admitted networks.")
        raise typer.Exit(1)
    holders = _still_admitted(entry)
    if holders:
        UI.success(f"Removed {entry} from your entries.")
        UI.info(f"It stays admitted: it lies inside {', '.join(holders)}.")
    else:
        UI.success(f"No longer admitting {entry}.")
    _applied_note()


@networks_app.command("tailscale")
def networks_tailscale(state: str = typer.Argument(..., metavar="on|off")):
    """Admit Tailscale, Headscale and NetBird devices (100.64.0.0/10)."""
    on = _on_off(state)

    def change(entries):
        if on and _MESH_VPN_NETWORK not in entries:
            return entries + [_MESH_VPN_NETWORK]
        if not on and _MESH_VPN_NETWORK in entries:
            return [e for e in entries if e != _MESH_VPN_NETWORK]
        return None

    _change_entries(change)
    holders = [] if on else _still_admitted(_MESH_VPN_NETWORK)
    if on:
        UI.success(f"Tailscale / NetBird ({_MESH_VPN_NETWORK}) admitted.")
    elif holders:
        UI.success(f"Removed {_MESH_VPN_NETWORK} from your entries.")
        UI.info("It stays admitted while VPN only is on and Tailscale or NetBird is up: "
                "the detected VPN networks are admitted then.")
    else:
        UI.success(f"Tailscale / NetBird ({_MESH_VPN_NETWORK}) not admitted.")
    _applied_note()


@app.command("vpn-only")
def server_vpn_only(state: str = typer.Argument(..., metavar="on|off")):
    """Admit only the VPN networks (and the ones you added); the local network is locked out."""
    on = _on_off(state)
    Config.set("local_network_vpn_only", on)
    if not on:
        UI.success("VPN only is off: the local networks are admitted again.")
        public = _public_addresses()
        if public:
            UI.warning(f"This machine has {_named(public)}: the local networks admitted now "
                       "include a hosting provider's own network and the container bridges.")
        _applied_note()
        return
    from vaf.network.binding import inbound_policy, local_interfaces
    policy = inbound_policy(detect_vpn=True)
    UI.success("VPN only is on.")
    if policy.vpn:
        UI.info("Admitted VPN networks: " + ", ".join(policy.vpn))
    single = [n for n in policy.vpn if n.endswith("/32")]
    if single:
        # A point-to-point VPN (WireGuard with a /32 address, OpenVPN net30) reports no
        # network around this machine's address; guessing one would admit strangers.
        UI.warning(", ".join(single) + " names only this machine's own address, so no peer on "
                   "that VPN gets in. Add the VPN's network: vaf server networks allow <network>")
    elif not policy.allowed:
        UI.warning("No VPN interface is up, so no other device can connect until one is.")
    # A LAN an entry of the admin's still covers stays reachable, and one an entry covers
    # in part (a single device, a smaller range) keeps exactly those addresses.
    import ipaddress
    blocked, partly = [], []
    for net in sorted({ipaddress.ip_network(i.network) for i in local_interfaces() if i.kind == "lan"},
                      key=lambda n: (int(n.network_address), n.prefixlen)):
        if any(net.subnet_of(a) for a in policy.networks):
            continue
        (partly if any(net.overlaps(a) for a in policy.networks) else blocked).append(str(net))
    if blocked:
        UI.warning("Devices on " + ", ".join(blocked) + " can no longer connect.")
    if partly:
        # Named by what really overlaps: an entry of the admin's, or a VPN network.
        touching = [str(a) for a in policy.networks if not a.is_loopback
                    and any(a.overlaps(ipaddress.ip_network(n)) for n in partly)]
        UI.warning("Devices on " + ", ".join(partly) + " can no longer connect, except the "
                   "addresses inside " + ", ".join(touching) + ", which stay admitted.")
    _applied_note()


# ── the certificate every TLS listener serves ─────────────────────────────────
#
# The terminal half of Settings, Local Network, Certificate. Every decision is the
# framework's (vaf/network/ssl_utils.py): the check, the switch, the restricted authority.
# The terminal writes through it and then asks the running VAF to load the new pair
# (POST /api/network/certificate/reload on the internal channel, the lane `vaf health`
# uses). Exit codes, fit for a renewal hook: 0 served now (or VAF is not running and serves
# it at its next start), 1 refused and nothing changed, 3 changed but the running VAF still
# serves the previous one (restart it).

cert_app = typer.Typer(help="The certificate VAF serves: show it, use your own, go back to VAF's own")
app.add_typer(cert_app, name="cert")
ca_app = typer.Typer(help="VAF's local certificate authority: its fingerprint, renew it",
                     invoke_without_command=True)
app.add_typer(ca_app, name="ca")

_VIA_TERMINAL = "in a terminal on this machine"
EXIT_REFUSED = 1
EXIT_RESTART_NEEDED = 3


def _line(text: str = "") -> None:
    """A line of the certificate commands. Rich's emoji codes are off: they turned the ":CD:"
    of a fingerprint like AB:CD:EF into a disc symbol (measured), and these lines carry
    fingerprints a person compares character by character."""
    UI.console.print(text, markup=True, emoji=False)


def _source_words(source: str) -> str:
    return {"own": "your own", "automatic": "VAF's own", "none": "none (TLS is off)"}.get(
        source, source)


def _date(iso: str) -> str:
    return str(iso or "")[:10]


def _days(count) -> str:
    return "1 day" if count == 1 else f"{count} days"


def _devices_line(issuer: str) -> None:
    """What a changed issuer means for the devices, said before anybody wonders."""
    UI.warning(f"Devices that do not trust {issuer} warn from their next connection on. A "
               "browser that opened VAF by its name before cannot click past that warning "
               f"(it remembers that VAF uses HTTPS): install {issuer} on it first.")


def _print_report(report) -> None:
    from rich.markup import escape

    from vaf.network.ssl_utils import colon_fingerprint
    facts = {k: escape(v) if isinstance(v, str) else v for k, v in (report.facts or {}).items()}
    if facts:
        _line(f"  Subject:      {facts.get('subject', '')}")
        _line(f"  Issuer:       {facts.get('issuer', '')}")
        # Each name is escaped too: a SAN like "[link=...]vafhost[/link]" printed as markup.
        _line(f"  Names:        {', '.join(escape(str(n)) for n in facts.get('names') or []) or '-'}")
        _line(f"  Valid until:  {_date(facts.get('not_after'))} "
              f"({_days(facts.get('days_left'))})")
        _line(f"  Key:          {facts.get('key_type')} {facts.get('key_size') or ''}".rstrip())
        _line(f"  SHA-256:      {colon_fingerprint(facts.get('sha256', ''))}")
        _line(f"  Authority:    {facts.get('authority_subject', '')}")
        _line(f"                {colon_fingerprint(facts.get('authority_sha256', ''))}")
    for item in report.errors:
        _line(f"[red]Error[/red]    {escape(item.text)}")
    for item in report.warnings:
        _line(f"[yellow]Warning[/yellow]  {escape(item.text)}")


def _backend_port_listens() -> bool:
    import socket
    port = int(Config.get("local_network_port", 8001) or 8001)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _ask_running_vaf_to_reload():
    """(outcome, answer): "answered" with the reload result, "not_running", "no_channel" (a
    VAF that serves the backend itself, `vaf run --web`, has no internal channel to ask) or
    "error" (a status other than 200, e.g. a VAF from before this route)."""
    import requests

    from vaf.core.web_interface import internal_api_base
    try:
        response = requests.post(f"{internal_api_base()}/api/network/certificate/reload",
                                 timeout=30)
    except requests.RequestException:
        if Config.get_bool("local_network_tls_enabled", False) and _backend_port_listens():
            return "no_channel", None
        return "not_running", None
    if response.status_code != 200:
        return "error", {"status": response.status_code}
    try:
        return "answered", response.json()
    except ValueError:
        return "error", {"status": response.status_code}


def _finish_with_reload(what: str) -> None:
    """Ask the running VAF to load the pair and exit with the code that says what happened."""
    outcome, answer = _ask_running_vaf_to_reload()
    if outcome == "not_running":
        # Nothing serves the files of the certificate this one replaced.
        from vaf.network.ssl_utils import prune_custom_certificates
        prune_custom_certificates()
        UI.success(f"{what} VAF is not running; it serves this certificate at its next start.")
        raise typer.Exit(0)
    if outcome == "answered" and isinstance(answer, dict):
        if answer.get("source") == "none":
            UI.success(f"{what} TLS is off; VAF serves this certificate once TLS is on.")
            raise typer.Exit(0)
        if answer.get("ok"):
            UI.success(f"{what} The running VAF serves it now, no restart needed.")
            raise typer.Exit(0)
        listeners = answer.get("listeners") or {}
        detail = ", ".join(f"{name} {status}" for name, status in listeners.items())
        refused = answer.get("refused")
        if "unconfirmed" in listeners.values():
            # The load started and had not finished when the wait ended: it may have applied.
            UI.warning(f"{what} The running VAF did not confirm the new certificate in time "
                       f"({detail}); it may already serve it (check with `vaf server cert "
                       "show` on the machine, or in a browser), else restart it "
                       "(`systemctl --user restart vaf`, or quit and start the tray).")
            raise typer.Exit(EXIT_RESTART_NEEDED)
        UI.warning(f"{what} The running VAF still serves the previous certificate"
                   + (f" ({detail})" if detail else "")
                   + (f" ({refused})" if refused else "")
                   + ": restart it (`systemctl --user restart vaf`, or quit and start the tray).")
        raise typer.Exit(EXIT_RESTART_NEEDED)
    if outcome == "no_channel":
        UI.warning(f"{what} A VAF started with `vaf run --web` serves the previous certificate "
                   "until it is restarted: it has no channel this terminal can ask.")
        raise typer.Exit(EXIT_RESTART_NEEDED)
    status = (answer or {}).get("status")
    UI.warning(f"{what} The running VAF did not load it (HTTP {status}); it still serves the "
               "previous certificate: restart it.")
    raise typer.Exit(EXIT_RESTART_NEEDED)


def _peer_trust_line(fingerprint: str) -> None:
    from rich.markup import escape

    from vaf.network.ssl_utils import colon_fingerprint
    # The host the invitation names: one the served certificate covers, which the peer
    # checks (ssl_utils.room_host_choice). With none, the command would be one a peer's name
    # check refuses, so the reason is printed instead.
    host, why = "<address>", None
    try:
        from vaf.network.binding import get_local_network_ip
        from vaf.network.ssl_utils import room_host_choice
        address = str(get_local_network_ip() or "").strip()
        # Without a LAN address there is nothing to choose for (the reasons quote it): the
        # line keeps the placeholder, as when the address cannot be read at all.
        if address:
            chosen, why = room_host_choice(address)
            host = chosen or host
    except Exception:
        pass
    if why:
        # The reason quotes the certificate's names, which come from whoever made it: a
        # SAN like "[link=...]vafhost[/link]" would otherwise print as markup.
        UI.warning("Other machines that joined a room on this VAF cannot reach it now: "
                   f"{escape(why)}.")
        return
    _line("Other machines that joined a room on this VAF trust the new authority again with:")
    _line(f"  vaf a2a trust wss://{escape(host)}{_access_port_suffix()} --ca-fp "
          f"{colon_fingerprint(fingerprint)}")


def _read_file_or_exit(path: Optional[str], what: str) -> bytes:
    if not path:
        return b""
    from pathlib import Path
    try:
        return Path(path).expanduser().read_bytes()
    except OSError as e:
        UI.error(f"Cannot read the {what} file {path}: {e}")
        raise typer.Exit(EXIT_REFUSED)


@cert_app.command("show")
def cert_show():
    """The certificate VAF serves: where it comes from, its names, dates and fingerprints."""
    from vaf.network.ssl_utils import certificate_state, colon_fingerprint
    state = certificate_state()
    if not state["tls"]:
        UI.info("TLS is off, so VAF serves no certificate (`vaf server on` switches it on).")
        return
    from rich.markup import escape
    _line(f"\n[bold]Certificate:[/bold] {_source_words(state['source'])}")
    problem = state.get("problem")
    if problem:
        UI.error(f"Your certificate {state.get('own_path')} cannot be used: {problem['text']} "
                 "VAF serves its own instead; fix the file or run `vaf server cert reset`.")
    facts = state.get("certificate")
    if not facts:
        UI.info("  VAF issues its own certificate at its next start.")
        return
    _line(f"  Subject:      {escape(facts['subject'])}")
    _line(f"  Issuer:       {escape(facts['issuer'])}")
    _line(f"  Names:        {escape(', '.join(facts['names'])) or '-'}")
    left = facts["days_left"]
    until = f"{_date(facts['not_after'])} ({_days(left)})" if left >= 0 else (
        f"{_date(facts['not_after'])} (EXPIRED)")
    _line(f"  Valid until:  {until}")
    _line(f"  SHA-256:      {colon_fingerprint(facts['sha256'])}")
    _line(f"  Authority:    {escape(facts['authority_subject'])}")
    _line(f"                {colon_fingerprint(facts['authority_sha256'])}")
    if state.get("uncovered"):
        UI.warning("It does not name " + escape(", ".join(state["uncovered"])) + ": a device "
                   "that reaches VAF there gets a certificate warning. Open VAF by an address "
                   "or name it carries instead.")
    _line()


@cert_app.command("set")
def cert_set(
    cert: str = typer.Option(..., "--cert", help="The certificate (PEM); may hold the chain and the key"),
    key: Optional[str] = typer.Option(None, "--key", help="Its private key (PEM, without a passphrase)"),
    chain: Optional[str] = typer.Option(None, "--chain", help="The intermediate certificates (PEM)"),
    check: bool = typer.Option(False, "--check", help="Only check it; change nothing"),
):
    """Serve your own certificate. It is checked first, and served at once without a restart.

    Exit code 0 served now (or at the next start, when VAF is not running), 1 refused and
    nothing changed, 3 installed but the running VAF still serves the previous one."""
    from vaf.network.ssl_utils import (CertificateRefused, inspect_certificate,
                                       install_certificate)
    cert_pem = _read_file_or_exit(cert, "certificate")
    key_pem = _read_file_or_exit(key, "key")
    chain_pem = _read_file_or_exit(chain, "chain")
    if check:
        report = inspect_certificate(cert_pem, key_pem, chain_pem)
        _line("\n[bold]Check:[/bold]")
        _print_report(report)
        if report.ok:
            UI.success("The certificate can be used (nothing was changed).")
            return
        raise typer.Exit(EXIT_REFUSED)
    try:
        result = install_certificate(cert_pem, key_pem, chain_pem, via=_VIA_TERMINAL,
                                     reload=False)
    except CertificateRefused as refused:
        _line("\n[bold]Check:[/bold]")
        _print_report(refused.report)
        UI.error("Not installed; nothing was changed.")
        raise typer.Exit(EXIT_REFUSED)
    _line("\n[bold]Check:[/bold]")
    _print_report(result.report)
    if "authority_changes" in result.report.codes():
        _devices_line("its issuer")
        _peer_trust_line(result.new_authority or "")
    _finish_with_reload("Your certificate is installed.")


@cert_app.command("reset")
def cert_reset():
    """Go back to VAF's own certificate, freshly issued by the authority devices already trust."""
    from vaf.network.ssl_utils import fingerprints_match, reset_certificate
    result = reset_certificate(via=_VIA_TERMINAL, reload=False)
    if result.old_authority and result.new_authority and not fingerprints_match(
            result.old_authority, result.new_authority):
        _devices_line("VAF's authority (`vaf server ca`)")
        _peer_trust_line(result.new_authority)
    _finish_with_reload("VAF's own certificate is back.")


@ca_app.callback()
def ca_show(ctx: typer.Context):
    """VAF's local certificate authority: where it is, its fingerprint, whether it is restricted."""
    if ctx.invoked_subcommand is not None:
        return
    from vaf.network.ssl_utils import colon_fingerprint, local_authority
    authority = local_authority()
    if authority is None:
        UI.info("This machine has no local authority yet; VAF creates one when TLS starts.")
        return
    from rich.markup import escape
    _line(f"\n[bold]Local authority:[/bold] {escape(authority['path'])}")
    _line(f"  SHA-256:      {colon_fingerprint(authority['fingerprint'])}")
    _line(f"  Valid until:  {_date(authority['not_after'])}")
    _line("  Restricted:   " + ("yes, to private addresses and names" if authority["restricted"]
                                   else "no"))
    UI.info("Compare this fingerprint with the one a device shows before you trust the "
            "authority on it, never with a page loaded over the connection you are checking.")
    if not authority["restricted"]:
        UI.info("`vaf server ca renew` restricts it to private addresses; every device then "
                "installs it again.")
        _left_out_line(authority.get("left_out_on_renewal") or [])
    _line()


def _left_out_line(left: list) -> None:
    """The machine's names a new authority leaves out of VAF's certificate, and what to use."""
    if not left:
        return
    from rich.markup import escape

    from vaf.network.ssl_utils import mdns_name
    mdns = mdns_name()
    instead = "its address" + (f" or {mdns}" if mdns and mdns not in left else "")
    UI.warning("A new authority leaves " + escape(", ".join(left)) + " out of VAF's "
               f"certificate: devices that open VAF by one of them get a warning; open it by "
               f"{escape(instead)} instead.")


@ca_app.command("renew")
def ca_renew(yes: bool = typer.Option(False, "--yes", help="Do not ask")):
    """Replace the local authority with a new one that only vouches for private names."""
    from vaf.network.ssl_utils import colon_fingerprint, renew_authority, served_certificate
    UI.warning("A new authority replaces the old one. Every device that installed the old one "
               "must install the new one, other machines that joined a room on this VAF trust "
               "its new fingerprint, and the old one can then be removed from your devices. A "
               "browser that opened VAF by its name before cannot click past the warning until "
               "the new authority is installed on it.")
    from vaf.network.ssl_utils import names_a_new_authority_leaves_out
    _left_out_line(names_a_new_authority_leaves_out())
    if not yes and not typer.confirm("Renew the authority now?"):
        UI.info("Nothing was changed.")
        raise typer.Exit(0)
    result = renew_authority(via=_VIA_TERMINAL, reload=False)
    _line(f"  Old SHA-256:  {colon_fingerprint(result.old_authority or '') or '-'}")
    _line(f"  New SHA-256:  {colon_fingerprint(result.new_authority or '')}")
    source = served_certificate().source
    if source == "own":
        UI.success("The authority is renewed. Your own certificate stays served.")
        return
    if source == "none":
        UI.success("The authority is renewed. TLS is off; VAF serves a certificate from it once "
                   "TLS is on.")
        return
    _peer_trust_line(result.new_authority or "")
    _finish_with_reload("The authority is renewed.")
