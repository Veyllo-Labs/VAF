#!/usr/bin/env python3
"""Check that every relative link in the project's Markdown files resolves.

Hard-fails (exit 1) on any relative Markdown link/image whose target file does not
exist. Warns (non-failing) on source-code line anchors (``...#Lnnn``): these point at
a specific line that rots when the referenced file changes — prefer a plain file link
plus the symbol/method name instead.

External links (http/https/mailto/tel), pure in-page anchors (``#section``) and link
targets inside fenced code blocks are ignored. Stdlib only.

Run:
    python scripts/check_doc_links.py
"""
from __future__ import annotations

import glob
import os
import re
import sys
from collections import defaultdict

# Matches Markdown links and images: [text](target) and ![alt](target)
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
EXCLUDE_DIRS = {"venv", ".venv", "node_modules", ".git"}
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#")


def iter_markdown_files():
    for path in glob.glob("**/*.md", recursive=True):
        if set(path.split(os.sep)) & EXCLUDE_DIRS:
            continue
        yield path


def iter_link_targets(text):
    """Yield raw link targets in ``text``, skipping fenced code blocks."""
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in LINK.finditer(line):
            yield match.group(1)


def normalize(target):
    t = target.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    parts = t.split()  # strip an optional link title:  path "Title"
    return parts[0] if parts else ""


def main():
    broken = []        # (file, raw_target)
    line_anchors = []  # (file, raw_target)
    checked = 0

    for f in iter_markdown_files():
        base = os.path.dirname(f)
        text = open(f, encoding="utf-8", errors="replace").read()
        for raw in iter_link_targets(text):
            target = normalize(raw)
            if not target or target.startswith(SKIP_PREFIXES):
                continue
            checked += 1
            path, _, anchor = target.partition("#")
            if not path:
                continue
            full = os.path.normpath(os.path.join(base, path))
            if not os.path.exists(full):
                broken.append((f, raw))
            if anchor[:1] == "L" and anchor[1:2].isdigit() and not path.endswith(".md"):
                line_anchors.append((f, raw))

    if line_anchors:
        print(f"WARNING: {len(line_anchors)} source-code line anchor(s) "
              "(rot-prone — prefer a file link + the symbol name):")
        for f, raw in line_anchors:
            print(f"  {f}  ->  {raw}")
        print()

    if broken:
        by_file = defaultdict(list)
        for f, raw in broken:
            by_file[f].append(raw)
        print(f"BROKEN: {len(broken)} relative Markdown link(s) do not resolve:\n")
        for f in sorted(by_file):
            print(f"  {f}")
            for raw in by_file[f]:
                print(f"      -> {raw}")
        print(f"\nChecked {checked} relative links; {len(broken)} broken.")
        return 1

    print(f"OK: all {checked} relative Markdown links resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
