# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Code Search Tool - Semantic code search
Search through codebase with intelligent pattern matching
"""
import os
import re
from pathlib import Path
from typing import Dict, Any, List

from vaf.tools.base import BaseTool

# Default file extensions
ALL_CODE_EXTENSIONS = {
    '.py', '.pyi', '.js', '.jsx', '.mjs', '.ts', '.tsx',
    '.rs', '.go', '.java', '.c', '.h', '.cpp', '.hpp',
    '.rb', '.php', '.swift', '.kt', '.scala',
    '.sh', '.bash', '.yaml', '.yml', '.json', '.md', '.html', '.css'
}

# Directories to skip
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", 
    ".venv", "venv", "target", "build", "dist", "out",
    ".vaf", ".cache", ".tox", "vendor",
}

# Symbol patterns by extension
SYMBOL_PATTERNS = {
    ".py": [r"^(?:async\s+)?def\s+(\w+)", r"^class\s+(\w+)"],
    ".js": [r"function\s+(\w+)", r"class\s+(\w+)", r"const\s+(\w+)\s*="],
    ".ts": [r"function\s+(\w+)", r"class\s+(\w+)", r"interface\s+(\w+)"],
    ".rs": [r"fn\s+(\w+)", r"struct\s+(\w+)", r"enum\s+(\w+)"],
    ".go": [r"func\s+(\w+)", r"type\s+(\w+)\s+struct"],
}


class CodeSearchTool(BaseTool):
    """Search through the codebase for code patterns."""
    
    name = "codesearch"
    permission_level = "read"
    side_effect_class = "none"
    coder_only = True  # Only available to Coder Sub-Agent
    description = """Search through the codebase for code patterns, function definitions, and text.

Use this tool to:
- Find function/class definitions
- Search for specific code patterns
- Locate usages of variables/imports
- Find files containing specific content

Search types:
- 'text': Simple text search (default)
- 'regex': Regular expression search
- 'symbol': Find function/class/variable definitions

Examples:
- codesearch(query="def process_data", search_type="symbol")
- codesearch(query="TODO|FIXME", search_type="regex")
- codesearch(query="import requests", search_type="text")
- codesearch(query="authenticate", path="src/")"""
    
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (text, regex pattern, or symbol name)"
            },
            "search_type": {
                "type": "string",
                "description": "Type of search: text, regex, or symbol (default: text)"
            },
            "path": {
                "type": "string",
                "description": "Directory or file to search in (default: current directory)"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default: 30)"
            }
        },
        "required": ["query"]
    }
    
    def run(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        search_type = kwargs.get("search_type", "text")
        path = kwargs.get("path", ".")
        max_results = kwargs.get("max_results", 30)
        context_lines = kwargs.get("context_lines", 2)
        
        if not query:
            return "Error: No search query provided"
        
        # Get search path
        search_path = Path(path).resolve()
        if not search_path.exists():
            return f"Error: Path not found: {search_path}"
        
        # Get files
        files = self._get_files(search_path)
        if not files:
            return "No matching files found in the directory"
        
        # Execute search
        if search_type == "regex":
            results = self._search_regex(query, files, context_lines, max_results)
        elif search_type == "symbol":
            results = self._search_symbol(query, files, context_lines, max_results)
        else:
            results = self._search_text(query, files, context_lines, max_results)
        
        # Format output
        if not results:
            return f"No results found for: {query}"
        
        output = [f"Search: '{query}' ({search_type})"]
        output.append(f"Files searched: {len(files)}")
        output.append(f"Results: {len(results)}")
        output.append("=" * 50)
        
        for i, match in enumerate(results, 1):
            filepath = match.get("file", "?")
            line_num = match.get("line", "?")
            matched = match.get("match", "")
            
            output.append(f"\n[{i}] {filepath}:{line_num}")
            output.append(f"    {matched}")
        
        return "\n".join(output)
    
    def _get_files(self, path: Path) -> List[Path]:
        """Get all code files."""
        files = []
        
        if path.is_file():
            if path.suffix in ALL_CODE_EXTENSIONS:
                return [path]
            return []
        
        for item in path.rglob("*"):
            if item.is_file():
                # Skip excluded directories
                skip = False
                for parent in item.parents:
                    if parent.name in SKIP_DIRS or parent.name.startswith("."):
                        skip = True
                        break
                
                if skip:
                    continue
                
                if item.suffix in ALL_CODE_EXTENSIONS:
                    files.append(item)
        
        return files
    
    def _search_text(self, query: str, files: List[Path], context_lines: int, max_results: int) -> List[Dict]:
        """Simple text search."""
        results = []
        query_lower = query.lower()
        
        for filepath in files:
            if len(results) >= max_results:
                break
            
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                for i, line in enumerate(lines):
                    if query_lower in line.lower():
                        results.append({
                            "file": str(filepath),
                            "line": i + 1,
                            "match": line.strip(),
                        })
                        
                        if len(results) >= max_results:
                            break
            except Exception:
                continue
        
        return results
    
    def _search_regex(self, pattern: str, files: List[Path], context_lines: int, max_results: int) -> List[Dict]:
        """Regex search."""
        results = []
        
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return [{"file": "error", "line": 0, "match": f"Invalid regex: {e}"}]
        
        for filepath in files:
            if len(results) >= max_results:
                break
            
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                for i, line in enumerate(lines):
                    if regex.search(line):
                        results.append({
                            "file": str(filepath),
                            "line": i + 1,
                            "match": line.strip(),
                        })
                        
                        if len(results) >= max_results:
                            break
            except Exception:
                continue
        
        return results
    
    def _search_symbol(self, query: str, files: List[Path], context_lines: int, max_results: int) -> List[Dict]:
        """Search for symbol definitions."""
        results = []
        query_lower = query.lower()
        
        for filepath in files:
            if len(results) >= max_results:
                break
            
            suffix = filepath.suffix
            patterns = SYMBOL_PATTERNS.get(suffix, [
                rf"def\s+{query}",
                rf"class\s+{query}",
                rf"function\s+{query}",
            ])
            
            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                
                for i, line in enumerate(lines):
                    for pattern in patterns:
                        match = re.search(pattern, line)
                        if match:
                            symbol = match.group(1) if match.groups() else match.group(0)
                            if query_lower in symbol.lower():
                                results.append({
                                    "file": str(filepath),
                                    "line": i + 1,
                                    "symbol": symbol,
                                    "match": line.strip(),
                                })
                                
                                if len(results) >= max_results:
                                    break
                    
                    if len(results) >= max_results:
                        break
            except Exception:
                continue
        
        return results
