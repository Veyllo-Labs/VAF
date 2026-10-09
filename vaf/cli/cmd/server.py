# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import platform
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


def _access_port_suffix() -> str:
    """URL port suffix for the LAN access port, empty when it is plain 443.

    Cosmetic by definition, and its callers print it AFTER changing config, so
    it must never be the reason a command ends in a traceback with no
    confirmation. An unresolvable port falls back to the usual effective one.
    """
    try:
        from vaf.network.binding import resolve_lan_access_ports
        access_port, _ = resolve_lan_access_ports(wait_for_proxy=False)
    except Exception:
        access_port = 8443
    return "" if access_port == 443 else f":{access_port}"


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

    if enabled:
        try:
            _print_access(_access_port_suffix())
        except Exception as e:
            UI.warning(f"Could not read the network interfaces: {e}")
    UI.print()


def _print_access(suffix: str) -> None:
    """Interfaces with their access URL, the admitted networks, and what was refused."""
    from vaf.network.binding import (REFUSAL_REASONS, access_addresses, inbound_policy,
                                     local_interfaces)
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
    local = "the VPN networks" if policy.vpn_only else "the local networks"
    UI.print(f"\n[bold]Admitted:[/bold] this machine, {local}"
             + (f", {', '.join(policy.allowed)}" if policy.allowed else ""))
    UI.print(f"[bold]VPN only:[/bold] {'[yellow]YES[/yellow]' if policy.vpn_only else 'no'}")
    for value, code in policy.refused:
        UI.warning(f"Ignored entry {value}: {REFUSAL_REASONS.get(code, code)}")


@app.command(name="provision")
def server_provision(
    open_firewall: bool = typer.Option(
        True,
        "--firewall/--no-firewall",
        help="Open the OS firewall for the LAN access port (subnet-scoped)",
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

    _enable_lan_keys()
    Config.set("server_mode", True)
    UI.success("Server mode enabled (LAN hosting with TLS, locked on).")

    try:
        from vaf.network.ssl_utils import ensure_ssl_certificates
        cert_path, _key_path = ensure_ssl_certificates()
        if cert_path:
            UI.info(f"TLS certificate ready: {cert_path}")
    except Exception as e:
        UI.warning(f"TLS certificate preparation failed ({e}); it is retried on service start.")

    from vaf.network.binding import resolve_lan_access_ports
    access_port, frontend_port = resolve_lan_access_ports(wait_for_proxy=False)

    if open_firewall:
        try:
            from vaf.network.firewall import setup_firewall
            outcome = setup_firewall(access_port, frontend_port)
        except Exception as e:
            outcome = False
            UI.warning(f"Firewall setup error: {e}")
        if outcome == "present":
            UI.success(f"Firewall rule already in place for port {access_port}.")
        elif outcome:
            UI.success(f"Firewall opened for port {access_port} (LAN subnet only).")
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
        from vaf.network.binding import access_addresses
        reachable = access_addresses()
    except Exception:
        reachable = []
    if reachable:
        suffix = "" if access_port == 443 else f":{access_port}"
        UI.print("\n[bold]Access once the service is running:[/bold]")
        for iface in reachable:
            UI.print(f"  - https://{iface.ip}{suffix}  ({iface.name})")


# ── who is admitted besides the local networks ─────────────────────────────
#
# The CLI half of the "Remote access (VPN)" settings. Both write the same two keys and
# go through the same check (binding.normalize_allowed_networks). The access check reads
# the file on every request; a running VAF re-applies the firewall within about 25
# seconds (the tray polls the file), without a restart. Named boundary: like the rest of this group there is no
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
    UI.info("The access check uses this at once; a running VAF re-applies the firewall within about 25 seconds. Nothing restarts.")


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
        _applied_note()
        return
    from vaf.network.binding import inbound_policy, local_interfaces
    policy = inbound_policy(detect_vpn=True)
    UI.success("VPN only is on.")
    if policy.vpn:
        UI.info("Admitted VPN networks: " + ", ".join(policy.vpn))
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
        touching = [e for e in policy.allowed
                    if any(ipaddress.ip_network(e).overlaps(ipaddress.ip_network(n)) for n in partly)]
        UI.warning("Devices on " + ", ".join(partly) + " can no longer connect, except the "
                   "addresses your own entries admit (" + ", ".join(touching) + ").")
    _applied_note()
