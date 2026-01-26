from pathlib import Path
import os

# Simulate web_server.py location: vaf/core/web_server.py
# So we need to emulate that depth if we were running FROM there, but here we are running from root probably.
# Let's just implement the exact logic I wrote in web_server.py but adapted to run from CWD or wherever.

# Logic in web_server.py:
# project_root = Path(__file__).parent.parent.parent

# Let's pretend this script is at /Users/m.c.elsner/VAF/vaf/core/debug_probe.py
pseudo_file_path = Path("/Users/m.c.elsner/VAF/vaf/core/web_server.py")
project_root = pseudo_file_path.parent.parent.parent
models_dir = project_root / "models"

print(f"Computed project_root: {project_root}")
print(f"Computed models_dir: {models_dir}")
print(f"Exists? {models_dir.exists()}")

if models_dir.exists():
    print("Contents:")
    for f in models_dir.glob("*.gguf"):
        print(f" - {f.name}")
else:
    print("Models dir not found via computed path.")

# Check CWD
print(f"CWD: {os.getcwd()}")
print(f"CWD models exists? {Path('models').exists()}")
