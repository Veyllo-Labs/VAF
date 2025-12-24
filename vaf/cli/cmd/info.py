import typer
import sys
import os
import platform
import shutil
import subprocess
from vaf.core.config import Config
from vaf.cli.ui import UI
import importlib.metadata

app = typer.Typer()

@app.command()
def info():
    """Display system and debugging information."""
    UI.panel("Veyllo Diagnostic Tool", style="bold magenta")
    
    # Python Info
    UI.print(f"[bold]Python:[/bold] {sys.version.split()[0]} ({platform.architecture()[0]})")
    UI.print(f"[bold]Platform:[/bold] {platform.system()} {platform.release()}")
    
    # Dependencies
    try:
        llama_ver = importlib.metadata.version("llama-cpp-python")
        UI.print(f"[bold]llama-cpp-python:[/bold] {llama_ver}")
    except:
        UI.print("[bold]llama-cpp-python:[/bold] [red]Not Installed[/red]")

    # GPU Check
    UI.print("\n[bold cyan]--- GPU Detection ---[/bold cyan]")
    try:
        if shutil.which("nvidia-smi"):
            result = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
            if result.returncode == 0:
                UI.print("[green]✓ NVIDIA GPU detected via nvidia-smi[/green]")
                # Print first few lines of output
                for line in result.stdout.splitlines()[:10]:
                    UI.print(f"  [dim]{line}[/dim]")
            else:
                UI.error("nvidia-smi found but returned error code.")
        else:
             UI.print("[yellow]⚠ nvidia-smi not found in PATH.[/yellow]")
    except Exception as e:
        UI.error(f"Error checking GPU: {e}")

    # Config
    UI.print("\n[bold cyan]--- Current Config ---[/bold cyan]")
    config = Config.load()
    for k, v in config.items():
        UI.print(f"[bold]{k}:[/bold] {v}")

    # Tip
    UI.print("\n[dim]To repair GPU support, run: 'vaf install-gpu'[/dim]")

@app.command("install-gpu")
def install_gpu():
    """Force reinstall of GPU-accelerated dependencies."""
    system = platform.system()
    UI.event("System", f"Starting GPU Repair for {system}...", style="warning")
    
    # 1. Uninstall
    UI.event("Setup", "Uninstalling existing llama-cpp-python...", style="dim")
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "llama-cpp-python"], capture_output=True)
    
    # 2. Reinstall
    UI.event("Setup", "Installing Hardware-Accelerated llama-cpp-python...", style="highlight")
    
    env = os.environ.copy()
    pip_cmd = [sys.executable, "-m", "pip", "install", "llama-cpp-python", "--no-cache-dir", "--force-reinstall"]
    
    try:
        if system == "Darwin":
            # macOS - Apple Silicon (Metal)
            env["CMAKE_ARGS"] = "-DGGML_METAL=on"
            UI.event("Info", "Targeting Apple Metal (M1/M2/M3)...", style="dim")
            # Build from source, no wheel URL needed usually for Metal auto-discovery, 
            # but defining CMAKE_ARGS enforces it.
            
        elif system == "Linux":
            # Linux - Defaulting to CUDA for now, theoretically could check for ROCm
            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
            # Linux often uses the same prebuilt wheels repo or builds from source
            pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
            UI.event("Info", "Targeting CUDA (Linux)...", style="dim")
            
        elif system == "Windows":
            # Windows - CUDA
            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
            pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
            UI.event("Info", "Targeting CUDA (Windows)...", style="dim")
            
        else:
            UI.event("Warning", f"Unknown platform {system}, attempting standard install...", style="warning")

        subprocess.check_call(pip_cmd, env=env)
        UI.event("Success", "GPU Setup Complete! Please restart vaf.", style="success")
        
    except subprocess.CalledProcessError:
        UI.error(f"Failed to install GPU version for {system}. Ensure build tools (cmake, xcode, cuda) are installed.")
