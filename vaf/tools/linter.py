"""
Linter Tool

Checks code files for syntax errors and linting issues.
Supports multiple languages: Python (ruff), JavaScript (eslint), etc.
"""

import os
import sys
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, List

from vaf.tools.base import BaseTool
from vaf.core.platform import Platform


class LinterTool(BaseTool):
    name = "linter"
    description = (
        "Check code files for syntax errors and linting issues. "
        "Supports Python (ruff), JavaScript (eslint), and other languages. "
        "Returns detailed error messages that can be used to fix issues."
    )
    coder_only = True  # Only available to coding agent

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to file or directory to lint (relative to project root or absolute)"
            },
            "file_type": {
                "type": "string",
                "description": "Optional: File type hint (python, javascript, java, etc.). Auto-detected if not provided."
            }
        },
        "required": ["path"]
    }

    # Linter commands by file extension
    LINTERS = {
        '.py': {
            'command': ['ruff', 'check', '--output-format=text'],
            'install': 'pip install ruff',
            'name': 'ruff'
        },
        '.js': {
            'command': ['npx', '--yes', 'eslint', '--format=compact'],
            'install': 'npm install -g eslint',
            'name': 'eslint',
            'requires_node': True
        },
        '.ts': {
            'command': ['npx', '--yes', 'eslint', '--format=compact'],
            'install': 'npm install -g eslint @typescript-eslint/parser',
            'name': 'eslint',
            'requires_node': True
        },
        '.java': {
            'command': ['javac', '-Xlint:all'],
            'install': 'Java compiler (javac) must be installed',
            'name': 'javac',
            'requires_java': True
        }
    }

    @classmethod
    def detect_file_type(cls, file_path: str) -> Optional[str]:
        """Detect file type from extension."""
        ext = Path(file_path).suffix.lower()
        return ext if ext in cls.LINTERS else None

    @classmethod
    def is_linter_available(cls, file_type: str) -> tuple[bool, Optional[str]]:
        """
        Check if linter is available for the given file type.
        Returns: (is_available, error_message)
        """
        if file_type not in cls.LINTERS:
            return False, f"No linter configured for file type: {file_type}"

        linter_config = cls.LINTERS[file_type]
        linter_name = linter_config['name']
        command = linter_config['command'][0]  # First command is the linter

        # Check if Node.js is required
        if linter_config.get('requires_node'):
            if not shutil.which('node'):
                return False, (
                    f"{linter_name} requires Node.js. "
                    f"Install: https://nodejs.org/ or use your system package manager"
                )
            if not shutil.which('npx'):
                return False, f"{linter_name} requires npx (comes with Node.js)"

        # Check if Java is required
        if linter_config.get('requires_java'):
            if not shutil.which('javac'):
                return False, (
                    f"{linter_name} requires Java compiler (javac). "
                    f"Install JDK from: https://adoptium.net/"
                )

        # Check if linter command exists
        if command == 'ruff':
            # Ruff is a CLI tool, check if it's available
            if not shutil.which('ruff'):
                return False, (
                    f"{linter_name} is not installed. "
                    f"Install: {linter_config['install']}"
                )
        elif command in ['npx', 'javac']:
            # System commands
            if not shutil.which(command):
                return False, (
                    f"{linter_name} is not available. "
                    f"Install: {linter_config['install']}"
                )
        else:
            # Generic check
            if not shutil.which(command):
                return False, (
                    f"{linter_name} is not available. "
                    f"Install: {linter_config['install']}"
                )

        return True, None

    def run(self, **kwargs) -> str:
        path = kwargs.get('path', '').strip()
        file_type_hint = kwargs.get('file_type', '').strip().lower()

        if not path:
            return "[ERROR] linter: missing path parameter"

        # Resolve path (can be relative or absolute)
        if os.path.isabs(path):
            target_path = Path(path)
        else:
            # Assume relative to current working directory
            target_path = Path(path).resolve()

        if not target_path.exists():
            return f"[ERROR] linter: path does not exist: {path}"

        # Detect file type
        if file_type_hint:
            # Use hint if provided
            ext = f".{file_type_hint}" if not file_type_hint.startswith('.') else file_type_hint
        else:
            # Auto-detect from extension
            ext = target_path.suffix.lower() if target_path.is_file() else None

        if not ext or ext not in self.LINTERS:
            if target_path.is_file():
                return f"[INFO] linter: No linter configured for file type: {ext or 'unknown'}"
            else:
                # For directories, try to find files with known extensions
                return self._lint_directory(target_path)

        # Check if linter is available
        is_available, error_msg = self.is_linter_available(ext)
        if not is_available:
            return f"[INFO] linter: {error_msg}"

        # Run linter
        linter_config = self.LINTERS[ext]
        command = linter_config['command'].copy()

        # Add file path to command
        command.append(str(target_path))

        try:
            # Run linter
            import platform
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "cwd": target_path.parent if target_path.is_file() else target_path,
                "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}
            }
            if platform.system() == "Windows":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.run(command, **run_kwargs)

            output_lines = []
            
            # Ruff returns exit code 0 if no issues, 1 if issues found
            # ESLint similar behavior
            if proc.returncode == 0:
                output_lines.append(f"✓ {linter_config['name']}: No issues found")
            else:
                # Errors or warnings found
                if proc.stdout:
                    output_lines.append(f"⚠ {linter_config['name']} found issues:")
                    output_lines.append(proc.stdout)
                if proc.stderr:
                    output_lines.append(f"Errors:")
                    output_lines.append(proc.stderr)

            # If no output but non-zero exit, still report
            if proc.returncode != 0 and not proc.stdout and not proc.stderr:
                output_lines.append(f"⚠ {linter_config['name']}: Check failed (exit code {proc.returncode})")

            return "\n".join(output_lines) if output_lines else "OK"

        except subprocess.TimeoutExpired:
            return f"[ERROR] linter: timeout after 30s"
        except FileNotFoundError:
            return f"[ERROR] linter: {linter_config['name']} command not found. Install: {linter_config['install']}"
        except Exception as e:
            return f"[ERROR] linter: {e}"

    def _lint_directory(self, directory: Path) -> str:
        """Lint all files in a directory with known extensions."""
        results = []
        found_files = False

        for ext, linter_config in self.LINTERS.items():
            # Check if linter is available
            is_available, error_msg = self.is_linter_available(ext)
            if not is_available:
                continue

            # Find files with this extension
            files = list(directory.rglob(f"*{ext}"))
            if not files:
                continue

            found_files = True
            command = linter_config['command'].copy()
            command.append(str(directory))

            try:
                import platform
                run_kwargs = {
                    "capture_output": True,
                    "text": True,
                    "timeout": 60,
                    "cwd": str(directory),
                    "env": {**os.environ, "PYTHONIOENCODING": "utf-8"}
                }
                if platform.system() == "Windows":
                    run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                proc = subprocess.run(command, **run_kwargs)

                if proc.returncode == 0:
                    results.append(f"✓ {linter_config['name']}: No issues in {len(files)} {ext} files")
                else:
                    if proc.stdout:
                        results.append(f"⚠ {linter_config['name']} ({ext}):")
                        results.append(proc.stdout)
                    if proc.stderr:
                        results.append(f"Errors ({ext}):")
                        results.append(proc.stderr)

            except Exception as e:
                results.append(f"[ERROR] {linter_config['name']} ({ext}): {e}")

        if not found_files:
            return "[INFO] linter: No lintable files found in directory"

        return "\n\n".join(results) if results else "OK"

