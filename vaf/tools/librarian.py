"""
VAF Librarian - Smart File & Information Retrieval Agent
Optimized for fast direct execution of simple tasks
Falls back to LLM only for complex queries
"""
import os
import re
import json
import time
import requests
import sys
import subprocess
import platform
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from rich.live import Live

from vaf.tools.base import BaseTool
from vaf.cli.ui import UI, AnimatedHeader
from vaf.tools.filesystem import ReadFileTool, ListFilesTool, TreeTool, FinderTool, WriteFileTool, FolderSizeTool
from vaf.tools.python_sandbox import PythonSandboxTool

# Try to import psutil for better disk info (optional)
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class LibrarianTool(BaseTool):
    """
    Smart Librarian that handles file/info retrieval tasks.
    
    OPTIMIZATION: Simple tasks are executed DIRECTLY without LLM calls.
    Only complex queries go through the LLM reasoning loop.
    """
    
    name = "librarian_agent"
    description = """A specialized Sub-Agent for Information Retrieval and System Information.
Use for: 
- File operations: 'How many files...', 'List files in...', 'Find file...', 'Read file...'
- System information: 'How many storage devices...', 'What drives are available...', 'Disk space...'
- Storage devices: Counts HDD, SSD, USB drives and shows their capacity
Fast and context-efficient. Has access to storage device information (HDD, SSD, USB drives)."""
    
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task (e.g., 'Count files in Downloads', 'Find config.json')."
            }
        },
        "required": ["task"]
    }
    
    def __init__(self):
        super().__init__()
        # Initialize tools
        self.tools = {
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "list_files": ListFilesTool(),
            "tree": TreeTool(),
            "find_files": FinderTool(),
            "python_sandbox": PythonSandboxTool(),
            "folder_size": FolderSizeTool(),
        }
        
        # Cross-platform home directory
        self.home = Path.home()
        
        # Common folder mappings (cross-platform, multilingual)
        # Try common folder names for each language/OS
        self.folder_aliases = {}
        
        # English folder names
        for name in ["Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music"]:
            path = self.home / name
            if path.exists():
                self.folder_aliases[name.lower()] = path
        
        # German folder names (Windows often uses these)
        german_mappings = {
            "downloads": ["Downloads", "Herunterladen"],
            "desktop": ["Desktop", "Arbeitsplatz"],
            "documents": ["Documents", "Dokumente", "Dokumen"],  # Common typo/abbreviation
            "pictures": ["Pictures", "Bilder"],
            "videos": ["Videos"],
            "music": ["Music", "Musik"],
        }
        
        for key, variants in german_mappings.items():
            for variant in variants:
                path = self.home / variant
                if path.exists():
                    self.folder_aliases[variant.lower()] = path
                    self.folder_aliases[key] = path  # Also map English key
        
        # Always add home and ~
        self.folder_aliases["home"] = self.home
        self.folder_aliases["~"] = self.home
    
    def run(self, **kwargs) -> str:
        task = kwargs.get('task', '').strip()
        if not task:
            return "Error: No task provided."
        
        # ═══════════════════════════════════════════════════════════════════════
        # FAST PATH: Try to handle simple tasks DIRECTLY (no LLM needed)
        # ═══════════════════════════════════════════════════════════════════════
        
        # Try direct execution first (no animation needed for fast path)
        direct_result = self._try_direct_execution(task)
        if direct_result:
            # Fast path succeeded - show brief animation
            header = AnimatedHeader("Collaboration Mode Active", "Main Agt", "Librarian")
            live = Live(header, refresh_per_second=12, console=UI.console)
            live.start()
            try:
                time.sleep(0.5)  # Brief animation
                UI.event("Sub-Agent", "Librarian activated...", style="bold cyan")
                time.sleep(0.3)
            finally:
                live.stop()
            return direct_result
        
        # ═══════════════════════════════════════════════════════════════════════
        # SLOW PATH: Complex task - use LLM reasoning
        # ═══════════════════════════════════════════════════════════════════════
        
        # LLM path has its own animation
        return self._execute_with_llm(task)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # DIRECT EXECUTION (Fast Path)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _try_direct_execution(self, task: str) -> Optional[str]:
        """
        Try to execute simple tasks directly without LLM.
        Returns result string or None if task is too complex.
        """
        task_lower = task.lower()
        
        # ─────────────────────────────────────────────────────────────────────
        # GUARD: Web search result payloads should NOT trigger filesystem heuristics.
        # Without this, URLs like ".com" can be misread as file extensions (e.g. "*.com"),
        # causing bogus searches like "Searching for '*.com' in \\season".
        # ─────────────────────────────────────────────────────────────────────
        if (
            "### web search results" in task_lower
            or "web search results:" in task_lower
            or "user question:" in task_lower
            or "http://" in task_lower
            or "https://" in task_lower
        ):
            # If this looks like the web_lookup workflow "synthesis" prompt,
            # we MUST NOT return the deterministic summary — the workflow expects an actual answer.
            # We only use the deterministic summary to prevent bogus filesystem heuristics.
            if (
                "answer the user's question" in task_lower
                or "use the web search results" in task_lower
                or "include 2-3 source links" in task_lower
                or "synthesize" in task_lower
            ):
                return None

            summary = self._summarize_web_results(task)
            if summary:
                return summary

            # If we couldn't parse, force LLM slow-path instead of filesystem
            return None
        # Normalize common mojibake sequences seen in some Windows consoles (e.g., "groÃŸ" instead of "groß")
        task_norm = (
            task_lower
            .replace("ÃŸ", "ß")
            .replace("ãÿ", "ß")
            .replace("Ã¶", "ö")
            .replace("Ã¤", "ä")
            .replace("Ã¼", "ü")
            .replace("Ã–", "ö")
            .replace("Ã„", "ä")
            .replace("Ãœ", "ü")
        )
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Storage/Drive information ("how many drives", "datenträger")
        # ─────────────────────────────────────────────────────────────────────
        storage_patterns = [
            r"how many (storage|drive|disk|datenträger|datanräger|datnträger)",
            r"wie viele (speicher|laufwerk|festplatte|datenträger|datanräger|datnträger)",
            r"what (drives|storage|disks|laufwerke)",
            r"welche (laufwerke|festplatten|speicher|datenträger)",
            r"list (drives|storage|disks|laufwerke)",
            r"zeige (laufwerke|festplatten|speicher)",
            r"storage devices",
            r"disk space",
            r"speicherplatz",
        ]
        
        for pattern in storage_patterns:
            if re.search(pattern, task_norm):
                # Return storage information directly
                disk_info = self._get_disk_info()
                
                # Extract device count from the formatted string
                if "**Total:**" in disk_info:
                    # Count is already in the string
                    return f"""### Storage Devices Information

{disk_info}
"""
                else:
                    # Count manually if not in string
                    device_count = disk_info.count("**") // 2
                    if device_count == 0:
                        device_count = max(1, disk_info.count("\n") - 2)
                    
                    return f"""### Storage Devices Information

{disk_info}

**Total:** {device_count} storage device(s) found.
"""
        
        # Extract path from task
        path = self._extract_path(task)

        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Folder size ("how big", "size of", "wie groß", "ordnergröße")
        # ─────────────────────────────────────────────────────────────────────
        size_patterns = [
            r"how (big|large) is",
            r"size of",
            r"folder size",
            r"directory size",
            # German: allow flexible phrasing like "wie groß mein downloads folder ist"
            r"wie gro(ß|ss)",
            r"ordnergr(ö|oe)(ß|ss)e",
            r"größe.*(downloads|download|ordner|folder)",
            r"speicherverbrauch",
        ]
        for pattern in size_patterns:
            if re.search(pattern, task_lower):
                # Resolve aliases like "downloads"/"desktop" via _extract_path
                # If path doesn't exist, give a precise actionable message
                try:
                    if not path.exists():
                        return (
                            "[ERROR] Folder not found.\n\n"
                            f"Extracted path: {path}\n"
                            "Try one of these:\n"
                            f"- Use a full path (Windows example): {self.home / 'Downloads'}\n"
                            "- Or mention a known folder alias: downloads / desktop / documents\n"
                        )
                except Exception:
                    pass

                # Compute size deterministically (no LLM)
                return self.tools["folder_size"].run(path=str(path), top_n=10)
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Count files ("how many files", "count files", "wie viele")
        # ─────────────────────────────────────────────────────────────────────
        count_patterns = [
            r"how many (files|items|documents|pdf|pdfs)",
            r"count (files|items|documents|pdf|pdfs)",
            r"wie viele (dateien|files|dokumente|pdf|pdfs)",
            r"anzahl (dateien|files|pdf|pdfs)",
            r"number of (files|items|pdf|pdfs)",
        ]
        
        # Check if specific file type is mentioned (PDF, JPG, etc.)
        file_type = None
        ext_match = re.search(r'\b(pdf|jpg|jpeg|png|txt|doc|docx|zip|mp3|mp4)\b', task_lower)
        if ext_match:
            file_type = ext_match.group(1)
        
        for pattern in count_patterns:
            if re.search(pattern, task_norm):
                return self._count_files(path, file_type=file_type)
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: List files ("list files", "show files", "zeige dateien")
        # ─────────────────────────────────────────────────────────────────────
        list_patterns = [
            r"list (files|items|documents|contents)",
            r"show (files|items|documents|contents)",
            r"zeige (dateien|files|inhalt)",
            r"what('s| is) in",
            r"was ist in",
        ]
        
        for pattern in list_patterns:
            if re.search(pattern, task_norm):
                return self._list_files(path)
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Find files ("find", "search", "locate", "suche")
        # ─────────────────────────────────────────────────────────────────────
        find_patterns = [
            r"find (file|files)?",
            r"search (for)?",
            r"locate",
            r"suche",
            r"finde",
            r"where is",
            r"wo ist",
        ]
        
        # Extract search pattern
        search_term = self._extract_search_term(task)
        
        for pattern in find_patterns:
            if re.search(pattern, task_lower) and search_term:
                return self._find_files(path, search_term)
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Read file ("read file", "show content", "cat")
        # ─────────────────────────────────────────────────────────────────────
        read_patterns = [
            r"read (file|content)",
            r"show (content|file content)",
            r"cat ",
            r"lies ",
            r"zeige inhalt",
        ]
        
        file_path = self._extract_file_path(task)
        
        for pattern in read_patterns:
            if re.search(pattern, task_lower) and file_path:
                return self._read_file(file_path)
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Write file ("write file", "create file", "save to file")
        # ─────────────────────────────────────────────────────────────────────
        write_patterns = [
            r"write (to )?file",
            r"create file",
            r"save (to )?file",
            r"schreibe (in )?datei",
            r"erstelle datei",
            r"speichere (in )?datei",
        ]
        
        # For write operations, we need both path and content
        # If content is not clear, let LLM handle it
        for pattern in write_patterns:
            if re.search(pattern, task_lower) and file_path:
                # Extract content from task (simple cases only)
                content_match = re.search(r'(?:with|content|text|inhalt)[:\s]+(.+?)(?:$|\.)', task_lower, re.IGNORECASE)
                if content_match:
                    content = content_match.group(1).strip()
                    return self._write_file(file_path, content)
                # If no clear content, let LLM handle it
                break
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Folders on drive ("what folders", "welche ordner", "folders on drive")
        # ─────────────────────────────────────────────────────────────────────
        folder_patterns = [
            r"what (folders|directories|ordner) (on|in|auf)",
            r"welche (ordner|verzeichnisse) (auf|in)",
            r"folders (on|in) (drive|disk|laufwerk)",
            r"ordner (auf|in) (laufwerk|festplatte)",
            r"list (folders|directories|ordner)",
            r"zeige (ordner|verzeichnisse)",
        ]
        
        for pattern in folder_patterns:
            if re.search(pattern, task_lower):
                # Extract drive/path from task
                drive_path = self._extract_path(task)
                return self._list_files(drive_path)
        
        # ─────────────────────────────────────────────────────────────────────
        # PATTERN: Tree/Structure ("tree", "structure", "struktur")
        # ─────────────────────────────────────────────────────────────────────
        tree_patterns = [
            r"tree",
            r"structure",
            r"struktur",
            r"directory structure",
            r"folder structure",
        ]
        
        for pattern in tree_patterns:
            if re.search(pattern, task_lower):
                return self._show_tree(path)
        
        # No direct pattern matched - needs LLM
        return None
    
    def _extract_path(self, task: str) -> Path:
        """Extract and resolve path from task description."""
        task_lower = task.lower()
        
        # Check for folder aliases (try longest matches first)
        # Sort by length descending to match "documents" before "doc"
        sorted_aliases = sorted(self.folder_aliases.items(), key=lambda x: len(x[0]), reverse=True)
        for alias, path in sorted_aliases:
            # Match whole words or at word boundaries
            if re.search(r'\b' + re.escape(alias) + r'\b', task_lower):
                return path
        
        # Also try fuzzy matching for common typos/abbreviations
        # "dokumen" -> "documents" or "dokumente"
        fuzzy_mappings = {
            "dokumen": ["documents", "dokumente"],
            "dok": ["documents", "dokumente"],
            "down": ["downloads", "herunterladen"],
            "desk": ["desktop", "arbeitsplatz"],
        }
        
        for fuzzy_key, possible_folders in fuzzy_mappings.items():
            if fuzzy_key in task_lower:
                for folder_name in possible_folders:
                    path = self.home / folder_name
                    if path.exists():
                        return path
        
        # Check for explicit path patterns
        # Windows: C:\Users\... or D:\...
        win_match = re.search(r'([A-Za-z]:\\[^\s"\']+)', task)
        if win_match:
            return Path(win_match.group(1))
        
        # Unix: /home/... or ~/...
        unix_match = re.search(r'((?:/[\w.-]+)+|~[\w/.-]*)', task)
        if unix_match:
            path_str = unix_match.group(1)
            if path_str.startswith('~'):
                return Path(path_str).expanduser()
            return Path(path_str)
        
        # Default to current directory
        return Path.cwd()

    def _summarize_web_results(self, task: str) -> Optional[str]:
        """
        Deterministic summarizer for web_search payloads (no LLM).
        Extracts the query and the top links/snippets so workflows like web_lookup
        don't accidentally trigger filesystem heuristics.
        """
        lower = task.lower()
        if "web search results" not in lower:
            return None

        import re

        # Try to extract "User question:" and "Query:"
        user_q = None
        m = re.search(r"user question:\s*(.+)", task, flags=re.IGNORECASE)
        if m:
            user_q = m.group(1).strip()

        query = None
        m = re.search(r"^query:\s*(.+)$", task, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            query = m.group(1).strip()

        # Extract a few links (supports either "Link: ..." or raw URLs)
        urls = re.findall(r"https?://[^\s\)\]]+", task)
        urls = [u.rstrip(".,") for u in urls]

        # Extract numbered result titles if present ("1. **Title**")
        titles = re.findall(r"^\s*\d+\.\s*\*\*(.+?)\*\*", task, flags=re.MULTILINE)

        lines = []
        lines.append("### Web Lookup (Deterministic Summary)")
        if user_q:
            lines.append(f"**Question:** {user_q}")
        elif query:
            lines.append(f"**Question:** {query}")

        if titles:
            lines.append("\n**Top results:**")
            for t in titles[:5]:
                lines.append(f"- {t.strip()}")

        if urls:
            lines.append("\n**Sources:**")
            for u in urls[:5]:
                lines.append(f"- {u}")
        else:
            lines.append("\n[INFO] No URLs found in the search payload.")

        return "\n".join(lines)
    
    def _extract_search_term(self, task: str) -> Optional[str]:
        """Extract search term/pattern from task."""
        # Look for quoted strings
        quoted = re.search(r'["\']([^"\']+)["\']', task)
        if quoted:
            return quoted.group(1)
        
        # Look for file extensions
        ext_match = re.search(r'\.([\w]+)\b', task)
        if ext_match:
            return f"*.{ext_match.group(1)}"
        
        # Look for specific file patterns
        patterns = [
            r'named?\s+(\S+)',
            r'called?\s+(\S+)',
            r'for\s+(\S+)',
            r'suche\s+(?:nach\s+)?(\S+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, task.lower())
            if match:
                term = match.group(1)
                # Add wildcards if not present
                if '*' not in term:
                    return f"*{term}*"
                return term
        
        return None
    
    def _extract_file_path(self, task: str) -> Optional[Path]:
        """Extract specific file path from task."""
        # Windows path
        win_match = re.search(r'([A-Za-z]:\\[^\s"\']+\.\w+)', task)
        if win_match:
            return Path(win_match.group(1))
        
        # Unix path
        unix_match = re.search(r'((?:/[\w.-]+)+\.\w+)', task)
        if unix_match:
            return Path(unix_match.group(1))
        
        # Relative path
        rel_match = re.search(r'([\w./\\-]+\.\w+)', task)
        if rel_match:
            return Path(rel_match.group(1))
        
        return None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # DIRECT TASK IMPLEMENTATIONS
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _count_files(self, path: Path, file_type: str = None) -> str:
        """Count files in directory, optionally filtered by extension."""
        filter_text = f" ({file_type.upper()} files)" if file_type else ""
        UI.event("Librarian", f"Counting files{filter_text} in {path}...", style="dim")
        
        if not path.exists():
            return f"[ERROR] Path does not exist: {path}"
        
        if not path.is_dir():
            return f"[ERROR] Not a directory: {path}"
        
        try:
            # Count files (not directories), optionally filtered by extension
            all_files = [f for f in path.iterdir() if f.is_file()]
            
            if file_type:
                # Filter by extension (case-insensitive)
                ext = file_type.lower().lstrip('.')
                files = [f for f in all_files if f.suffix.lower() == f'.{ext}']
            else:
                files = all_files
            
            dirs = [d for d in path.iterdir() if d.is_dir()]
            
            total_files = len(files)
            total_dirs = len(dirs)
            
            # Get some stats
            extensions = {}
            total_size = 0
            
            for f in files:
                ext = f.suffix.lower() or "(no extension)"
                extensions[ext] = extensions.get(ext, 0) + 1
                try:
                    total_size += f.stat().st_size
                except:
                    pass
            
            # Format size
            if total_size < 1024:
                size_str = f"{total_size} B"
            elif total_size < 1024 * 1024:
                size_str = f"{total_size / 1024:.1f} KB"
            elif total_size < 1024 * 1024 * 1024:
                size_str = f"{total_size / (1024 * 1024):.1f} MB"
            else:
                size_str = f"{total_size / (1024 * 1024 * 1024):.1f} GB"
            
            # Top extensions (only show if not filtering by specific type)
            if file_type:
                ext_str = f".{file_type.lower()}: {total_files}"
            else:
                top_ext = sorted(extensions.items(), key=lambda x: x[1], reverse=True)[:5]
                ext_str = ", ".join([f"{ext}: {count}" for ext, count in top_ext])
            
            return f"""### Folder: {path.name or path}

**Files:** {total_files}
**Folders:** {total_dirs}
**Total Size:** {size_str}

**Top Extensions:** {ext_str}"""
            
        except PermissionError:
            return f"[ERROR] Permission denied: {path}"
        except Exception as e:
            return f"[ERROR] Error counting files: {e}"
    
    def _list_files(self, path: Path, limit: int = 30) -> str:
        """List files in directory."""
        UI.event("Librarian", f"Listing files in {path}...", style="dim")
        
        if not path.exists():
            return f"[ERROR] Path does not exist: {path}"
        
        if not path.is_dir():
            return f"[ERROR] Not a directory: {path}"
        
        try:
            items = list(path.iterdir())
            files = sorted([f for f in items if f.is_file()], key=lambda x: x.name.lower())
            dirs = sorted([d for d in items if d.is_dir()], key=lambda x: x.name.lower())
            
            result = [f"### Folder: {path.name or path}\n"]
            
            # Directories first
            for d in dirs[:10]:
                result.append(f"[DIR] {d.name}/")
            
            if len(dirs) > 10:
                result.append(f"   ... and {len(dirs) - 10} more folders")
            
            result.append("")
            
            # Files
            for f in files[:limit]:
                size = f.stat().st_size
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size // 1024}KB"
                else:
                    size_str = f"{size // (1024 * 1024)}MB"
                result.append(f"[FILE] {f.name} ({size_str})")
            
            if len(files) > limit:
                result.append(f"\n... and {len(files) - limit} more files")
            
            result.append(f"\n**Total:** {len(files)} files, {len(dirs)} folders")
            
            return "\n".join(result)
            
        except PermissionError:
            return f"[ERROR] Permission denied: {path}"
        except Exception as e:
            return f"[ERROR] Error listing files: {e}"
    
    def _find_files(self, path: Path, pattern: str) -> str:
        """Find files matching pattern."""
        UI.event("Librarian", f"Searching for '{pattern}' in {path}...", style="dim")
        
        if not path.exists():
            return f"[ERROR] Path does not exist: {path}"
        
        try:
            # Use glob for pattern matching
            if '*' in pattern:
                matches = list(path.rglob(pattern))
            else:
                # Search for pattern in filename
                matches = [f for f in path.rglob('*') if pattern.lower() in f.name.lower()]
            
            # Limit results
            matches = matches[:50]
            
            if not matches:
                return f"No files found matching '{pattern}' in {path}"
            
            result = [f"### Search: '{pattern}'\n"]
            result.append(f"Found {len(matches)} matches:\n")
            
            for f in matches[:30]:
                try:
                    rel_path = f.relative_to(path)
                except ValueError:
                    rel_path = f
                
                icon = "[DIR]" if f.is_dir() else "[FILE]"
                result.append(f"{icon} {rel_path}")
            
            if len(matches) > 30:
                result.append(f"\n... and {len(matches) - 30} more")
            
            return "\n".join(result)
            
        except Exception as e:
            return f"[ERROR] Error searching: {e}"
    
    def _write_file(self, file_path: Path, content: str) -> str:
        """Write content to a file."""
        try:
            # Use the write_file tool
            result = self.tools["write_file"].run(path=str(file_path), content=content)
            return result
        except Exception as e:
            return f"Error writing file: {str(e)}"
    
    def _read_file(self, file_path: Path) -> str:
        """Read file contents."""
        UI.event("Librarian", f"Reading {file_path}...", style="dim")
        
        if not file_path.exists():
            return f"[ERROR] File does not exist: {file_path}"
        
        if not file_path.is_file():
            return f"[ERROR] Not a file: {file_path}"
        
        try:
            # Check file size
            size = file_path.stat().st_size
            if size > 100 * 1024:  # 100KB limit
                return f"[ERROR] File too large ({size // 1024}KB). Max 100KB."
            
            content = file_path.read_text(encoding='utf-8', errors='replace')
            
            # Truncate if needed
            if len(content) > 5000:
                content = content[:5000] + "\n\n... (truncated)"
            
            return f"### File: {file_path.name}\n\n```\n{content}\n```"
            
        except Exception as e:
            return f"[ERROR] Error reading file: {e}"
    
    def _show_tree(self, path: Path, max_depth: int = 3) -> str:
        """Show directory tree."""
        UI.event("Librarian", f"Building tree for {path}...", style="dim")
        
        if not path.exists():
            return f"[ERROR] Path does not exist: {path}"
        
        try:
            result = self.tools["tree"].run(path=str(path), depth=max_depth)
            return f"### Directory Tree\n\n```\n{result}\n```"
        except Exception as e:
            return f"[ERROR] Error building tree: {e}"
    
    # ═══════════════════════════════════════════════════════════════════════════
    # DISK/STORAGE INFORMATION (Cross-Platform)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _get_disk_info(self) -> str:
        """Get information about all storage devices (HDD, SSD, USB) - OS independent."""
        drives_info = []
        
        try:
            if HAS_PSUTIL:
                # Use psutil for cross-platform disk info
                partitions = psutil.disk_partitions()
                for partition in partitions:
                    try:
                        usage = psutil.disk_usage(partition.mountpoint)
                        total_gb = usage.total / (1024**3)
                        free_gb = usage.free / (1024**3)
                        used_gb = usage.used / (1024**3)
                        used_percent = (usage.used / usage.total) * 100
                        
                        # Try to detect device type
                        device_type = "Unknown"
                        if sys.platform == "win32":
                            # Windows: Check if it's a removable drive
                            if partition.fstype == "" or "removable" in partition.opts.lower():
                                device_type = "USB/Removable"
                            elif "fixed" in partition.opts.lower():
                                # Try to detect SSD vs HDD (Windows 10+)
                                device_type = self._detect_windows_drive_type(partition.device)
                            else:
                                device_type = "Fixed Drive"
                        else:
                            # Linux/Mac: Check mount point and device name
                            if "/media/" in partition.mountpoint or "/mnt/" in partition.mountpoint:
                                device_type = "USB/Removable"
                            elif "/dev/sd" in partition.device:
                                # Linux: Check if it's an SSD (look for rotational attribute)
                                device_type = self._detect_linux_drive_type(partition.device)
                            elif "/dev/disk" in partition.device:
                                # macOS
                                device_type = "Fixed Drive"
                        
                        drives_info.append({
                            "device": partition.device,
                            "mountpoint": partition.mountpoint,
                            "fstype": partition.fstype,
                            "type": device_type,
                            "total_gb": total_gb,
                            "free_gb": free_gb,
                            "used_gb": used_gb,
                            "used_percent": used_percent
                        })
                    except PermissionError:
                        # Skip drives we can't access
                        continue
                    except Exception:
                        continue
            else:
                # Fallback: Use OS-specific commands
                if sys.platform == "win32":
                    drives_info = self._get_windows_disk_info()
                elif sys.platform == "darwin":
                    drives_info = self._get_macos_disk_info()
                else:
                    drives_info = self._get_linux_disk_info()
        except Exception as e:
            # If all fails, return basic info
            return f"[WARN] Could not retrieve disk information: {e}"
        
        if not drives_info:
            return "No storage devices found."
        
        # Format the information
        result = "**Storage Devices:**\n\n"
        device_count = len(drives_info)
        
        for i, drive in enumerate(drives_info, 1):
            device = drive.get("device", "Unknown")
            mount = drive.get("mountpoint", "")
            drive_type = drive.get("type", "Unknown")
            total = drive.get("total_gb", 0)
            free = drive.get("free_gb", 0)
            used_pct = drive.get("used_percent", 0)
            
            result += f"{i}. **{device}** ({drive_type})\n"
            result += f"   Mount: {mount}\n"
            result += f"   Total: {total:.1f} GB | Free: {free:.1f} GB | Used: {used_pct:.1f}%\n\n"
        
        result += f"**Total:** {device_count} storage device(s)"
        return result
    
    def _detect_windows_drive_type(self, device: str) -> str:
        """Detect if Windows drive is SSD or HDD."""
        try:
            # Use PowerShell to check if drive is SSD
            # Get drive letter from device path (e.g., "C:" from "C:\\")
            drive_letter = device[0] if device else "C"
            cmd = f'powershell -Command "Get-PhysicalDisk | Get-Disk | Where-Object {{$_.Number -eq (Get-Partition -DriveLetter {drive_letter}).DiskNumber}} | Select-Object -ExpandProperty MediaType"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                media_type = result.stdout.strip()
                if "SSD" in media_type or "ssd" in media_type.lower():
                    return "SSD"
                elif "HDD" in media_type or "HDD" in media_type or "Rotational" in media_type:
                    return "HDD"
        except:
            pass
        # Default: Try to guess based on common patterns
        if device and len(device) > 0:
            # C: is usually the main drive (often SSD on modern systems)
            if device[0].upper() == "C":
                return "Fixed Drive (likely SSD)"
        return "Fixed Drive"
    
    def _detect_linux_drive_type(self, device: str) -> str:
        """Detect if Linux drive is SSD or HDD."""
        try:
            # Check /sys/block for rotational attribute
            block_device = device.replace("/dev/", "").rstrip("0123456789")
            rotational_path = f"/sys/block/{block_device}/queue/rotational"
            if os.path.exists(rotational_path):
                with open(rotational_path, 'r') as f:
                    is_rotational = f.read().strip() == "1"
                return "HDD" if is_rotational else "SSD"
        except:
            pass
        return "Fixed Drive"
    
    def _get_windows_disk_info(self) -> List[Dict]:
        """Get disk info on Windows using wmic."""
        drives = []
        try:
            # Get logical drives
            cmd = 'wmic logicaldisk get deviceid,drivetype,freespace,size,volumename'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:]  # Skip header
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 2:
                        device = parts[0]
                        drive_type_code = parts[1] if len(parts) > 1 else "3"
                        # Drive types: 2=Removable, 3=Fixed, 4=Network, 5=CD
                        if drive_type_code == "2":
                            drive_type = "USB/Removable"
                        elif drive_type_code == "3":
                            drive_type = "Fixed Drive"
                        else:
                            continue
                        
                        # Get size info
                        free_bytes = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                        total_bytes = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                        
                        if total_bytes > 0:
                            drives.append({
                                "device": device,
                                "mountpoint": device + "\\",
                                "type": drive_type,
                                "total_gb": total_bytes / (1024**3),
                                "free_gb": free_bytes / (1024**3),
                                "used_gb": (total_bytes - free_bytes) / (1024**3),
                                "used_percent": ((total_bytes - free_bytes) / total_bytes) * 100
                            })
        except:
            pass
        return drives
    
    def _get_linux_disk_info(self) -> List[Dict]:
        """Get disk info on Linux using df."""
        drives = []
        try:
            result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:]  # Skip header
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 6:
                        device = parts[0]
                        mount = parts[5]
                        size_str = parts[1]
                        used_str = parts[2]
                        avail_str = parts[3]
                        use_pct = parts[4].rstrip('%')
                        
                        # Determine drive type
                        if "/media/" in mount or "/mnt/" in mount:
                            drive_type = "USB/Removable"
                        else:
                            drive_type = self._detect_linux_drive_type(device)
                        
                        # Parse sizes (convert from human-readable)
                        def parse_size(s):
                            if 'G' in s:
                                return float(s.replace('G', ''))
                            elif 'T' in s:
                                return float(s.replace('T', '')) * 1024
                            elif 'M' in s:
                                return float(s.replace('M', '')) / 1024
                            return 0
                        
                        total_gb = parse_size(size_str)
                        used_gb = parse_size(used_str)
                        free_gb = parse_size(avail_str)
                        
                        drives.append({
                            "device": device,
                            "mountpoint": mount,
                            "type": drive_type,
                            "total_gb": total_gb,
                            "free_gb": free_gb,
                            "used_gb": used_gb,
                            "used_percent": float(use_pct)
                        })
        except:
            pass
        return drives
    
    def _get_macos_disk_info(self) -> List[Dict]:
        """Get disk info on macOS using df and diskutil."""
        drives = []
        try:
            # Use df for basic info
            result = subprocess.run(['df', '-h'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:]
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 9:
                        device = parts[0]
                        mount = parts[8]
                        size_str = parts[1]
                        used_str = parts[2]
                        avail_str = parts[3]
                        use_pct = parts[4].rstrip('%')
                        
                        # Determine drive type (check if external)
                        drive_type = "Fixed Drive"
                        try:
                            diskutil_result = subprocess.run(
                                ['diskutil', 'info', device],
                                capture_output=True, text=True, timeout=2
                            )
                            if 'External' in diskutil_result.stdout or 'Removable' in diskutil_result.stdout:
                                drive_type = "USB/Removable"
                        except:
                            pass
                        
                        def parse_size(s):
                            if 'G' in s:
                                return float(s.replace('Gi', '').replace('G', ''))
                            elif 'T' in s:
                                return float(s.replace('Ti', '').replace('T', '')) * 1024
                            return 0
                        
                        total_gb = parse_size(size_str)
                        used_gb = parse_size(used_str)
                        free_gb = parse_size(avail_str)
                        
                        drives.append({
                            "device": device,
                            "mountpoint": mount,
                            "type": drive_type,
                            "total_gb": total_gb,
                            "free_gb": free_gb,
                            "used_gb": used_gb,
                            "used_percent": float(use_pct)
                        })
        except:
            pass
        return drives
    
    # ═══════════════════════════════════════════════════════════════════════════
    # ERROR HANDLING & FORMATTING
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _format_error_response(self, response, task: str, history: List, tools_schema: List) -> str:
        """Format error response with helpful context for Main Agent."""
        status_code = response.status_code
        error_message = ""
        error_type = ""
        
        # Try to extract detailed error from response
        try:
            error_data = response.json()
            if "error" in error_data:
                error_obj = error_data["error"]
                error_message = error_obj.get("message", "")
                error_type = error_obj.get("type", "")
        except:
            error_message = response.text[:500] if response.text else ""
        
        # Analyze the task to provide context
        task_lower = task.lower()
        path_hint = ""
        if "dokumen" in task_lower or "documents" in task_lower:
            # Check if path exists
            possible_paths = [
                self.home / "Documents",
                self.home / "Dokumente",
            ]
            existing_paths = [str(p) for p in possible_paths if p.exists()]
            if existing_paths:
                path_hint = f"\nHint: The Documents folder exists at: {existing_paths[0]}"
            else:
                path_hint = f"\nHint: Documents folder not found. Try checking: {self.home}"
        
        # Analyze what might be wrong
        suggestions = []
        
        if status_code == 400:
            suggestions.append("- **Invalid request format** - The task description might be unclear")
            suggestions.append("- **Missing parameters** - The task might need more specific information")
            suggestions.append("- **Path not found** - The folder path might be incorrect or not exist")
            
            # Check if path extraction failed
            extracted_path = self._extract_path(task)
            if not extracted_path.exists():
                suggestions.append(f"- **Path issue** - Extracted path '{extracted_path}' does not exist")
                # Suggest alternatives
                if "dokumen" in task_lower:
                    suggestions.append(f"- **Try:** Use full path like '{self.home / 'Documents'}' or '{self.home / 'Dokumente'}'")
        
        elif status_code == 404:
            suggestions.append("- **Resource not found** - The requested path or file does not exist")
        
        elif status_code == 500:
            suggestions.append("- **Server error** - Internal server problem, not your fault")
        
        # Detect common server-side format issues for more actionable retries
        extra_retry = ""
        if "Cannot have 2 or more assistant messages" in (error_message or ""):
            extra_retry = (
                "\n\n**Retry Hint (Main Agent):**\n"
                "- This is a server-side request ordering error.\n"
                "- Retry with a fresh tool call (new librarian_agent invocation) and a single, explicit objective.\n"
                "- Prefer deterministic tools for OS queries (folder_size, list_files, tree) instead of LLM reasoning.\n"
            )

        # Build helpful error message
        error_report = f"""[ERROR] **Librarian Agent Error Report**

**Status Code:** {status_code}
**Error Type:** {error_type or 'Unknown'}
**Error Message:** {error_message or 'No detailed error message available'}

**Original Task:** "{task}"

**What went wrong:**
{chr(10).join(suggestions) if suggestions else "- Unable to determine specific issue"}

**Available Tools:**
{chr(10).join([f"- {t.name}: {t.description[:60]}..." for t in self.tools.values()])}

**Suggestions for Main Agent:**
1. **Check the path** - Verify the folder/file path exists
2. **Rephrase the task** - Be more specific about what you want (e.g., "Count PDF files in Documents folder")
3. **Use direct path** - Instead of "Dokumen", try "Documents" or the full path
4. **Check permissions** - Ensure the path is accessible{path_hint}
{extra_retry}

**Context:**
- Home directory: {self.home}
- Available folder aliases: {', '.join(list(self.folder_aliases.keys())[:5])}...
"""
        return error_report
    
    def _format_connection_error(self, error: str, task: str) -> str:
        """Format connection error with helpful context."""
        return f"""[ERROR] **Librarian Agent Connection Error**

**Error:** {error}

**Original Task:** "{task}"

**What happened:**
- Could not connect to the LLM server (http://127.0.0.1:8080)
- The server might be down or not responding

**Suggestions for Main Agent:**
1. **Check server status** - Verify the LLM server is running
2. **Try again later** - The server might be temporarily unavailable
3. **Use alternative approach** - Consider using direct file operations if possible

**Note:** The librarian agent needs the LLM server to process complex queries.
"""
    
    # ═══════════════════════════════════════════════════════════════════════════
    # LLM EXECUTION (Slow Path - for complex queries)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _execute_with_llm(self, task: str) -> str:
        """Execute complex task using LLM reasoning."""
        
        # Show animation
        header = AnimatedHeader("Collaboration Mode Active", "Main Agt", "Librarian")
        live = Live(header, refresh_per_second=12, console=UI.console)
        live.start()
        
        time.sleep(1.0)
        UI.event("Sub-Agent", "Librarian analyzing complex query...", style="bold cyan")
        
        # Get disk/storage information
        disk_info = self._get_disk_info()
        
        # System prompt
        system_prompt = f"""You are the Librarian, a file/info retrieval specialist.
User's Home: '{self.home}'

Available Tools:
- read_file(path): Reads a file's contents
- write_file(path, content): Writes content to a file
- list_files(path, sort_by='name'|'date'|'size', limit=100): Lists files and folders in a directory. Use this to see what folders exist on a drive.
- tree(path, depth): Shows directory tree structure. Use this to explore folder structure on a drive.
- find_files(path, pattern): Finds files by name pattern (glob) recursively
- folder_size(path, top_n=10): Calculates total size of a folder recursively and shows largest files
- python_sandbox(code): Execute Python code safely for mathematical calculations, data processing, and algorithms

RULES:
1. Call ONE tool, get result, then ANSWER immediately
2. Do NOT think in loops - act decisively
3. Summarize results (don't dump raw data)
4. If unsure about path, use the home directory

TOOL SELECTION GUIDE:
- "Read file content" / "Read file X" -> Use read_file(path)
- "Write to file" / "Create file" / "Save content to file" -> Use write_file(path, content)
- "What folders on drive X?" / "Welche Ordner auf Laufwerk X?" -> Use list_files(path) to see all folders
- "Show folder structure" -> Use tree(path, depth) for visual tree
- "Find files matching pattern" -> Use find_files(path, pattern)
- "Largest files" / "Biggest files" / "Größte Dateien" -> Use list_files(path, sort_by='size', limit=20) to find largest files
- "Files by size" / "Dateien nach Größe" -> Use list_files(path, sort_by='size') to sort files by size
- "Multiple files analysis" / "Mehrere Dateien analysieren" -> Use list_files with sort_by='size' or find_files, then analyze results
- "Calculate" / "Math" / "Compute" / "Algorithm" -> Use python_sandbox(code) for mathematical calculations and data processing

COMPLEX FILE QUERIES:
- For "largest files" / "biggest files" / "größte Dateien": Use list_files(path, sort_by='size', limit=20) to get top files by size
- For "analyze data files" / "multiple files": Use find_files to find all matching files, then list_files with sort_by='size' to analyze
- For complex analysis: Combine find_files + list_files + python_sandbox if needed for calculations

Common paths:
- Downloads: {self.folder_aliases.get('downloads', 'N/A')}
- Desktop: {self.folder_aliases.get('desktop', 'N/A')}
- Documents: {self.folder_aliases.get('documents', 'N/A')}

{disk_info}"""

        # Initialize context manager for this librarian agent session
        from vaf.core.context import ContextManager
        max_tokens = 8192  # Same as main agent
        context_manager = ContextManager(max_tokens=max_tokens)

        history = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Task: {task}"}
        ]
        
        # Snapshot history (before processing)
        history_snapshot_len = len(history)

        def _sanitize_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """
            The local OpenAI-compatible server is strict about message ordering.
            In particular it may reject requests if the message list ends with
            multiple assistant messages (e.g. after retries with empty outputs).
            """
            if not messages:
                return messages

            # Remove trailing assistant messages if there are 2+ at the end
            while len(messages) >= 2 and messages[-1].get("role") == "assistant" and messages[-2].get("role") == "assistant":
                messages.pop(-2)

            return messages
        
        # Reduced max steps
        max_steps = 5
        
        # Descriptive status messages
        # ASCII-only status strings for maximum terminal compatibility
        status_messages = [
            "Thinking...",
            "Analyzing...",
            "Processing...",
            "Refining...",
            "Finalizing...",
        ]
        
        for step in range(max_steps):
            status = status_messages[min(step, len(status_messages) - 1)]
            UI.event("Librarian", status, style="dim")
            
            # Context management - prevent token overflow
            if context_manager.should_compress(history):
                UI.event("Librarian", "Compressing context...", style="dim")
                history = context_manager.compress(history)
            
            try:
                # Get model name
                model_name = "user-model"
                try:
                    m_res = requests.get("http://127.0.0.1:8080/v1/models", timeout=2)
                    if m_res.status_code == 200:
                        data = m_res.json()
                        if 'data' in data and len(data['data']) > 0:
                            model_name = data['data'][0]['id']
                except:
                    pass
                
                # Build tools schema with validation
                tools_schema = []
                for t in self.tools.values():
                    params = getattr(t, 'parameters', {})
                    # Ensure parameters is a valid dict
                    if not isinstance(params, dict):
                        params = {"type": "object", "properties": {}}
                    
                    # Validate required fields
                    if "type" not in params:
                        params["type"] = "object"
                    if "properties" not in params:
                        params["properties"] = {}
                    
                    tools_schema.append({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description or f"Tool: {t.name}",
                            "parameters": params
                        }
                    })
                
                # API call with better error handling
                try:
                    history = _sanitize_history(history)
                    res = requests.post(
                        "http://127.0.0.1:8080/v1/chat/completions",
                        json={
                            "model": model_name,
                            "messages": history,
                            "max_tokens": 1024,
                            "temperature": 0.1,
                            "tools": tools_schema,
                            "tool_choice": "auto",
                        },
                        timeout=60,
                    )
                    
                    if res.status_code != 200:
                        live.stop()
                        return self._format_error_response(res, task, history, tools_schema)
                except requests.exceptions.RequestException as e:
                    live.stop()
                    return self._format_connection_error(str(e), task)
                
                msg = res.json()['choices'][0]['message']
                content = msg.get('content', '')
                tool_calls = msg.get('tool_calls', [])
                
                history.append(msg)

                # If model returned neither tool calls nor content, retry with brief prompt
                if not tool_calls and not content:
                    # Remove the empty assistant message
                    if history and history[-1].get('role') == 'assistant':
                        history.pop()
                    
                    # ═══════════════════════════════════════════════════════════════
                    # NEW: Tool-Intent Detection (like main agent)
                    # ═══════════════════════════════════════════════════════════════
                    # Check if agent mentioned a tool name (but didn't actually call it yet)
                    # CRITICAL: First check if response is truly empty (content is already checked above)
                    # In librarian, we already have `not content`, so response is truly empty
                    is_truly_empty = not content
                    
                    # Only check tool intent if response is truly empty
                    if is_truly_empty:
                        # Get available tool names dynamically
                        available_tool_names = list(self.tools.keys()) if hasattr(self, 'tools') and self.tools else []
                        # Also check for common tool names used by librarian
                        common_tool_names = ["read_file", "write_file", "list_files", "find_files", "tree", "folder_size", "python_sandbox"]
                        all_tool_names = list(set(available_tool_names + common_tool_names))
                        
                        # Check if any tool name appears in the last assistant message (case-insensitive)
                        last_assistant_msg = None
                        for msg in reversed(history):
                            if msg.get('role') == 'assistant' and msg.get('content'):
                                last_assistant_msg = str(msg.get('content', '')).lower()
                                break
                        
                        mentioned_tools = []
                        if last_assistant_msg:
                            # Check if any tool name appears in the response (case-insensitive)
                            mentioned_tools = [tool_name for tool_name in all_tool_names if tool_name.lower() in last_assistant_msg]
                        
                        # CRITICAL: Only reset if BOTH conditions are met:
                        # 1. Response is truly empty (content is None/empty) - checked BEFORE any cleaning
                        # 2. Tool was mentioned but not called
                        # This is language-independent - we only check if response is empty, not what language it's in
                        if mentioned_tools and not tool_calls:
                            tool_hint = mentioned_tools[0]
                        UI.event("Librarian", f"Tool-Intent detected for '{tool_hint}' without action - resetting to snapshot", style="dim")
                        
                        # Check if there's thinking DIRECTLY after user prompt
                        user_prompt_idx = 1  # User message is at index 1 (after system at 0)
                        
                        # Check if there's an assistant message with content directly after user prompt
                        first_assistant_after_user = None
                        first_assistant_idx = None
                        
                        # Look at messages right after user prompt (within next 2 messages)
                        for i in range(user_prompt_idx + 1, min(user_prompt_idx + 3, len(history))):
                            msg = history[i]
                            if msg.get('role') == 'assistant' and msg.get('content'):
                                content_str = str(msg.get('content', ''))
                                # Keep if it has substantial content (thinking/reasoning)
                                if len(content_str.strip()) > 20:
                                    first_assistant_after_user = content_str
                                    first_assistant_idx = i
                                    break  # Only take the FIRST one directly after user prompt
                        
                        if first_assistant_after_user and first_assistant_idx is not None:
                            # Keep user prompt + first thinking - this becomes the new snapshot
                            history = history[:first_assistant_idx + 1]
                            UI.event("Librarian", f"Reset to thinking snapshot (user prompt + {len(first_assistant_after_user)} chars of first thinking)", style="dim")
                        else:
                            # No thinking found - reset to user prompt snapshot (as before)
                            history = history[:history_snapshot_len]
                            UI.event("Librarian", f"Reset to user prompt snapshot", style="dim")
                        
                        # Add a brief system prompt (will work better now because first thinking is preserved)
                        history.append({
                            "role": "system",
                            "content": "You didn't respond. Please answer or continue where you left off."
                        })
                        
                        # Continue the loop - if it fails again, this system message will be removed with the reset
                        continue
                    
                    # ═══════════════════════════════════════════════════════════════
                    # Snapshot System with Thinking Preservation (like main agent)
                    # ═══════════════════════════════════════════════════════════════
                    # Check if there's thinking DIRECTLY after user prompt
                    # The user prompt is at history_snapshot_len (index 1, after system prompt)
                    user_prompt_idx = 1  # User message is at index 1 (after system at 0)
                    
                    # Check if there's an assistant message with content directly after user prompt
                    first_assistant_after_user = None
                    first_assistant_idx = None
                    
                    # Look at messages right after user prompt (within next 2 messages)
                    for i in range(user_prompt_idx + 1, min(user_prompt_idx + 3, len(history))):
                        msg = history[i]
                        if msg.get('role') == 'assistant' and msg.get('content'):
                            content_str = str(msg.get('content', ''))
                            # Keep if it has substantial content (thinking/reasoning)
                            if len(content_str.strip()) > 20:
                                first_assistant_after_user = content_str
                                first_assistant_idx = i
                                break  # Only take the FIRST one directly after user prompt
                    
                    if first_assistant_after_user and first_assistant_idx is not None:
                        # Keep user prompt + first thinking - this becomes the new snapshot
                        history = history[:first_assistant_idx + 1]
                        UI.event("Librarian", f"Reset to thinking snapshot (user prompt + {len(first_assistant_after_user)} chars of first thinking)", style="dim")
                    else:
                        # No thinking found - reset to user prompt snapshot (as before)
                        history = history[:history_snapshot_len]
                        UI.event("Librarian", f"Reset to user prompt snapshot", style="dim")
                    
                    # Add a brief system prompt (will work better now because first thinking is preserved)
                    history.append({
                        "role": "system",
                        "content": "You didn't respond. Please answer or continue where you left off."
                    })
                    
                    # Continue the loop - if it fails again, this system message will be removed with the reset
                    continue
                
                # If no tool calls and has content → done
                if not tool_calls and content:
                    live.stop()
                    return f"### Librarian Report\n\n{content}"
                
                # Execute tools
                for tc in tool_calls:
                    fn_name = tc['function']['name']
                    try:
                        fn_args = json.loads(tc['function']['arguments'])
                    except:
                        fn_args = {}
                    
                    # Show tool action with icon
                    tool_icons = {
                        "list_files": "Listing",
                        "read_file": "Reading",
                        "find_files": "Searching",
                        "tree": "Mapping",
                        "folder_size": "Sizing",
                    }
                    icon = tool_icons.get(fn_name, "Calling")
                    UI.event("Librarian", f"{icon}: {fn_name}", style="bold green")
                    
                    if fn_name in self.tools:
                        try:
                            result = self.tools[fn_name].run(**fn_args)
                        except Exception as e:
                            result = f"Error: {e}"
                    else:
                        result = f"Unknown tool: {fn_name}"
                    
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc['id'],
                        "name": fn_name,
                        "content": str(result)[:2000]  # Limit result size
                    })
                
            except requests.exceptions.Timeout:
                live.stop()
                return "Error: Request timed out"
            except Exception as e:
                live.stop()
                return f"Error: {e}"
        
        live.stop()
        
        # Extract last result if available
        for m in reversed(history):
            if m.get('role') == 'tool':
                return f"### Librarian Report\n\n{m.get('content', '')[:1000]}"
        
        return "Librarian could not complete the task."
