import os
import shutil
from vaf.tools.base import BaseTool

# Common Safety Logic
# Common Safety Logic
BLOCKED_DIRS = [
    "Windows", "Program Files", "Program Files (x86)", "System32", # Windows
    "/etc", "/usr", "/sys", "/proc", "/var", "/boot", # Linux/Mac
    ".git", ".ssh", "node_modules", ".env", "id_rsa"
]

def is_safe_path(path):
    try:
         abs_path = os.path.abspath(os.path.expanduser(path))
         for blocked in BLOCKED_DIRS:
             if blocked in abs_path:
                 return False, f"Access denied: {blocked}"
         return True, abs_path
    except:
         return False, "Invalid path"

class ListFilesTool(BaseTool):
    name = "list_files"
    description = "Lists files in a directory."

    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path"},
            "sort_by": {"type": "string", "enum": ["name", "date", "size"], "description": "Sort order (default: name)"},
            "limit": {"type": "integer", "description": "Max files to return (default: 100)"}
        },
        "required": ["path"]
    }

    def run(self, **kwargs) -> str:
        path = kwargs.get('path', '.')
        sort_by = kwargs.get('sort_by', 'name')
        limit = kwargs.get('limit', 100)
        
        safe, res = is_safe_path(path)
        if not safe: return res
        
        try:
            entries = []
            with os.scandir(res) as it:
                for entry in it:
                    try:
                        stat = entry.stat()
                        entries.append({
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "size": stat.st_size,
                            "mtime": stat.st_mtime
                        })
                    except: pass
            
            # Sorting
            if sort_by == 'date':
                entries.sort(key=lambda x: x['mtime'], reverse=True) # Newest first
            elif sort_by == 'size':
                entries.sort(key=lambda x: x['size'], reverse=True) # Largest first
            else:
                entries.sort(key=lambda x: x['name'].lower())

            # Formatting
            import datetime
            output = f"Directory Listing: {res} (Total files found: {len(entries)}) (Sorted by: {sort_by})\n"
            output += f"{'Type':<6} {'Date':<18} {'Size':<10} {'Name'}\n"
            output += "-"*60 + "\n"
            
            count = 0
            for e in entries:
                if count >= limit:
                    output += f"... (and {len(entries) - limit} more)\n"
                    break
                    
                dt = datetime.datetime.fromtimestamp(e['mtime']).strftime('%Y-%m-%d %H:%M')
                
                if e['is_dir']:
                    size_str = "<DIR>"
                    type_str = "[DIR]"
                else:
                    type_str = "[FILE]"
                    # Size formatting
                    if e['size'] < 1024: size_str = f"{e['size']} B"
                    elif e['size'] < 1024*1024: size_str = f"{e['size']/1024:.1f} KB"
                    else: size_str = f"{e['size']/(1024*1024):.1f} MB"
                
                output += f"{type_str:<6} {dt:<18} {size_str:<10} {e['name']}\n"
                count += 1
                
            return output if output else "Empty Directory"
        except Exception as e: return str(e)

class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Reads the content of a file."

    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"]
    }

    def run(self, **kwargs) -> str:
        path = kwargs.get('path', '')
        safe, res = is_safe_path(path)
        if not safe: return res
        try:
            if not os.path.exists(res): return "Error: File not found."
            if not os.path.isfile(res): return "Error: Not a file."
            
            with open(res, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return content
        except Exception as e: return str(e)

class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Writes content to a file."

    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"}, 
            "content": {"type": "string"}
        },
        "required": ["path", "content"]
    }

    def run(self, **kwargs) -> str:
        path = kwargs.get('path', '')
        content = kwargs.get('content', '')
        safe, res = is_safe_path(path)
        if not safe: return res
        
        import time
        
        # Retry mechanism for file locking issues (especially on Windows)
        max_retries = 3
        retry_delay = 0.1  # 100ms between retries
        
        for attempt in range(max_retries):
            try:
                # Ensure parent directory exists
                parent_dir = os.path.dirname(res)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)
                
                # On Windows, files can be locked even after closing
                # Use atomic write pattern: write to temp file, then rename
                import tempfile
                import shutil
                
                # Create temp file in same directory (for atomic rename)
                temp_dir = os.path.dirname(res) or '.'
                temp_fd, temp_path = tempfile.mkstemp(
                    suffix='.tmp',
                    dir=temp_dir,
                    text=True
                )
                
                try:
                    # Write to temp file
                    with os.fdopen(temp_fd, 'w', encoding='utf-8') as f:
                        f.write(content)
                        f.flush()  # Ensure data is written
                        os.fsync(f.fileno())  # Force write to disk (OS-independent)
                    
                    # Atomic rename (works on all OS)
                    # On Windows, this will fail if file is locked
                    if os.path.exists(res):
                        # Remove existing file first (on Windows, rename fails if target exists)
                        try:
                            os.remove(res)
                        except PermissionError:
                            # File is locked - wait and retry
                            os.remove(temp_path)  # Clean up temp file
                            if attempt < max_retries - 1:
                                time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                                continue
                            raise
                    
                    # Atomic rename
                    shutil.move(temp_path, res)
                    
                    # Verify file was written correctly
                    if os.path.exists(res) and os.path.getsize(res) == len(content.encode('utf-8')):
                        return f"File written successfully to {res}"
                    else:
                        return f"⚠️ File written but size verification failed: {res}"
                        
                except Exception as e:
                    # Clean up temp file on error
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except:
                        pass
                    raise
                    
            except PermissionError as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                # Final attempt failed - return detailed error
                error_msg = str(e).lower()
                if 'denied' in error_msg or 'permission' in error_msg:
                    return (
                        f"❌ Permission denied: Cannot write to {res}\n"
                        f"Possible causes:\n"
                        f"- File is open in another program (editor, browser, etc.)\n"
                        f"- Insufficient file permissions\n"
                        f"- File is read-only\n"
                        f"Solution: Close any programs using this file and try again."
                    )
                return f"❌ Permission error: {e}"
            except OSError as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                # Final attempt failed - return detailed error
                error_msg = str(e).lower()
                if 'locked' in error_msg or 'in use' in error_msg or 'being used' in error_msg:
                    return (
                        f"❌ File is locked: {res}\n"
                        f"The file is currently being used by another program.\n"
                        f"Please close any programs that have this file open and try again."
                    )
                elif 'no space' in error_msg or 'disk full' in error_msg:
                    return f"❌ Disk full: Cannot write to {res}. Free up disk space and try again."
                elif 'path too long' in error_msg:
                    return f"❌ Path too long: {res}. Use a shorter path."
                return f"❌ OS error: {e}"
            except Exception as e:
                # Generic error with clear message
                error_type = type(e).__name__
                return f"❌ Error writing file ({error_type}): {e}\nFile: {res}"

class MoveFileTool(BaseTool):
    name = "move_file"
    description = "Moves or renames a file."

    parameters = {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "dst": {"type": "string"}
        },
        "required": ["src", "dst"]
    }

    def run(self, **kwargs) -> str:
        src = kwargs.get('src', '')
        dst = kwargs.get('dst', '')
        
        safe_src, res_src = is_safe_path(src)
        if not safe_src: return f"Source Error: {res_src}"
        
        safe_dst, res_dst = is_safe_path(dst)
        if not safe_dst: return f"Dest Error: {res_dst}"
        
        try:
            shutil.move(res_src, res_dst)
            return f"Moved {res_src} to {res_dst}"
        except Exception as e: return str(e)

class TreeTool(BaseTool):
    name = "tree"
    description = "Generates an ASCII tree view of a directory structure."

    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Root path (default: current dir)"},
            "depth": {"type": "integer", "description": "Max depth (default: 2)"}
        },
        "required": []
    }

    def run(self, **kwargs) -> str:
        path = kwargs.get('path', '.')
        try:
            depth = int(kwargs.get('depth', 2))
        except: depth = 2
        
        safe, res = is_safe_path(path)
        if not safe: return res
        
        if not os.path.exists(res): return "Error: Path not found."
        if not os.path.isdir(res): return "Error: Path is not a directory."
        
        tree_str = f"{os.path.basename(res)}/\n"
        
        for root, dirs, files in os.walk(res):
            level = root.replace(res, '').count(os.sep)
            if level >= depth: 
                del dirs[:] # Stop recursing
                continue
            
            indent = "│   " * (level)
            subindent = "├── "
            
            # Limited output for sanity
            if len(files) + len(dirs) > 50:
                 files = files[:40]
                 files.append(f"... (+{len(files)-40} more)")
            
            for d in dirs:
                tree_str += f"{indent}{subindent}{d}/\n"
            for f in files:
                tree_str += f"{indent}{subindent}{f}\n"
                
        return tree_str

class FinderTool(BaseTool):
    name = "find_files"
    description = "Finds files matching a glob pattern (e.g. *.py) recursively."
    
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Root path to search in"},
            "pattern": {"type": "string", "description": "Glob pattern (e.g. *.json, app.py, *test*)"}
        },
        "required": ["pattern"]
    }

    def run(self, **kwargs) -> str:
        import fnmatch
        path = kwargs.get('path', '.')
        pattern = kwargs.get('pattern', '*')
        
        safe, res = is_safe_path(path)
        if not safe: return res
        
        matches = []
        try:
            for root, dirs, files in os.walk(res):
                for name in files:
                    if fnmatch.fnmatch(name, pattern):
                        full_path = os.path.join(root, name)
                        # Return relative to search root for readability, or absolute?
                        # Using absolute is clearer for tools.
                        matches.append(full_path)
                        
                if len(matches) > 100:
                    break # Safety limit
            
            if not matches: return "No files found matching pattern."
            
            # Limit output
            count = len(matches)
            output = f"Found {count} files:\n" + "\n".join(matches[:50])
            if count > 50: output += f"\n... ({count-50} more)"
            return output
            
        except Exception as e: return str(e)
