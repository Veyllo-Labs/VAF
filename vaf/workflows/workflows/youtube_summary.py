# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
YouTube Summary Workflow

Summarize a YouTube video from its CAPTIONS: yt-dlp (installed into the sandbox
per run) downloads the video's own subtitles - auto-generated or uploaded, no
video/audio download - the sandbox cleans them into a bounded transcript, a
CONTENT_ONLY generation step writes the Markdown summary, write_file saves it
into the chat workspace.

Composed from a live session where the agent built this lane ad-hoc over
host_bash (chat cyan375464): this template runs the same recipe fully inside
the Docker sandbox instead - no host pip install, no per-command confirmations.
If the video has no subtitles at all, the summary step writes an honest note
instead of hallucinating content.

NOTE ON BRACES: the engine substitutes {variables} via regex in EVERY string;
unknown brace-blocks pass through literally, but blocks containing a dot are
treated as nested lookups and fail. The sandbox code below therefore avoids
f-strings and dotted expressions inside braces entirely.
"""

_SUBS_CODE = r"""
import glob
import re
import subprocess
import sys
import time

url = "{video_url}"

meta = subprocess.run(
    ["yt-dlp", "--skip-download", "--no-warnings", "--print", "%(title)s", url],
    capture_output=True, text=True, timeout=60,
)
title = (meta.stdout or "").strip().splitlines()[-1] if meta.stdout else "(Titel unbekannt)"

# One language per request with fallback: a combined de,en,en-orig request fires
# several subtitle downloads at once and trips YouTube's HTTP 429 rate limit far
# more easily (observed live). Stop at the first language that yields a file.
files = []
last_err = ""
for lang in ["de", "en", "en-orig"]:
    r = subprocess.run(
        ["yt-dlp", "--write-subs", "--write-auto-subs", "--sub-langs", lang,
         "--sub-format", "vtt", "--skip-download", "--no-warnings",
         "--sleep-requests", "1", "-o", "/tmp/vsub_" + lang, url],
        capture_output=True, text=True, timeout=120,
    )
    files = sorted(glob.glob("/tmp/vsub_" + lang + "*.vtt"))
    if files:
        break
    last_err = (r.stderr or r.stdout or "")[-300:]
    if "429" in last_err:
        time.sleep(5)

if not files:
    print("TITLE: " + title)
    if "429" in last_err:
        print("RATE_LIMITED_BY_YOUTUBE")
    else:
        print("NO_SUBTITLES_AVAILABLE")
    print(last_err)
    sys.exit(0)

raw = open(files[0], encoding="utf-8", errors="replace").read()
lines = []
seen_last = None
for line in raw.splitlines():
    line = re.sub(r"<[^>]+>", "", line).strip()
    if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
        continue
    if line == seen_last:
        continue
    seen_last = line
    lines.append(line)
text = " ".join(lines)
text = re.sub(r"\s+", " ", text).strip()
if len(text) > 12000:
    text = text[:9000] + " ...[Transkript gekuerzt]... " + text[-2500:]
print("TITLE: " + title)
print("TRANSCRIPT:")
print(text)
"""

WORKFLOW = {
    "name": "YouTube Summary",
    "description": (
        "Summarize a YouTube video from its subtitles/captions (yt-dlp in the sandbox, "
        "no video download, no host installs) and save the summary as Markdown into the "
        "chat workspace. Works only when the video has captions (auto-generated count); "
        "without captions it reports that honestly instead of guessing."
    ),
    "triggers": [
        "fasse das video zusammen",
        "fasse dieses youtube video zusammen",
        "youtube video zusammenfassen",
        "summarize this video",
        "summarize this youtube video",
        "video zusammenfassung",
        "video summary",
    ],
    "trigger_patterns": [
        r"(youtube\.com|youtu\.be).*(zusammenfass|summar)",
        r"(zusammenfass|summar).*(youtube\.com|youtu\.be)",
    ],
    "variables": {
        "video_url": "The full YouTube URL (youtube.com/watch?v=... or youtu.be/...)",
        "filename": "Output Markdown filename (optional, default: youtube_summary.md)",
    },
    "defaults": {
        "filename": "youtube_summary.md",
    },
    "steps": [
        {
            "tool": "python_sandbox",
            "args": {
                "code": _SUBS_CODE,
                "packages": ["yt-dlp"],
                "timeout": 240,
            },
            "input": "{video_url}",
            "output": "transcript",
            "description": "Fetch and clean the video's subtitles in the sandbox (no video download)",
        },
        {
            "tool": "coding_agent",
            "input": (
                "CONTENT_ONLY: Write ONLY the final Markdown text - no project structure, "
                "NO PROJECT files, no code fences around the whole output, no explanations.\n"
                "Task: Fasse das YouTube-Video unten auf DEUTSCH zusammen, basierend "
                "AUSSCHLIESSLICH auf dem mitgelieferten Transkript.\n"
                "Struktur: '# <Videotitel>' als Ueberschrift, eine Zeile '**Quelle:** {video_url}', "
                "dann '## Kurzfassung' (3-5 Saetze), '## Kernpunkte' (Bullets), optional "
                "'## Details' fuer erkennbare Abschnitte, zum Schluss '## Fazit' (1-2 Saetze).\n"
                "WICHTIG: Wenn im Material 'NO_SUBTITLES_AVAILABLE' steht, schreibe NUR einen "
                "kurzen Hinweis, dass das Video keine Untertitel hat und daher nicht "
                "zusammengefasst werden kann. Wenn dort 'RATE_LIMITED_BY_YOUTUBE' steht, "
                "schreibe NUR einen Hinweis, dass YouTube die Abfrage gerade begrenzt "
                "(spaeter erneut versuchen). Erfinde NIEMALS Inhalte.\n\n"
                "Material (Titel + Transkript):\n{transcript}\n"
            ),
            "output": "summary",
            "description": "Write the Markdown summary strictly from the transcript",
            "validate": True,
        },
        {
            "tool": "write_file",
            "args": {
                "path": "{filename}",
                "content": "{summary}",
            },
            "input": "{filename}",
            "output": "saved",
            "description": "Save the summary into the chat workspace",
        },
    ],
}
