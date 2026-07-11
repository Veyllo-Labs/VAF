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
import json
import re
import sys
import time
import urllib.request

import yt_dlp

url = "{video_url}"

# ONE metadata call via the yt_dlp Python API, then ONE fetch of the SIGNED
# caption URL it returns. The previous approach (yt-dlp CLI writing subtitle
# FILES per language) issued several unauthenticated subtitle downloads, which
# YouTube rate-limits far more aggressively - a live run burned 67s of 429s and
# gave up while the caption track existed. This method was discovered by the
# coder sub-agent improvising after exactly that failure.
info = None
last_err = ""
for attempt in [1, 2]:
    try:
        ydl = yt_dlp.YoutubeDL(dict(quiet=True, skip_download=True, no_warnings=True))
        info = ydl.extract_info(url, download=False)
        break
    except Exception as e:
        last_err = str(e)[-300:]
        time.sleep(4)

if info is None:
    print("TITLE: (Titel unbekannt)")
    if "429" in last_err:
        print("RATE_LIMITED_BY_YOUTUBE")
    else:
        print("NO_SUBTITLES_AVAILABLE")
    print(last_err)
    sys.exit(0)

title = info.get("title") or "(Titel unbekannt)"


def pick_track(container):
    if not container:
        return None
    for lang in ["de", "en", "en-orig", "en-US", "en-GB"]:
        tracks = container.get(lang)
        if not tracks:
            continue
        for tr in tracks:
            if tr.get("ext") == "json3":
                return tr.get("url")
        for tr in tracks:
            u = tr.get("url")
            if u:
                return u + "&fmt=json3"
    return None


cap_url = pick_track(info.get("subtitles")) or pick_track(info.get("automatic_captions"))
if not cap_url:
    print("TITLE: " + title)
    print("NO_SUBTITLES_AVAILABLE")
    sys.exit(0)

raw = ""
last_err = ""
for attempt in [1, 2, 3]:
    try:
        raw = urllib.request.urlopen(cap_url, timeout=30).read().decode("utf-8", "replace")
        if raw.strip():
            break
    except Exception as e:
        last_err = str(e)[-200:]
    time.sleep(4)

if not raw.strip():
    print("TITLE: " + title)
    if "429" in last_err:
        print("RATE_LIMITED_BY_YOUTUBE")
    else:
        print("NO_SUBTITLES_AVAILABLE")
    print(last_err)
    sys.exit(0)

text = ""
try:
    data = json.loads(raw)
    texts = []
    for ev in data.get("events") or []:
        for seg in ev.get("segs") or []:
            t = seg.get("utf8")
            if t:
                texts.append(t)
    text = "".join(texts)
except Exception:
    # Not json3 (vtt/srv fallback): strip tags, timestamps and duplicates.
    lines = []
    prev = None
    for line in raw.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if line == prev:
            continue
        prev = line
        lines.append(line)
    text = " ".join(lines)

text = re.sub(r"\s+", " ", text).strip()
if not text:
    print("TITLE: " + title)
    print("NO_SUBTITLES_AVAILABLE")
    sys.exit(0)
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
                "(spaeter erneut versuchen). Erfinde NIEMALS Inhalte.\n"
                "ABSOLUT VERBOTEN: das Transkript oder Videoinhalte SELBST zu beschaffen - "
                "kein python_sandbox, kein web_search, kein Fetch-Versuch. Deine EINZIGE "
                "Aufgabe ist das Schreiben des Markdown-Texts aus dem Material unten; bei "
                "einem Marker schreibst du SOFORT nur den Hinweis und rufst task_done auf. "
                "(Ein frueherer Lauf hat hier 6 Minuten mit eigenen Fetch-Versuchen verbrannt.)\n\n"
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
