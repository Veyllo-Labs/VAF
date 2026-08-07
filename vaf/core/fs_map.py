# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
import os
import platform
import re
import time
import json
from pathlib import Path
from collections import defaultdict
from typing import Dict, Optional, List

class FilesystemMap:
    """OS-agnostic filesystem index for intelligent navigation."""
    
    def __init__(self):
        self.os_type = platform.system()  # Windows | Darwin | Linux
        self.home = Path.home()
        self.map = {}
        
    def get_standard_locations(self) -> Dict[str, Path]:
        """
        Returns OS-specific standard folders.
        
        Returns:
            Dict mapping semantic names to actual paths
            e.g., {'documents': Path('/Users/user/Documents')}
        """
        locations = {}
        
        if self.os_type == "Windows":
            # Windows-specific paths
            locations = {
                'desktop': self.home / 'Desktop',
                'documents': self.home / 'Documents',
                'downloads': self.home / 'Downloads',
                'pictures': self.home / 'Pictures',
                'videos': self.home / 'Videos',
                'music': self.home / 'Music',
                'appdata': Path(os.getenv('APPDATA', self.home / 'AppData' / 'Roaming')),
                'temp': Path(os.getenv('TEMP', 'C:\\Windows\\Temp')),
                'public': Path(os.getenv('PUBLIC', 'C:\\Users\\Public')),
            }
            
        elif self.os_type == "Darwin":  # macOS
            locations = {
                'desktop': self.home / 'Desktop',
                'documents': self.home / 'Documents',
                'downloads': self.home / 'Downloads',
                'pictures': self.home / 'Pictures',
                'videos': self.home / 'Movies',  # macOS calls it "Movies"
                'music': self.home / 'Music',
                'library': self.home / 'Library',
                'applications': Path('/Applications'),
                'temp': Path('/tmp'),
            }
            
        else:  # Linux (and other Unix-like)
            # XDG Base Directory Specification
            xdg_config = os.getenv('XDG_CONFIG_HOME', self.home / '.config')
            xdg_data = os.getenv('XDG_DATA_HOME', self.home / '.local' / 'share')
            
            locations = {
                'desktop': self.home / 'Desktop',
                'documents': self.home / 'Documents',
                'downloads': self.home / 'Downloads',
                'pictures': self.home / 'Pictures',
                'videos': self.home / 'Videos',
                'music': self.home / 'Music',
                'config': Path(xdg_config),
                'data': Path(xdg_data),
                'temp': Path('/tmp'),
                'opt': Path('/opt'),  # Installed software
            }
        
        # Filter: Only return existing paths
        existing = {}
        for name, path in locations.items():
            try:
                if path.exists() and path.is_dir():
                    existing[name] = path
            except (PermissionError, OSError):
                continue  # Skip inaccessible paths
        
        return existing
    
    def scan_folder(
        self, 
        path: Path, 
        max_depth: int = 2,
        max_items: int = 10000  # Prevent infinite scanning
    ) -> Optional[dict]:
        """
        Scans a folder and counts file types.
        
        Args:
            path: Folder path to scan
            max_depth: Maximum recursion depth (0 = only direct children)
            max_items: Safety limit to prevent scanning massive folders
        
        Returns:
            Dict with folder stats or None if inaccessible
        """
        if not path.exists() or not path.is_dir():
            return None
        
        result = {
            'path': str(path),
            'name': path.name or str(path),  # Handle root paths
            'folder_count': 0,
            'file_types': defaultdict(int),
            'total_files': 0,
            'size_bytes': 0,
            'subfolders': []
        }
        
        try:
            items = list(path.iterdir())
            
            # Safety: Stop if folder has too many items
            if len(items) > max_items:
                result['note'] = f'Skipped: {len(items)} items (too large)'
                return result
            
            for item in items:
                # Skip hidden files/folders (cross-platform)
                name = item.name
                if name.startswith('.') or name.startswith('$') or name.startswith('~'):
                    continue
                
                # Skip system folders (Windows)
                if self.os_type == "Windows" and name in [
                    'System Volume Information', '$RECYCLE.BIN', 
                    'Config.Msi', 'Recovery'
                ]:
                    continue
                
                try:
                    if item.is_dir():
                        result['folder_count'] += 1
                        
                        # Recursive scan if depth allows
                        if max_depth > 0:
                            subfolder = self.scan_folder(
                                item, 
                                max_depth - 1,
                                max_items
                            )
                            if subfolder:
                                result['subfolders'].append(subfolder)
                    
                    elif item.is_file():
                        result['total_files'] += 1
                        
                        # Extension (normalized to lowercase, no dot)
                        ext = item.suffix.lower().lstrip('.') or 'no_ext'
                        result['file_types'][ext] += 1
                        
                        # File size (safe)
                        try:
                            result['size_bytes'] += item.stat().st_size
                        except (PermissionError, OSError):
                            pass
                
                except (PermissionError, OSError):
                    continue  # Skip inaccessible items
        
        except (PermissionError, OSError):
            return None
        
        return result
    
    def build_map(self, depth: int = 1) -> dict:
        """
        Builds the complete filesystem map.
        
        Args:
            depth: Scan depth (0=only main folders, 1=+subfolders)
        
        Returns:
            Complete map structure
        """
        locations = self.get_standard_locations()
        
        self.map = {
            'os': self.os_type,
            'home': str(self.home),
            'scanned_at': time.time(),
            'locations': {}
        }
        
        # Scan each standard location
        for name, path in locations.items():
            scan_result = self.scan_folder(path, max_depth=depth)
            if scan_result:
                self.map['locations'][name] = scan_result
        
        return self.map
    
    def format_summary(self, compact: bool = False) -> str:
        """
        Formats the map for LLM consumption (English).
        
        The LLM thinks in English, so we use English descriptions.
        """
        if not self.map:
            self.build_map()
        
        if compact:
            return self._format_compact()
        
        return self._format_detailed()
    
    def _format_compact(self) -> str:
        """Ultra-compact format (saves tokens)."""
        parts = [f"OS={self.map['os']}"]
        
        for name, data in self.map['locations'].items():
            # Get most common file type
            if data['file_types']:
                top_type = max(
                    data['file_types'].items(), 
                    key=lambda x: x[1]
                )[0]
            else:
                top_type = 'empty'
            
            parts.append(
                f"{name}:{data['total_files']}files,"
                f"{data['folder_count']}dirs,main={top_type}"
            )
        
        return " | ".join(parts)
    
    def _format_detailed(self) -> str:
        """Detailed format with full context."""
        lines = [
            "FILESYSTEM MAP",
            f"OS: {self.map['os']}",
            f"Home: {self.map['home']}",
            ""
        ]
        
        for loc_name, data in sorted(self.map['locations'].items()):
            # Calculate size in MB
            size_mb = data['size_bytes'] / (1024 * 1024)
            
            # Get top 3 file types
            top_types = sorted(
                data['file_types'].items(),
                key=lambda x: x[1],
                reverse=True
            )[:3]
            
            # Format location header
            lines.append(
                f"📁 {loc_name.upper()}: "
                f"{data['folder_count']} folders, "
                f"{data['total_files']} files "
                f"({size_mb:.1f}MB)"
            )
            
            # Add common file types
            if top_types:
                type_str = ", ".join(
                    f"{count} {ext.upper()}" 
                    for ext, count in top_types
                )
                lines.append(f"   Common types: {type_str}")
            
            # Add notable subfolders (top 3 by file count)
            if data.get('subfolders'):
                sorted_subs = sorted(
                    data['subfolders'],
                    key=lambda x: x['total_files'],
                    reverse=True
                )[:3]
                
                for sub in sorted_subs:
                    lines.append(
                        f"   ├─ {sub['name']}: "
                        f"{sub['total_files']} files"
                    )
            
            lines.append("")  # Empty line between locations
        
        return "\n".join(lines)
    
    def get_system_prompt_addition(self) -> str:
        """
        Returns the system prompt text to inject into the agent.
        
        This tells the LLM how to use the filesystem map.
        """
        map_text = self.format_for_llm(compact=False)
        
        return f"""
## FILESYSTEM NAVIGATION CONTEXT

You have access to this filesystem map for intelligent file operations:

{map_text}

**NAVIGATION RULES:**

1. **Explicit folder wins** - When the user names a folder (e.g. "in Downloads", "im Downloads Ordner"), use THAT folder. Do NOT default to Documents.

2. **Default Locations** - Use these standard folders only when NO folder is specified:
   - Documents → "documents" folder (PDFs, TXTs, DOCXs)
   - Images/Pictures → "pictures" folder (JPGs, PNGs)
   - Downloads → "downloads" folder (temporary files)
   - Videos → "videos" folder (MP4s, MOVs)

3. **Quick Queries** - Answer from the map when possible:
   - "How many documents?" → Check 'documents' file_types
   - "Where are my pictures?" → Point to 'pictures' location
   - "Total PDFs?" → Sum PDF counts across locations

4. **Smart Search** - When searching for files:
   - Start in the MOST LIKELY location based on file type
   - Example: "find report.pdf" → Search documents first, then downloads
   - Example: "find vacation.jpg" → Search pictures first

5. **Don't Over-Search** - If the map answers the question, respond immediately
   - User: "How many files in Documents?"
   - You: "You have 65 files in your Documents folder" ← Direct answer
   - DO NOT search the filesystem if the map already has this info

6. **Path Format** - Always use OS-appropriate paths:
   - Windows: Use backslashes (C:\\Users\\...)
   - macOS/Linux: Use forward slashes (/Users/...)

Current OS: {self.map['os']}
"""

    #: Words that name a standard LOCATION, and the map key they resolve to. Word-boundary
    #: matched, so 'doc' does not match 'docker' and 'movie' does not match 'remove'.
    #: Only the four folders the fast path has always answered for; anything else falls
    #: through to a real search rather than growing this table on speculation.
    _LOCATION_WORDS = (
        ("documents", r"documents?|dokumente?n?|docs?"),
        ("downloads", r"downloads?|downloadordner"),
        ("pictures", r"pictures?|images?|photos?|fotos?|bilder"),
        ("videos", r"videos?|movies?|filme?"),
    )

    #: Words that name a file TYPE, and the extension the scan counts it under. A type is
    #: NOT a location - conflating the two is what made every PDF question an answer about
    #: the Documents folder.
    _TYPE_WORDS = (
        ("pdf", r"pdfs?"),
        ("docx", r"docx"),
        ("txt", r"txts?"),
        ("jpg", r"jpe?gs?"),
        ("png", r"pngs?"),
        ("mp4", r"mp4s?"),
        ("mp3", r"mp3s?"),
        ("zip", r"zips?"),
    )

    def query_fast(self, question: str) -> Optional[str]:
        """
        Attempts to answer simple queries directly from the map.

        This avoids expensive file_search operations.

        Location first, type second, and never a count the scan did not take. The old
        version matched INTENT in a fixed order with the file types folded into the
        Documents branch, so "how many PDFs are in Downloads" answered about Documents -
        the type keyword decided the folder and the folder keyword was never reached.
        Measured on a real map: Downloads held 33 PDFs, the answer said 9, which is the
        Documents count. Same shape as the 2026-07-13 incident one layer up: an answer
        about a folder nobody asked about, delivered fast enough to look authoritative.

        Args:
            question: Natural language question

        Returns:
            Answer string if found, None if needs file search
        """
        q = question.lower()
        # Strip path-like tokens and filename mentions BEFORE intent matching: a task
        # text quoting '/home/user/Documents/x.html' or 'report.pdf' must never trigger
        # a folder-stats answer (live 2026-07-13: four delete tasks were each answered
        # with Documents statistics because 'document' substring-matched the PATH).
        q = re.sub(r"\S*[/\\]\S*", " ", q)
        q = re.sub(r"\b[\w-]+\.[a-z0-9]{1,5}\b", " ", q)

        location = next((name for name, pattern in self._LOCATION_WORDS
                         if re.search(rf"\b(?:{pattern})\b", q)), None)
        file_type = next((ext for ext, pattern in self._TYPE_WORDS
                          if re.search(rf"\b(?:{pattern})\b", q)), None)

        if location:
            return self._answer_for_location(location, file_type)
        if file_type:
            return self._answer_across_locations(file_type)
        return None

    def _answer_for_location(self, location: str, file_type: Optional[str]) -> Optional[str]:
        """Stats for ONE named folder, or None when the map cannot honestly answer."""
        loc = (self.map.get('locations') or {}).get(location)
        if not loc:
            return None                      # folder not on this machine: let the search look

        label = location.capitalize()
        total = loc.get('total_files', 0)
        types = loc.get('file_types')

        if file_type is not None:
            if not types:
                # No per-type data for this folder. A zero here would be a count nobody
                # took, dressed as one - fall through to a real search instead.
                return None
            return (f"{label} folder contains {types.get(file_type, 0)} "
                    f"{file_type.upper()}s (of {total} files in total)")

        if types:
            top = ", ".join(f"{n} {ext.upper()}s" for ext, n
                            in sorted(types.items(), key=lambda kv: -kv[1])[:4] if n)
            if top:
                return f"{label} folder contains: {top} (Total: {total} files)"
        return f"{label} folder: {total} files"

    def _answer_across_locations(self, file_type: str) -> Optional[str]:
        """A type question with no folder named - answer for every folder that has the data.

        The old code answered "Documents" here. That is a guess presented as a fact, and on
        this machine it was the wrong folder by a factor of three. Naming every folder costs
        nothing (the counts are already in the map) and cannot be silently wrong.
        """
        counts = []
        for name, loc in (self.map.get('locations') or {}).items():
            types = loc.get('file_types')
            if types and types.get(file_type):
                counts.append((name, types[file_type]))
        if not counts:
            return None

        counts.sort(key=lambda kv: -kv[1])
        where = ", ".join(f"{name.capitalize()} {n}" for name, n in counts)
        return f"{file_type.upper()}s by folder: {where} (Total: {sum(n for _, n in counts)})"


    def suggest_search_location(self, filename: str) -> Path:
        """
        Suggests the best location to search for a file.
        
        Args:
            filename: The file to find (e.g., "report.pdf")
        
        Returns:
            Most likely Path to search in
        """
        ext = Path(filename).suffix.lower().lstrip('.')
        
        # Extension-based mapping
        location_map = {
            'pdf': 'documents',
            'docx': 'documents',
            'txt': 'documents',
            'xlsx': 'documents',
            'jpg': 'pictures',
            'jpeg': 'pictures',
            'png': 'pictures',
            'gif': 'pictures',
            'mp4': 'videos',
            'mov': 'videos',
            'avi': 'videos',
            'mp3': 'music',
            'wav': 'music',
            'zip': 'downloads',
            'exe': 'downloads',
        }
        
        suggested_loc = location_map.get(ext, 'documents')
        
        # Get the actual path
        loc_data = self.map['locations'].get(suggested_loc)
        if loc_data:
            return Path(loc_data['path'])
        
        # Fallback to home
        return self.home


# ============================================
# CACHED VERSION (Faster startup)
# ============================================

class CachedFilesystemMap(FilesystemMap):
    """Filesystem map with persistent caching for instant loading."""
    
    def __init__(self, cache_file: Optional[Path] = None):
        super().__init__()
        
        # Default cache location: ~/.vaf_fs_cache.json
        if cache_file:
            self.cache_file = cache_file
        else:
            self.cache_file = self.home / '.vaf_fs_cache.json'
    
    def load_from_cache(self) -> bool:
        """
        Loads the map from cache file.
        
        Returns:
            True if loaded successfully, False otherwise
        """
        if not self.cache_file.exists():
            return False
        
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                self.map = json.load(f)
            
            # Validate cache (check if it's from the same OS)
            if self.map.get('os') != self.os_type:
                return False  # OS changed, rebuild needed
            
            # Check age (rebuild if older than 1 hour)
            scanned_at = self.map.get('scanned_at', 0)
            if time.time() - scanned_at > 3600:  # 1 hour
                return False
            
            return True
            
        except (json.JSONDecodeError, KeyError, OSError):
            return False
    
    def save_to_cache(self):
        """Saves the current map to cache file."""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.map, f, indent=2)
        except OSError:
            pass  # Ignore if can't write cache
    
    def build_map(self, depth: int = 1, force_rebuild: bool = False) -> dict:
        """
        Builds map with caching.
        
        Args:
            depth: Scan depth
            force_rebuild: Ignore cache and rebuild
        
        Returns:
            Filesystem map
        """
        # Try to load from cache first
        if not force_rebuild and self.load_from_cache():
            return self.map
        
        # Build fresh map
        result = super().build_map(depth)
        
        # Save to cache
        self.save_to_cache()
        
        return result