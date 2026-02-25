import platform
import typer
from vaf.core.config import Config
from vaf.cli.ui import UI

app = typer.Typer(help="Manage local network server mode (Hosting/SSL)")

@app.command(name="on")
def server_on():
    """Enable local network hosting (bind to 0.0.0.0). Uses HTTP so the frontend starts reliably; enable TLS in Settings if you need HTTPS."""
    Config.set("local_network_enabled", True)
    Config.set("local_network_tls_enabled", False)
    UI.success("✓ Local network hosting enabled (HTTP).")
    UI.info("VAF will listen on 0.0.0.0. From other devices: http://<this-PC-IP>:3000 (e.g. http://192.168.2.114:3000).")
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
                port = 443
                if Config.get("local_network_tls_enabled"):
                    port = Config.get("local_network_https_port", 443)
                    if platform.system() == "Windows" and port == 443:
                        port = 8443
                suffix = "" if port == 443 else f":{port}"
                UI.print("\n[bold]LAN access (integrated HTTPS proxy):[/bold]")
                for _, ip in ips:
                    UI.print(f"  - https://{ip}{suffix}")
        except Exception:
            pass
    UI.print()
