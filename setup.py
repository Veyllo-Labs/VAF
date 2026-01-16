"""
Setup script for VAF (Veyllo Agentic Framework)
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding="utf-8") if readme_file.exists() else ""

# Read requirements
requirements_file = Path(__file__).parent / "requirements.txt"
requirements = []
if requirements_file.exists():
    with open(requirements_file, "r", encoding="utf-8") as f:
        requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

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
    author="Veyllo Labs",
    url="https://github.com/Veyllo-Labs/Veyllo-App",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "vaf=vaf.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)

