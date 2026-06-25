#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Simple script to bump the version number in vaf/version.py
Usage: python scripts/bump_version.py [major|minor|patch|x.y.z]
"""
import sys
import re
from pathlib import Path

def bump_version(part, current_version):
    major, minor, patch = map(int, current_version.split('.'))
    if part == 'major':
        return f"{major + 1}.0.0"
    elif part == 'minor':
        return f"{major}.{minor + 1}.0"
    elif part == 'patch':
        return f"{major}.{minor}.{patch + 1}"
    return current_version

def main():
    root_dir = Path(__file__).parent.parent
    version_file = root_dir / "vaf" / "version.py"
    
    if not version_file.exists():
        print(f"Error: {version_file} not found")
        sys.exit(1)
        
    content = version_file.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    
    if not match:
        print("Error: Could not find version string in vaf/version.py")
        sys.exit(1)
        
    current_version = match.group(1)
    print(f"Current version: {current_version}")
    
    new_version = None
    
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in ['major', 'minor', 'patch']:
            new_version = bump_version(arg, current_version)
        elif re.match(r'^\d+\.\d+\.\d+$', arg):
            new_version = arg
        else:
            print(f"Invalid argument: {arg}")
            print("Usage: python scripts/bump_version.py [major|minor|patch|x.y.z]")
            sys.exit(1)
    else:
        # Interactive mode
        print("\nSelect bump type:")
        print("1. Patch (x.y.Z+1)")
        print("2. Minor (x.Y+1.0)")
        print("3. Major (X+1.0.0)")
        print("4. Custom")
        
        choice = input("\nChoice [1]: ").strip()
        if not choice or choice == '1':
            new_version = bump_version('patch', current_version)
        elif choice == '2':
            new_version = bump_version('minor', current_version)
        elif choice == '3':
            new_version = bump_version('major', current_version)
        elif choice == '4':
            new_version = input("Enter version: ").strip()
            
    if not new_version:
        print("Cancelled.")
        return

    # Update file
    new_content = re.sub(
        r'__version__\s*=\s*"[^"]+"',
        f'__version__ = "{new_version}"',
        content
    )
    
    version_file.write_text(new_content, encoding="utf-8")
    print(f"\n✅ Updated version to: {new_version}")
    print(f"File updated: {version_file}")

if __name__ == "__main__":
    main()
