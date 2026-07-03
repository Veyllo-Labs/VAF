# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import typer
import sys
import os
import platform
import shutil
import subprocess
from vaf.core.config import Config
from vaf.cli.ui import UI
from vaf.core.gpu_detection import detect_all_gpus, get_primary_gpu, get_gpu_support_info
from vaf.core.security_misconfig import collect_security_findings
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

    # GPU Check - Enhanced with multi-vendor support
    UI.print("\n[bold cyan]--- GPU Detection ---[/bold cyan]")
    try:
        gpus = detect_all_gpus()
        primary = get_primary_gpu()
        support_info = get_gpu_support_info()
        
        if not gpus:
            UI.print("[yellow]⚠ No GPU detected. VAF will use CPU mode.[/yellow]")
        else:
            # Show primary GPU
            if primary:
                vendor_colors = {
                    "nvidia": "green",
                    "amd": "magenta",
                    "intel": "blue",
                    "apple": "cyan"
                }
                vendor_icons = {
                    "nvidia": "✓",
                    "amd": "✓",
                    "intel": "✓",
                    "apple": "✓"
                }
                color = vendor_colors.get(primary.vendor, "white")
                icon = vendor_icons.get(primary.vendor, "•")
                
                status = "[green]GPU Compute Available[/green]" if primary.compute_available else "[yellow]GPU Detected (Compute Not Available)[/yellow]"
                
                UI.print(f"[{color}]{icon} {primary.vendor.upper()} GPU:[/{color}] {primary.model}")
                if primary.vram_mb > 0:
                    _suffix = " (unified-memory GPU budget, ~65% of RAM)" if primary.vendor == "apple" else ""
                    UI.print(f"  VRAM: {primary.vram_mb} MB{_suffix}")
                UI.print(f"  Status: {status}")
                UI.print(f"  Recommended Backend: [bold]{support_info['recommended_backend']}[/bold]")
                
                # Show warning if GPU detected but compute not available
                if primary.vendor == "nvidia" and not primary.compute_available:
                    UI.print(f"  [yellow]⚠ CUDA not available. Run 'vaf install-gpu' to install CUDA support.[/yellow]")
                elif primary.vendor == "amd" and not primary.compute_available:
                    UI.print(f"  [yellow]⚠ ROCm not available. Install ROCm drivers for GPU acceleration.[/yellow]")
                elif primary.vendor == "intel" and not primary.compute_available:
                    UI.print(f"  [yellow]⚠ SYCL/oneAPI not available. Install Intel oneAPI for GPU acceleration.[/yellow]")
            
            # Show all GPUs if multiple
            if len(gpus) > 1:
                UI.print(f"\n  [dim]Total GPUs detected: {len(gpus)}[/dim]")
                for i, gpu in enumerate(gpus[1:], 1):
                    UI.print(f"  [dim]  {i+1}. {gpu.vendor.upper()}: {gpu.model}[/dim]")
                    
    except Exception as e:
        UI.error(f"Error checking GPU: {e}")
        import traceback
        UI.print(f"[dim]{traceback.format_exc()}[/dim]")

    # Config
    UI.print("\n[bold cyan]--- Current Config ---[/bold cyan]")
    config = Config.load()
    for k, v in config.items():
        UI.print(f"[bold]{k}:[/bold] {v}")

    # Security summary (non-secret)
    try:
        findings = collect_security_findings(config)
        high = sum(1 for f in findings if str(f.get("severity", "")).lower() == "high")
        medium = sum(1 for f in findings if str(f.get("severity", "")).lower() == "medium")
        UI.print("\n[bold cyan]--- Security Summary ---[/bold cyan]")
        if not findings:
            UI.print("[green]No misconfiguration findings.[/green]")
        else:
            UI.print(f"[yellow]Findings:[/yellow] {len(findings)} total (high={high}, medium={medium})")
    except Exception:
        pass

    # Tip
    UI.print("\n[dim]To repair GPU support, run: 'vaf install-gpu'[/dim]")

@app.command("about")
def about():
    """Show About / License information."""
    UI.panel("文 VAF - Veyllo Agentic Framework", style="bold cyan")
    
    logo = """
   O))         O))       O))))))))
    O))       O))))      O))      
     O))     O))  O))    O))      
      O))   O))    O))   O))))))  
       O)) O)) )))) O))  O))      
        O))))        O)) O))      
         O))          O))O))     (OO ) 
    """
    UI.print(f"[cyan]{logo}[/cyan]")
    
    UI.print(f"[bold]Version:[/bold] {importlib.metadata.version('vaf') if importlib.util.find_spec('vaf') else 'Dev'}")
    UI.print("[bold]Copyright:[/bold] (c) 2026 Veyllo GmbH")
    UI.print("[bold]Credits:[/bold] Built with ❤️ by Veyllo Labs")
    UI.print()
    
    UI.print("[bold]License:[/bold]")
    UI.print("Dual-licensed: [bold]GNU AGPL-3.0-or-later[/bold] (open source) or a [bold]Commercial License[/bold].")
    UI.print("See [bold]LICENSE[/bold], [bold]LICENSING.md[/bold], and [bold]COMMERCIAL.md[/bold] for full terms.")
    UI.print("Commercial / OEM licensing: [bold]legal@veyllo.io[/bold]")
    UI.print()
    
    UI.print("[bold]Links:[/bold]")
    UI.print("🌐 Website: https://veyllo.io")
    UI.print("💻 GitHub:  https://github.com/Veyllo-Labs/VAF")

@app.command("install-gpu")
def install_gpu():
    """Force reinstall of GPU-accelerated dependencies."""
    from vaf.core.gpu_detection import get_primary_gpu
    
    system = platform.system()
    UI.event("System", f"Starting GPU Setup for {system}...", style="warning")
    
    # Detect GPU
    primary_gpu = get_primary_gpu()
    
    if not primary_gpu:
        UI.warning("No GPU detected. Installing CPU-only version.")
        gpu_type = "cpu"
    else:
        gpu_type = primary_gpu.vendor
        UI.event("Info", f"Detected {primary_gpu.vendor.upper()} GPU: {primary_gpu.model}", style="dim")
    
    # 1. Uninstall
    UI.event("Setup", "Uninstalling existing llama-cpp-python...", style="dim")
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "llama-cpp-python"], capture_output=True)
    
    # 2. Reinstall with appropriate backend
    UI.event("Setup", "Installing Hardware-Accelerated llama-cpp-python...", style="highlight")
    
    env = os.environ.copy()
    pip_cmd = [sys.executable, "-m", "pip", "install", "llama-cpp-python", "--no-cache-dir", "--force-reinstall"]
    
    try:
        if system == "Darwin":
            # macOS - Apple Silicon (Metal)
            env["CMAKE_ARGS"] = "-DGGML_METAL=on"
            UI.event("Info", "Targeting Apple Metal (M1/M2/M3)...", style="dim")
            
        elif system == "Linux":
            if gpu_type == "amd":
                # AMD - ROCm
                env["CMAKE_ARGS"] = "-DGGML_ROCM=on"
                UI.event("Info", "Targeting AMD ROCm (Linux)...", style="dim")
                UI.print("[yellow]Note: Ensure ROCm drivers are installed. See: https://rocm.docs.amd.com/[/yellow]")
            elif gpu_type == "intel":
                # Intel - SYCL (experimental)
                env["CMAKE_ARGS"] = "-DGGML_SYCL=on"
                UI.event("Info", "Targeting Intel SYCL (Linux)...", style="dim")
                UI.print("[yellow]Note: Ensure Intel oneAPI is installed. See: https://www.intel.com/content/www/us/en/developer/tools/oneapi/overview.html[/yellow]")
            else:
                # Default to CUDA for NVIDIA or fallback
                env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
                pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
                UI.event("Info", "Targeting CUDA (Linux)...", style="dim")
            
        elif system == "Windows":
            if gpu_type == "amd":
                # AMD on Windows - OpenCL (limited support)
                env["CMAKE_ARGS"] = "-DGGML_OPENCL=on"
                UI.event("Info", "Targeting AMD OpenCL (Windows)...", style="dim")
                UI.print("[yellow]Note: OpenCL support is limited. Consider using Linux with ROCm for better AMD support.[/yellow]")
            elif gpu_type == "intel":
                # Intel Arc on Windows - SYCL
                env["CMAKE_ARGS"] = "-DGGML_SYCL=on"
                UI.event("Info", "Targeting Intel SYCL (Windows)...", style="dim")
                UI.print("[yellow]Note: Ensure Intel oneAPI is installed. See: https://www.intel.com/content/www/us/en/developer/tools/oneapi/overview.html[/yellow]")
            else:
                # Default to CUDA for NVIDIA or fallback
                env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
                pip_cmd.extend(["--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"])
                UI.event("Info", "Targeting CUDA (Windows)...", style="dim")
            
        else:
            UI.event("Warning", f"Unknown platform {system}, attempting standard install...", style="warning")

        subprocess.check_call(pip_cmd, env=env)
        UI.event("Success", "GPU Setup Complete! Please restart vaf.", style="success")
        
        if primary_gpu and not primary_gpu.compute_available:
            UI.print(f"\n[yellow]⚠ Warning: GPU detected but compute drivers may not be installed.[/yellow]")
            if primary_gpu.vendor == "nvidia":
                UI.print(f"[yellow]   Install CUDA Toolkit from: https://developer.nvidia.com/cuda-downloads[/yellow]")
            elif primary_gpu.vendor == "amd":
                UI.print(f"[yellow]   Install ROCm from: https://rocm.docs.amd.com/[/yellow]")
            elif primary_gpu.vendor == "intel":
                UI.print(f"[yellow]   Install Intel oneAPI from: https://www.intel.com/content/www/us/en/developer/tools/oneapi/overview.html[/yellow]")
        
    except subprocess.CalledProcessError as e:
        UI.error(f"Failed to install GPU version for {system}.")
        UI.print(f"[dim]Error: {e}[/dim]")
        UI.print(f"[yellow]Ensure build tools are installed (cmake, compiler, GPU drivers).[/yellow]")
