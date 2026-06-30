# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Setup script for VAF (Veyllo Agentic Framework)
"""

from setuptools import setup, find_packages
from setuptools.command.install import install
from setuptools.command.develop import develop
from pathlib import Path
import subprocess
import platform
import os

def run_setup_scripts():
    """Run platform-specific setup scripts after installation."""
    # Skip when an outer installer (install.sh / install.ps1 / bootstrap) already drives the
    # platform setup, or when we are in a nested setup run. Otherwise `pip install -e .` re-runs
    # setup_mac.sh / setup_win.ps1 and redoes brew/venv/alias/.app work the installer just did
    # (the macOS double-path issue).
    if os.environ.get("VAF_SKIP_POSTINSTALL") == "1" or os.environ.get("VAF_SKIP_PIP_INSTALL") == "1":
        print("Skipping post-install platform script (installer is handling setup).")
        return

    system = platform.system()
    project_root = Path(__file__).parent

    # Set environment variable to prevent loops
    os.environ["VAF_SKIP_PIP_INSTALL"] = "1"

    try:
        if system == "Windows":
            print("\n🪟 Windows detected. Running setup_win.ps1...")
            script_path = project_root / "scripts" / "setup_win.ps1"
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script_path)], check=False)
        elif system == "Darwin":
            print("\n🍎 macOS detected. Running setup_mac.sh...")
            script_path = project_root / "scripts" / "setup_mac.sh"
            subprocess.run(["bash", str(script_path)], check=False)
        elif system == "Linux":
            print("\n🐧 Linux detected. Skipping automated setup (manual steps may be required).")
    except Exception as e:
        print(f"⚠️  Post-installation scripts failed: {e}")

class PostInstallCommand(install):
    def run(self):
        install.run(self)
        run_setup_scripts()

class PostDevelopCommand(develop):
    def run(self):
        develop.run(self)
        run_setup_scripts()

# Read README for long description
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding="utf-8") if readme_file.exists() else ""

# ---------------------------------------------------------------------------
# Dependencies
#
# `pip install vaf` installs only BASE_REQUIRES — the minimal set needed to use
# VAF as a headless library (`from vaf import Agent`). Everything else lives in
# named extras so the library stays slim. The full desktop/server product gets
# the complete set via `pip install vaf[all]` or `pip install -r requirements.txt`
# (the latter is what install.sh uses, so the desktop install flow is unaffected).
# requirements.txt remains the canonical full list for the installer.
# ---------------------------------------------------------------------------
BASE_REQUIRES = [
    # CLI & UI
    "typer>=0.9.0", "rich>=13.0.0", "prompt_toolkit>=3.0.0", "colorama>=0.4.0",
    "shellingham>=1.5.0", "psutil>=5.9.0",
    # API & networking (core)
    "requests>=2.31.0", "httpx>=0.27.0", "PyGithub>=2.1.1",
    "beautifulsoup4>=4.12.0", "markdownify>=1.0.0",
    # LLM providers + model download
    "huggingface_hub[hf_xet]>=0.20.0", "tqdm>=4.65.0",
    "openai>=1.12.0", "anthropic>=0.18.0", "google-genai>=1.0.0",
    # Automation / scheduling
    "schedule>=1.2.0", "inquirer>=3.1.0",
    # IANA tz database for zoneinfo on Windows (no system tz db); used by vaf/core/user_time.py
    "tzdata>=2024.1",
    # Linting (used by the built-in linter tool)
    "ruff>=0.1.0",
    # Schema validation for tool-input repair (core tool-dispatch path)
    "jsonschema>=4.18.0",
    # PEP 440 version parsing/comparison for the self-updater (vaf/cli/cmd/update.py)
    "packaging>=23.0",
    # Auth + encrypted credential store (core infra)
    "argon2-cffi>=23.1.0", "keyring>=24.0.0", "filelock>=3.12.0",
    "PyJWT>=2.8.0", "pyotp>=2.9.0", "qrcode[pil]>=7.4.0", "cryptography>=41.0.0",
]

EXTRAS = {
    "server": [
        "fastapi>=0.109.0", "uvicorn[standard]>=0.27.0",
        "websockets>=12.0", "pydantic>=2.0.0",
    ],
    "discord": ["discord.py>=2.3.2"],
    "telegram": ["python-telegram-bot>=21.0"],
    "desktop": [
        "pystray>=0.19.5", "pillow>=10.0.0", "pywebview>=4.3.0",
        "pyobjc-framework-Cocoa>=9.0; sys_platform == 'darwin'",
        # PySide6 (LGPLv3) is the official Qt for Python binding. VAF is AGPL-3.0, so PyQt6
        # (GPLv3) would be license-compatible; we pick PySide6 because its weak copyleft keeps
        # the Qt *binding* itself unencumbered (dynamically linked, user-replaceable). VAF's own
        # AGPL obligations are independent of this choice. PySide6 bundles Qt WebEngine.
        "PySide6>=6.7.0; sys_platform == 'linux'",
        "qtpy>=2.0.0; sys_platform == 'linux'",
    ],
    "memory": [
        "sqlalchemy[asyncio]>=2.0.0", "asyncpg>=0.29.0", "pgvector>=0.2.0",
        "sentence-transformers>=2.2.0", "onnxruntime>=1.16.0",
        "tokenizers>=0.15.0", "numpy>=1.24.0", "redis>=5.0.0",
    ],
    "speech": ["SpeechRecognition>=3.10.0", "pyaudio>=0.2.14"],
    "browser": [
        "browser-use>=0.12.9; python_version >= '3.11'",
        "playwright>=1.49.0; python_version >= '3.11'",
    ],
    "pdf": [
        "PyPDF2>=3.0.0", "pdfplumber>=0.11.0", "pycryptodome>=3.15.0",
        "pdf2image>=1.16.0", "pytesseract>=0.3.10",
    ],
    "docs": ["python-docx>=1.1.0", "openpyxl>=3.1.0", "python-pptx>=0.6.21"],
    "dev": ["pytest>=7.0.0", "pytest-mock>=3.10.0"],
}
# `all` = everything (parity with requirements.txt / the desktop product).
EXTRAS["all"] = sorted({dep for deps in EXTRAS.values() for dep in deps})

# Read version from vaf/version.py to avoid importing the package
version_dict = {}
version_file = Path(__file__).parent / "vaf" / "version.py"
if version_file.exists():
    with open(version_file, "r", encoding="utf-8") as f:
        exec(f.read(), version_dict)
    version = version_dict["__version__"]
else:
    version = "0.0.0"

setup(
    name="vaf",
    version=version,
    description="VAF - Veyllo Agentic Framework: Local AI tool for developers",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Veyllo GmbH",
    author_email="legal@veyllo.io",
    license="AGPL-3.0-or-later",
    url="https://github.com/Veyllo-Labs/VAF",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=BASE_REQUIRES,
    extras_require=EXTRAS,
    include_package_data=True,
    package_data={
        "vaf": ["py.typed", "media/*", "media/**/*"],
    },
    cmdclass={
        'install': PostInstallCommand,
        'develop': PostDevelopCommand,
    },
    entry_points={
        "console_scripts": [
            "vaf=vaf.main:main",
        ],
    },
    classifiers=[
        # License is declared via the SPDX `license` expression above (PEP 639);
        # the deprecated `License ::` classifier is intentionally omitted (setuptools
        # warns on, and PyPI rejects, specifying both).
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)

