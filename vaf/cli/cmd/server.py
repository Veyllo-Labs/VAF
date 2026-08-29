# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import platform
import typer
from vaf.core.config import Config
from vaf.cli.ui import UI

app = typer.Typer(help="Manage local network server mode (Hosting/SSL)")


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
            from vaf.network.binding import get_all_local_ips
            ips = get_all_local_ips()
            if ips:
                suffix = _access_port_suffix()
                UI.print("\n[bold]LAN access (integrated HTTPS proxy):[/bold]")
                for _, ip in ips:
                    UI.print(f"  - https://{ip}{suffix}")
        except Exception:
            pass
    UI.print()


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
            UI.info("Open the access port manually, e.g. with firewalld (replace the subnet):")
            UI.info(
                "  sudo firewall-cmd --permanent --zone=public --add-rich-rule="
                f"'rule family=\"ipv4\" source address=\"192.168.1.0/24\" port port=\"{access_port}\" protocol=\"tcp\" accept'"
                " && sudo firewall-cmd --reload"
            )
            UI.info(f"  or with ufw: sudo ufw allow {access_port}/tcp")
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
        from vaf.network.binding import get_all_local_ips
        ips = get_all_local_ips()
    except Exception:
        ips = []
    if ips:
        suffix = "" if access_port == 443 else f":{access_port}"
        UI.print("\n[bold]LAN access once the service is running:[/bold]")
        for _, ip in ips:
            UI.print(f"  - https://{ip}{suffix}")
