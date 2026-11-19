# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
VAF Speech Manager
Handles Offline Text-to-Speech (TTS) and Speech-to-Text (STT) capabilities.
Prioritizes Piper TTS (Neural/Offline) -> pyttsx3 (Robotic/Offline).
"""
import sys
import threading
import time
import os
import platform
import subprocess
import shutil
from pathlib import Path
from typing import Optional

from vaf.core import verified_download
from vaf.core.config import Config
from vaf.cli.ui import UI

# Where a person reports a download that keeps failing its checksum.
REPORT_URL = "https://github.com/Veyllo-Labs/VAF/issues"


class _PiperSwapError(Exception):
    """Renaming the new Piper folder into place failed (on Windows: Piper is running)."""


def download_problem(subject: str, object_: str, error: Exception, then: str = "") -> str:
    """One short line for the person about a failed speech download, by cause: `subject`
    starts the sentence ("The voice de_DE-thorsten-high"), `object_` names the same thing
    inside one ("the voice ..."), `then` is a sentence that follows the cause ("Your current
    Piper is unchanged."). The report link always ends the line, so nothing runs into it.
    The raw error is for a dim detail line, not this one."""
    import requests
    from vaf.core.verified_download import DownloadIntegrityError

    after = f" {then}" if then else ""
    if isinstance(error, DownloadIntegrityError):
        return (f"The download of {object_} did not match its expected checksum, so it was not used.{after} "
                f"If this keeps happening, report it at {REPORT_URL}")
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return (f"{subject} could not be downloaded: the download server could not be reached. Check the "
                f"connection and try again.{after}")
    if isinstance(error, requests.HTTPError):
        return (f"{subject} could not be downloaded: the download server answered with an error "
                f"({http_status(error)}).{after}")
    return f"{subject} could not be installed.{after}"


def http_status(error: Exception):
    """The status code of a requests HTTPError, or None."""
    return getattr(getattr(error, "response", None), "status_code", None)


# How long a Piper install waits for another VAF process that is installing it (the
# download is about 20-26 MB).
PIPER_INSTALL_LOCK_TIMEOUT = 600.0

# NO LOCAL TTS! pyttsx3/comtypes causes 1-4GB RAM explosion via Windows SAPI
# All TTS must go through Docker (speech_tts_engine=docker or chatterbox)

# LAZY IMPORT for speech_recognition (only needed for CLI local STT, not WebUI)
HAS_STT = None
_sr = None

def _lazy_load_sr():
    """Lazy load speech_recognition only when needed (local STT for CLI)."""
    global HAS_STT, _sr
    if HAS_STT is None:
        try:
            import speech_recognition as sr
            _sr = sr
            HAS_STT = True
        except ImportError:
            HAS_STT = False
    return _sr

def tts_unavailable_message() -> str:
    """What speak() says when no lane produced audio and no local voice is left. Without
    local models (inference_profile = api) the Docker TTS it used to recommend does not run
    here; the provider is the lane to check."""
    from vaf.core import inference_profile
    if not inference_profile.local_inference_allowed():
        return ("No text-to-speech on this installation without local models: choose or "
                "check a text-to-speech provider (speech_tts_provider and its key) under "
                "Settings > Voice & Speech.")
    return "Local TTS disabled (causes RAM explosion). Use Docker TTS instead."


def stt_failure_message() -> str:
    """What the terminal mic says when the shared client gave no text. Without local
    models (inference_profile = api) only the provider was asked, so the container and the
    speech stack are no part of the answer."""
    from vaf.core import inference_profile
    if not inference_profile.local_inference_allowed():
        return ("Transcription failed: the speech-to-text provider gave no text, and this "
                "installation has no local engine to fall back to. Choose or check it "
                "(speech_stt_provider and its key) under Settings > Voice & Speech.")
    return ("Transcription failed: no speech-to-text provider gave text and the Whisper "
            "container did not answer - `vaf repair` starts the speech stack.")


def _strip_leading_fillers(text: str, fillers) -> str:
    """Every filler phrase at the start of `text` off, one after the other, with the
    punctuation after each: what the loop that stripped, lowered and sliced the whole rest
    once per phrase per round returned (2.7 s on 4 KB of fillers), by an index that only
    moves forward. `fillers` in the order they are tried (the longest first).

    A slice of the phrase's own length is lowered instead of the whole rest, and must START
    with the lowered phrase: the same answer for every phrase whose lowercase is one
    character for one and holds no sigma and no combining dot (all of them, pinned by
    tests/test_think_strip_sites.py). Any character but U+0130 and a capital sigma lowers one
    for one, whatever follows it. U+0130 lowers to "i" and a combining dot: at the slice's
    last place the dot falls past the phrase, so a phrase ending in "i" matches, as the whole
    lowered rest did ("Vediamo quİ"); anywhere earlier the dot stands inside the phrase,
    which has none. A capital sigma lowers to a sigma, which no phrase has. Only the phrases
    whose lowercase begins with the first character's lowercase are tried, in the same
    order."""
    end = len(text.rstrip())
    pos, matched = 0, False
    by_first = {}
    for phrase in fillers:
        if phrase:
            low = phrase.lower()
            by_first.setdefault(low[0], []).append((phrase, low))
    while True:
        start = pos
        while start < end and text[start].isspace():
            start += 1
        if start >= end:
            break
        for phrase, low in by_first.get(text[start].lower(), ()):
            if text[start:min(end, start + len(phrase))].lower().startswith(low):
                pos = start + len(phrase)
                while pos < end and text[pos] in ".,;: ":
                    pos += 1
                matched = True
                break
        else:
            break
    return text[pos:end] if matched else text


def _drop_standalone_filler(text: str, phrase: str) -> str:
    """`re.sub(r'(?:^|[\\s.])\\s*' + phrase + r'\\s*(?=[\\s.]|$)', ' ', text, flags=re.I)`,
    the filler wherever it stands alone, matched only where it can start.

    The regex tried every blank of a long run as the separator and scanned the rest of the
    run from each (20 s on 4 KB of blanks, once per phrase). Here each occurrence of the
    phrase gets the one start the regex would have found first: the dot right before the
    blank run in front of it, else the run's first blank, else the start of the text, never
    before the end of the previous match. The regex itself then decides at that start."""
    import re
    lit = re.compile(re.escape(phrase), re.IGNORECASE)
    rx = re.compile(r'(?:^|[\s.])\s*' + re.escape(phrase) + r'\s*(?=[\s.]|$)', re.IGNORECASE)
    out, pos = [], 0
    occurrence = lit.search(text, 0)
    while occurrence:
        q = occurrence.start()
        run = q
        while run > pos and text[run - 1].isspace():
            run -= 1
        if run == 0:
            start = 0
        elif run > pos and text[run - 1] == ".":
            start = run - 1
        elif run < q:
            start = run
        else:
            start = None
        m = rx.match(text, start) if start is not None else None
        if m:
            out.append(text[pos:m.start()])
            out.append(" ")
            pos = m.end()
            occurrence = lit.search(text, pos)
        else:
            occurrence = lit.search(text, q + 1)
    out.append(text[pos:])
    return "".join(out)


class SpeechManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.config = Config.load()
        # NO tts_engine - pyttsx3 removed to prevent RAM explosion
        self.stt_recognizer = None
        self.stt_mic = None
        self._is_speaking = False
        self._piper_checked = False
        self._has_piper = False
        self._speech_generation = 0  # To invalidate pending tasks on stop()
        
        # Process handles for safe cleanup
        self._current_player_process = None
        self._current_tts_process = None
        
        # TTS sequencing lock - ensures TTS plays sequentially, not in parallel
        self._tts_lock = threading.Lock()
        
        # Event callbacks
        self.on_speech_loading = None
        self.on_speech_start = None
        self.on_speech_end = None

        # Base paths
        self.base_dir = Path(__file__).parents[2]
        self.media_dir = Path(__file__).parents[1] / "media"
        self.bin_dir = self.base_dir / "bin" / "piper"
        self.models_dir = self.base_dir / "models" / "voices"
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # Lazy: Only init LOCAL STT (microphone) for CLI mode
        # WebUI uses browser audio -> Docker STT, no local mic needed!
        if self.is_local_stt_enabled():
            sr = _lazy_load_sr()
            if sr:
                self.stt_recognizer = sr.Recognizer()
                self._init_mic()

    def _init_mic(self):
        """Initialize microphone based on config."""
        try:
            sr = _lazy_load_sr()
            if not sr:
                self.stt_mic = None
                return
            mic_index = self.config.get("speech_mic_index", None)
            # Ensure index is integer if set
            if mic_index is not None:
                mic_index = int(mic_index)
            self.stt_mic = sr.Microphone(device_index=mic_index)
        except Exception:
            self.stt_mic = None

    def ensure_stt_capture(self) -> bool:
        """Build the LOCAL capture stack (recognizer + microphone) on demand.

        Capture is local by definition - the ENGINE choice only decides where
        the recorded audio is transcribed. The constructor skips mic init for
        the docker engine, which is right for the web server process (its
        audio arrives from the browser) and used to reach `listen()` too:
        with the DEFAULT engine the CLI's microphone was never built, so a
        push-to-talk answered "no speech" within half a second on every
        machine running the docker STT stack - in the terminal app and in the
        classic lane alike.
        """
        if not self.is_stt_enabled():
            return False
        sr = _lazy_load_sr()
        if not sr:
            return False
        if not self.stt_recognizer:
            self.stt_recognizer = sr.Recognizer()
        if not self.stt_mic:
            self._init_mic()
        return self.stt_mic is not None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def list_microphones(self) -> list:
        """List available microphones (MME only on Windows for clean list)."""
        sr = _lazy_load_sr()
        if not sr:
            return []
        try:
            import pyaudio
            p = pyaudio.PyAudio()
            info = p.get_host_api_info_by_index(0) # 0 is usually MME on Windows
            numdevices = info.get('deviceCount')

            filtered = []

            for i in range(0, numdevices):
                device_info = p.get_device_info_by_host_api_device_index(0, i)
                if device_info.get('maxInputChannels') > 0:
                    name = device_info.get('name')
                    # Still apply basic filter for clarity
                    n = name.lower()
                    if "mapper" not in n:
                        filtered.append(f"{i}: {name}")

            p.terminate()
            return filtered
        except:
            # Fallback to standard method if direct pyaudio fails
            return sr.Microphone.list_microphone_names()

    def set_microphone(self, index: int):
        """Set the active microphone index."""
        Config.set("speech_mic_index", index)
        # Update local config dict as well to reflect change immediately
        self.config["speech_mic_index"] = index
        self._init_mic()

    def is_tts_enabled(self) -> bool:
        # Use Config.get() to always read the CURRENT setting (not cached self.config)
        return Config.get("speech_tts_enabled", False)

    def is_stt_enabled(self) -> bool:
        return bool(Config.get("speech_stt_enabled") or Config.get("stt_enabled", False))

    def is_local_stt_enabled(self) -> bool:
        """Check if LOCAL STT (speech_recognition + microphone) is needed.

        Local STT is only needed for CLI mode.
        WebUI uses browser audio -> Docker STT, no local microphone needed.
        """
        if not self.is_stt_enabled():
            return False
        # If engine is "docker", we don't need local STT at all
        #
        # NAMED BOUNDARY - deliberately NOT speech_api.resolve_stt_engine().
        # That resolver answers "where does recorded audio go", this asks
        # "does a microphone have to be opened at startup", and the two only
        # look alike. Folding them together would also let a configured cloud
        # provider close the CLI microphone, and it would need a live mic test
        # on all three platforms to prove. The raw read stays until then.
        engine = Config.get("speech_stt_engine", "docker")
        return engine != "docker"

    def _get_piper_binary(self) -> Optional[Path]:
        """Get path to piper binary depending on OS."""
        system = platform.system().lower()
        if system == "windows":
            return self.bin_dir / "piper.exe"
        else:
            return self.bin_dir / "piper"

    def _check_piper(self) -> bool:
        """Piper is installed as the build this release expects, or it is installed now,
        or this is False.

        The installed Piper counts only when its program bin/piper/piper[.exe] is a regular
        file whose SHA-256 equals the pinned program digest of this platform's archive
        (vaf/core/speech_assets_pin.json; hashed once per process) and no link in bin/piper
        leads outside the folder (verified_download.refuse_escaping_links). Anything else
        (missing, a link, another build, an escaping link left by an older unfiltered
        unpack): the pinned archive is downloaded, verified and swapped in whole
        (_install_piper). Asked once per process, as before.

        Named boundary: the other files of an installed folder (the libraries and
        espeak-ng-data beside the program) are not compared with the archive. That needs a
        digest of the whole unpacked folder per platform, which only the pin script can
        record from the six archives, and the manifest holds none yet. A Piper whose
        program matches is therefore kept with whatever libraries an earlier VAF unpacked
        next to it; a fresh install or a replaced one always gets the verified folder."""
        if self._piper_checked:
            return self._has_piper
        self._has_piper = False
        try:
            entry = self._pinned_piper()
            if entry is not None:
                if self._piper_is_pinned(entry):
                    self._has_piper = True
                else:
                    size = f"{entry['size'] / 1_000_000:.1f} MB"
                    if os.path.lexists(self._get_piper_binary()):
                        UI.event("Speech", "The installed Piper is not the version this VAF release expects; "
                                 f"installing the right one ({size})...", style="warning")
                    else:
                        UI.event("Speech", f"Installing Piper, the offline speech program ({size})...",
                                 style="warning")
                    self._has_piper = self._install_piper(entry)
        except Exception as e:
            UI.error("Piper could not be installed; speech falls back to the other voices.")
            UI.event("System", f"Details: {e}", style="dim")
            self._has_piper = False
        self._piper_checked = True
        return self._has_piper

    def _piper_is_pinned(self, entry: dict) -> bool:
        """The installed program has the pinned digest and no link in its folder leaves it."""
        if os.path.islink(self.bin_dir):
            return False                  # the folder itself must be this install's own, not a link
        if not verified_download.has_pinned_digest(self._get_piper_binary(), entry["program_sha256"]):
            return False
        try:
            verified_download.refuse_escaping_links(str(self.bin_dir), "Piper")
        except (RuntimeError, OSError):
            return False
        return True

    def _piper_asset_name(self) -> str:
        """The name of the Piper release archive for this computer. Exception for a
        platform the Piper release has no archive for."""
        system = platform.system().lower()
        arch = platform.machine().lower()
        if system == "windows":
            if "64" in arch:
                return "piper_windows_amd64.zip"
            raise Exception("Windows 32-bit not supported by auto-installer")
        if system == "linux":
            if "x86_64" in arch:
                return "piper_linux_x86_64.tar.gz"
            if "aarch64" in arch:
                return "piper_linux_aarch64.tar.gz"   # Pi 64
            if "armv7" in arch:
                return "piper_linux_armv7l.tar.gz"    # Pi 32
            raise Exception(f"Linux arch {arch} not supported")
        if system == "darwin":
            # platform.machine() can return x86_64 even on M1/M2 if Python runs under
            # Rosetta 2, so the hardware is asked through sysctl.
            try:
                result = subprocess.run(['sysctl', '-n', 'hw.optional.arm64'],
                                        capture_output=True, text=True, timeout=2)
                is_arm = result.returncode == 0 and result.stdout.strip() == '1'
            except Exception:
                is_arm = "arm64" in arch
            return "piper_macos_aarch64.tar.gz" if is_arm else "piper_macos_x64.tar.gz"
        raise Exception(f"OS {system} not supported")

    def _pinned_piper(self) -> Optional[dict]:
        """This platform's pinned Piper archive (speech_pin.piper_asset), or None after
        saying why there is none: nothing unpinned is ever downloaded."""
        from vaf.core import speech_pin
        name = self._piper_asset_name()
        entry = speech_pin.piper_asset(name)
        if entry is None:
            UI.error(f"Piper has no download for this computer ({platform.system()} {platform.machine()}), "
                     "so it stays uninstalled.")
            UI.event("System", f"No pinned archive {name} in {speech_pin.PIN_PATH.name}", style="dim")
        return entry

    def _install_piper(self, entry: dict) -> bool:
        """Install the pinned Piper archive `entry` into bin/piper, swapping the folder whole.

        Under a file lock next to it (bin/.piper.lock), so two VAF processes never install
        at once; a process that finds the lock held says it waits, and once it holds the
        lock it first checks whether the other one already installed the pinned build. The
        archive is downloaded into bin/piper.staging only as the pinned bytes
        (download_verified), unpacked there with the shared safe unpack (extract_archive:
        data filter, link cap, every link checked after the lift), and its program must be
        a regular file with the pinned program digest. Only then is bin/piper renamed to
        bin/piper.previous and the staging folder renamed into its place; if that second
        rename fails, the old folder is renamed back. bin/piper.previous is removed once the
        new folder is in place. A failure before the swap leaves bin/piper exactly as it
        was, and nothing else in bin/ (bin/llama-cpp, bin/vllm, files at its top) is ever
        touched: every Piper of an earlier VAF lives entirely in bin/piper (the archive
        has one top folder, piper/, and the pin script refuses anything else), so the whole
        folder swap also removes whatever an earlier install left in it.

        Named boundary: on Windows the rename of bin/piper fails while piper.exe speaks;
        the install then keeps the old folder, says so, and the next process retries."""
        from filelock import FileLock, Timeout
        from vaf.core.verified_download import download_verified

        bin_root = self.bin_dir.parent
        staging = bin_root / "piper.staging"
        previous = bin_root / "piper.previous"
        program = self._get_piper_binary().name
        try:
            bin_root.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(bin_root / ".piper.lock"))
            try:
                lock.acquire(timeout=0)
            except Timeout:
                UI.event("Speech", "Waiting for another VAF process that is installing Piper...", style="warning")
                lock.acquire(timeout=PIPER_INSTALL_LOCK_TIMEOUT)
        except Timeout:
            UI.error(f"Another VAF process has been installing Piper for more than "
                     f"{int(PIPER_INSTALL_LOCK_TIMEOUT // 60)} minutes. Try again once it has finished.")
            return False
        except OSError as e:
            UI.error(f"The Piper folder {bin_root} cannot be used ({e}).")
            return False
        try:
            if self._piper_is_pinned(entry):
                return True               # another process installed it while this one waited
            had_piper = os.path.lexists(self._get_piper_binary())
            try:
                for leftover in (staging, previous):
                    verified_download.remove_tree_or_link(str(leftover))
                staging.mkdir(parents=True)
            except OSError as e:
                UI.error(f"Piper could not be installed: a leftover of an earlier install in {bin_root} "
                         f"cannot be removed ({e}). Remove it and try again.")
                return False
            try:
                size_mb = entry["size"] / 1_000_000
                UI.event("Download", f"Downloading the Piper speech program ({size_mb:.1f} MB)...", style="warning")
                archive = download_verified(entry["url"], entry["sha256"], staging / entry["name"],
                                            size=entry["size"], timeout=60)
                UI.event("System", "Extracting Piper...", style="dim")
                verified_download.extract_archive(str(archive), str(staging), program=program, what="Piper")
                staged = staging / program
                if not verified_download.has_pinned_digest(staged, entry["program_sha256"]):
                    raise RuntimeError(f"the archive's {program} is not the pinned program")
                if platform.system().lower() != "windows":
                    os.chmod(staged, 0o755)
                try:
                    moved_aside = False
                    if os.path.lexists(self.bin_dir):
                        os.rename(self.bin_dir, previous)
                        moved_aside = True
                    try:
                        os.rename(staging, self.bin_dir)
                    except OSError:
                        if moved_aside:
                            try:
                                os.rename(previous, self.bin_dir)
                            except OSError:
                                pass      # the next install removes it as a leftover
                        raise
                except OSError as e:
                    raise _PiperSwapError(e) from e
            except _PiperSwapError as e:
                verified_download.remove_quietly(staging)
                if had_piper:
                    UI.error("Piper could not be replaced right now (it may be in use); your current Piper is "
                             "unchanged and VAF tries again on its next start.")
                else:
                    UI.error("Piper could not be put in place right now; VAF tries again on its next start.")
                UI.event("System", f"Details: {e.__cause__}", style="dim")
                return False
            except Exception as e:
                verified_download.remove_quietly(staging)
                UI.error(download_problem("Piper", "Piper", e,
                                          "Your current Piper is unchanged." if had_piper else ""))
                UI.event("System", f"Details: {e}", style="dim")
                return False
            try:
                verified_download.remove_tree_or_link(str(previous))
            except OSError as e:
                UI.event("Speech", f"Piper is installed, but the replaced one could not be removed ({e}). "
                         f"Remove {previous} by hand.", style="warning")
            UI.success("Piper TTS installed and verified.")
            return True
        finally:
            lock.release()

    # Mapping lang -> Piper model name (high/medium quality where available).
    # SSOT for _ensure_voice_model (download) and has_local_voice (the live
    # call's language-follow checks THIS map + on-disk files, no download).
    PIPER_VOICES = {
            # Major Languages
            "en": "en_US-lessac-high",
            "de": "de_DE-thorsten-high",
            "fr": "fr_FR-siwis-medium",
            "es": "es_ES-sharvard-medium",
            "zh": "zh_CN-huayan-medium",
            "it": "it_IT-paola-medium",
            "pt": "pt_BR-faber-medium",
            "ru": "ru_RU-ruslan-medium",
            
            # Nordic
            "da": "da_DK-talesyntese-medium",
            "fi": "fi_FI-harri-medium",
            "is": "is_IS-bui-medium",
            "no": "no_NO-talesyntese-medium",
            "sv": "sv_SE-nst-medium",
            
            # Eastern Europe / Slavic
            "cs": "cs_CZ-jirka-medium",
            "hu": "hu_HU-anna-medium",
            "pl": "pl_PL-darkman-medium",
            "ro": "ro_RO-mihai-medium",
            "sk": "sk_SK-lili-medium",
            "sl": "sl_SI-artur-medium",
            "sr": "sr_RS-serbski_institut-medium",
            "uk": "uk_UA-ukrainian_tts-medium",
            "bg": "bg_BG-dimitar-medium",
            
            # Middle East / Asia
            "ar": "ar_JO-kareem-low",
            "fa": "fa_IR-amir-medium",
            "tr": "tr_TR-dfki-medium",
            "ka": "ka_GE-natia-medium",
            "kk": "kk_KZ-issai-high",
            "ne": "ne_NP-google-medium",
            "vi": "vi_VN-vivos-x_low",
            
            # Others
            "ca": "ca_ES-upc_ona-medium",
            "cy": "cy_GB-gwryw_gogleddol-medium",
            "el": "el_GR-rapunzelina-medium",
            "lb": "lb_LU-marylux-medium",
            "nl": "nl_NL-mls-medium",
            "sw": "sw_CD-lanfrica-medium",
    }

    def _voice_model_name(self, short_lang: str) -> Optional[str]:
        """Piper model name for a 2-letter language, or None if unsupported."""
        return self.PIPER_VOICES.get((short_lang or "")[:2].lower())

    def _ensure_voice_model(self, lang: str) -> Optional[Path]:
        """The pinned ONNX voice for the language, downloaded and verified when needed.

        Both files of the voice (the .onnx model and its .onnx.json settings) come from
        the one rhasspy/piper-voices commit pinned in vaf/core/speech_assets_pin.json, and
        only as the bytes recorded there (fetch_verified): a file already there with the
        pinned digest is kept (hashed once per process), a truncated or foreign one is
        replaced by verified bytes, and bytes that hash differently are never written
        under the voice's name. None, with the reason said, when the voice is not pinned or
        could not be fetched.

        A voice whose download failed its checksum or was refused by the server is not
        fetched again in this process (a 63 to 128 MB transfer would otherwise repeat at
        every spoken reply); the next start of VAF tries again. A connection that could not
        be made is retried at the next reply."""
        from vaf.core import speech_pin
        from vaf.core.verified_download import DownloadIntegrityError, fetch_verified, has_pinned_digest

        # Handle 'auto' -> default to English
        if lang == "auto":
            lang = "en"

        # Default to English if lang not supported
        short_lang = lang[:2].lower()
        model_name = self.PIPER_VOICES.get(short_lang, self.PIPER_VOICES["en"])

        onnx_file = self.models_dir / f"{model_name}.onnx"
        json_file = self.models_dir / f"{model_name}.onnx.json"
        failed = self.__dict__.setdefault("_voice_failed", set())
        if model_name in failed:
            return None
        try:
            pinned = speech_pin.voice_files(model_name)
        except RuntimeError as e:
            UI.error("The list of voices this VAF release may download cannot be read, so no voice is "
                     "downloaded.")
            UI.event("System", f"Details: {e}", style="dim")
            return None
        if pinned is None:
            UI.error(f"The voice {model_name} has no download in this VAF release.")
            return None
        onnx, config = pinned
        try:
            if has_pinned_digest(onnx_file, onnx["sha256"], onnx["size"]) \
                    and has_pinned_digest(json_file, config["sha256"], config["size"]):
                return onnx_file
            UI.event("Download", f"Downloading the voice {model_name} ({onnx['size'] / 1_000_000:.1f} MB)...",
                     style="warning")
            fetch_verified(onnx["url"], onnx["sha256"], onnx_file, size=onnx["size"], timeout=60)
            fetch_verified(config["url"], config["sha256"], json_file, size=config["size"], timeout=60)
            UI.success(f"Voice {model_name} ready.")
            return onnx_file
        except Exception as e:
            import requests
            # Remembered: a checksum failure and a refusal (4xx). A server error (5xx), a
            # timeout or rate limit (408, 429) and a connection that could not be made are
            # transient and retried at the next reply.
            status = http_status(e) if isinstance(e, requests.HTTPError) else None
            refused = status is not None and 400 <= status < 500 and status not in (408, 429)
            if isinstance(e, DownloadIntegrityError) or refused:
                failed.add(model_name)
            UI.error(download_problem(f"The voice {model_name}", f"the voice {model_name}", e))
            UI.event("System", f"Details: {e}", style="dim")
            return None

    def _play_audio(self, file_path: str):
        """Play WAV/MP3 file (OS-independent)."""
        system = platform.system().lower()
        try:
            if system == "windows":
                # Check file extension to determine player
                if file_path.lower().endswith('.mp3'):
                    # For MP3: Use Windows Media Player (wmplayer.exe) in silent mode
                    # Alternative: Use PowerShell with Windows.Media.Playback (Win10+)
                    try:
                        # Try modern PowerShell method first (Win10+)
                        ps_script = f"""
Add-Type -AssemblyName PresentationCore
$player = New-Object System.Windows.Media.MediaPlayer
$player.Open([System.Uri]::new('{file_path}'))
$player.Play()
Start-Sleep -Milliseconds 500
while ($player.NaturalDuration.HasTimeSpan -eq $false) {{ Start-Sleep -Milliseconds 100 }}
$duration = $player.NaturalDuration.TimeSpan.TotalSeconds
Start-Sleep -Seconds $duration
$player.Stop()
$player.Close()
"""
                        self._current_player_process = subprocess.Popen(
                             ['powershell', '-NoProfile', '-Command', ps_script],
                             creationflags=subprocess.CREATE_NO_WINDOW
                        )
                        self._current_player_process.wait(timeout=5)
                    except:
                        # Fallback: Use mplayer/vlc if installed, or just skip
                        pass
                else:
                    # For WAV: Use SoundPlayer (works reliably)
                    cmd = f"(New-Object Media.SoundPlayer '{file_path}').PlaySync()"
                    self._current_player_process = subprocess.Popen(
                        ['powershell', '-c', cmd], 
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    self._current_player_process.wait()

            elif system == "darwin":
                # macOS: afplay supports both WAV and MP3
                self._current_player_process = subprocess.Popen(['afplay', file_path])
                self._current_player_process.wait()

            else:
                # Linux: Try multiple players (mpg123 for MP3, aplay for WAV)
                if file_path.lower().endswith('.mp3'):
                    # Try mpg123 first, then mpv, then ffplay, then cvlc/vlc
                    players = ['mpg123', 'mpv', 'ffplay', 'cvlc', 'vlc']
                    for player in players:
                        try:
                            if player == 'mpv':
                                cmd = [player, '--no-video', '--really-quiet', file_path]
                            elif player == 'ffplay':
                                cmd = [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', file_path]
                            elif player in ('cvlc', 'vlc'):
                                cmd = [player, '--intf', 'dummy', '--play-and-exit', '-q', file_path]
                            else:
                                cmd = [player, '-q', file_path]
                            
                            self._current_player_process = subprocess.Popen(cmd)
                            self._current_player_process.wait(timeout=5)
                            break
                        except (FileNotFoundError, subprocess.CalledProcessError):
                            continue
                else:
                    # WAV: Use aplay
                    self._current_player_process = subprocess.Popen(['aplay', '-q', file_path])
                    self._current_player_process.wait()
        except:
            pass  # Silently fail - sound is optional
        finally:
            self._current_player_process = None
    
    def _play_success_sound(self):
        """Play success sound after successful STT (OS-independent)."""
        try:
            # Find the sound file
            sound_path = self.media_dir / "sounds" / "sst.mp3"
            
            if not sound_path.exists():
                return  # No sound file, skip silently
            
            # Play in background thread to avoid blocking
            import threading
            def play_worker():
                self._play_audio(str(sound_path))
            
            thread = threading.Thread(target=play_worker, daemon=True)
            thread.start()
        except:
            pass  # Silently fail - sound is optional
    
    def play_answer_ready_sound(self):
        """Play sound when agent has finished thinking and answer is ready (OS-independent)."""
        try:
            # Find the sound file
            sound_path = self.media_dir / "sounds" / "tts01.mp3"
            
            if not sound_path.exists():
                return  # No sound file, skip silently
            
            # Play in background thread to avoid blocking
            import threading
            def play_worker():
                self._play_audio(str(sound_path))
            
            thread = threading.Thread(target=play_worker, daemon=True)
            thread.start()
        except:
            pass  # Silently fail - sound is optional

    def has_local_voice(self, lang: str) -> bool:
        """True when a Piper voice for `lang` is ALREADY downloaded (no
        download side effect). Used by the live call's language-follow: the
        agent switches its spoken language mid-call only when the matching
        voice exists locally."""
        try:
            short = (lang or "")[:2].lower()
            model_name = self._voice_model_name(short)
            if not model_name:
                return False
            return ((self.models_dir / f"{model_name}.onnx").exists()
                    and (self.models_dir / f"{model_name}.onnx.json").exists())
        except Exception:
            return False

    def call_lane_speaks(self, lang: str) -> bool:
        """Can the lane the LIVE CALL actually synthesizes with speak `lang`?

        The call runs force_engine="docker": cloud TTS provider lane first,
        else the tts-multilang container - NOT the host Piper store that
        has_local_voice checks (audit finding: gating the language follow on
        the host store either never triggered on standard installs or made
        the TEXT switch language while the container spoke its fallback
        voice). Cloud providers are broadly multilingual -> True; the
        container is asked for its INSTALLED languages. Fail-closed: an
        unreachable container means no language switch this turn.
        """
        short = (lang or "")[:2].lower()
        if not short:
            return False
        try:
            from vaf.core.config import Config
            if (Config.get("speech_tts_provider", "") or "").strip():
                return True
            import requests as _rq
            base = Config.get("speech_tts_docker_url", "http://localhost:5002")
            r = _rq.get(f"{base}/languages", timeout=2)
            if r.status_code != 200:
                return False
            data = r.json()
            items = data.get("languages", data) if isinstance(data, dict) else data
            for it in (items or []):
                if isinstance(it, dict) and str(it.get("code") or "")[:2].lower() == short:
                    return bool(it.get("installed"))
            return False
        except Exception:
            return False

    def synthesize_audio(self, text: str, lang: str = "auto", force_engine: Optional[str] = None,
                         *, user_key: Optional[str] = None,
                         deadline_s: Optional[float] = None, cancelled=None, on_wait=None,
                         busy_raises: bool = False) -> Optional[bytes]:
        """Synthesize text to audio bytes (WAV) without playing. Used for Web UI streaming.
        When force_engine is set (e.g. 'docker'), use that engine; otherwise use config.
        Web UI always uses force_engine='docker' so audio is streamed to the client, never played on server.

        Every local lane waits in its fair queue, taken exactly once (speech_client: `tts`
        or `tts_chatterbox` for the HTTP lane, `tts_piper` here for the binary). The
        keyword arguments go to that queue; `deadline_s` defaults to
        speech_client.WAIT_BACKGROUND_S. A refusal returns None, or raises
        fair_queue.QueueBusy with `busy_raises=True`."""
        from vaf.core import speech_client as _sc
        if deadline_s is None:
            deadline_s = _sc.WAIT_BACKGROUND_S
        queue_args = dict(user_key=user_key, deadline_s=deadline_s, cancelled=cancelled,
                          on_wait=on_wait, busy_raises=busy_raises)
        # Skip TTS check if force_engine is set (caller already verified TTS is enabled)
        if not force_engine and not self.is_tts_enabled():
            print(f"[TTS Debug] TTS disabled, returning None")
            return None

        clean_text = self._clean_markdown(text)
        if not clean_text.strip():
            print(f"[TTS Debug] Empty text after cleaning, returning None")
            return None

        preferred_engine = (force_engine or Config.get("speech_tts_engine", "docker")).lower()
        lang_short = (lang or "en")[:2].lower()

        print(f"[TTS Debug] Synthesizing: engine={preferred_engine}, lang={lang_short}, text_len={len(clean_text)}")

        # Cloud provider lane (speech_tts_provider) - consulted BEFORE the engine
        # dispatch, mirroring vision_provider. force_engine only selects the local
        # fallback engine; a configured provider takes precedence. Never raises;
        # None falls through to the local engine below.
        # Only when a provider is set, as speech_client.synthesize asks: otherwise every
        # local request would count as a served `tts_api` request in the health numbers.
        from vaf.core import speech_api as _speech_api
        api_audio = None
        if _speech_api.select_tts_backend()[0]:
            api_audio = _sc.synthesize_api(clean_text, lang_short, want_format="wav",
                                           user_key=user_key)
        if api_audio:
            print(f"[TTS Debug] Cloud provider TTS SUCCESS ({len(api_audio)} bytes)")
            return api_audio

        # Without local models (inference_profile = api) neither the container nor the
        # Piper binary runs here; a server the admin runs elsewhere (chatterbox) and the
        # macOS voice download nothing and stay.
        from vaf.core import inference_profile
        if preferred_engine in ("docker", "piper") and not inference_profile.local_inference_allowed():
            print(f"[TTS Debug] {inference_profile.refusal('the local text-to-speech')}")
            return None

        # Docker/Chatterbox TTS: HTTP service (e.g. Piper in Docker or Chatterbox).
        # New: Multi-language All-in-One container uses /synthesize endpoint with language auto-detection.
        if preferred_engine in ("docker", "chatterbox"):
            from vaf.core import speech_client
            # Chatterbox has its own URL config
            if preferred_engine == "chatterbox":
                url = speech_client.chatterbox_base_url()
            else:
                # Use the single TTS URL - multi-lang container handles language internally
                url = (Config.get("speech_tts_docker_url") or "http://localhost:5002").strip().rstrip("/")

            print(f"[TTS Debug] Docker TTS URL: {url}")

            if url:
                # Shared client: /synthesize POST with legacy direct-POST +
                # base64-JSON fallback preserved (never raises, returns None).
                body = speech_client.synthesize_docker(clean_text, lang_short, want_format="wav",
                                                       base_url=url, **queue_args)
                if body:
                    print(f"[TTS Debug] SUCCESS - returning WAV audio ({len(body)} bytes)")
                    return body
                print("[TTS Debug] Docker TTS returned no audio")
            return None

        if preferred_engine == "piper" and self._check_piper():
            model_path = self._ensure_voice_model(lang)
            if model_path:
                import tempfile
                from vaf.core.fair_queue import QueueBusy
                try:
                    piper_slot = _sc.speech_queue("tts_piper").slot(
                        user_key, deadline_s=deadline_s, cancelled=cancelled, on_wait=on_wait)
                    piper_slot.__enter__()
                except QueueBusy:
                    if busy_raises:
                        raise
                    return None
                wav_path = ""
                try:
                    fd, wav_path = tempfile.mkstemp(prefix="vaf_", suffix=".wav")
                    os.close(fd)
                    binary = self._get_piper_binary()
                    
                    # Run Piper
                    # Use subprocess.run for synchronous execution (we need the file now)
                    # Ensure NO WINDOW is spawned on Windows
                    run_args = {}
                    if os.name == 'nt':
                        run_args["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                        si = subprocess.STARTUPINFO()
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        si.wShowWindow = subprocess.SW_HIDE
                        run_args["startupinfo"] = si
                    
                    subprocess.run(
                        [str(binary), "--model", str(model_path), "--output_file", wav_path],
                        input=clean_text.encode('utf-8'),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                        **run_args
                    )
                    
                    # Read the generated WAV file
                    if os.path.exists(wav_path) and os.path.getsize(wav_path) > 1000:
                        with open(wav_path, "rb") as f:
                            audio_data = f.read()
                        return audio_data
                        
                except Exception as e:
                    from vaf.cli.ui import UI
                    UI.event("Debug", f"Piper synthesis failed: {e}", style="dim")
                finally:
                    piper_slot.__exit__(None, None, None)
                    if wav_path and os.path.exists(wav_path):
                        try:
                            os.remove(wav_path)
                        except:
                            pass
                            
        return None

    def stop(self):
        """Stop speaking immediately."""
        self._is_speaking = False
        self._speech_generation += 1  # Invalidate pending tasks
        
        # Kill specific player process if running
        if self._current_player_process:
            try:
                self._current_player_process.terminate()
                # If terminate doesn't work, we could try kill, but terminate is usually enough
                self._current_player_process = None
            except:
                pass

        # Kill specific TTS process (Piper) if running
        if self._current_tts_process:
            try:
                self._current_tts_process.terminate()
                self._current_tts_process = None
            except:
                pass
                    
        # NO pyttsx3 - removed to prevent RAM explosion

    def release_tts_resources(self):
        """
        Release TTS-related resources to free memory (e.g. during emergency cleanup).
        Stops any playback, kills Piper subprocess.
        """
        self.stop()

    def speak(self, text: str, lang: str = "auto"):
        """Speak text using Docker TTS or Piper. NO local pyttsx3 (causes RAM explosion)."""
        if not self.is_tts_enabled(): return

        clean_text = self._clean_markdown(text)
        if not clean_text.strip(): return

        # Capture generation ID to detect stop() calls while waiting
        current_gen = self._speech_generation

        # DEBUG: Show what is actually being sent to TTS
        # UI.event("TTS Debug", f"Speaking ({len(clean_text)} chars): {clean_text[:100]}...", style="dim")

        def _speak_worker():
            # CRITICAL: Acquire TTS lock to ensure sequential playback
            # This prevents multiple TTS calls from overlapping
            with self._tts_lock:
                if self.on_speech_loading:
                    try: self.on_speech_loading()
                    except: pass
                
                # ABORT if stop() was called while we were waiting
                if self._speech_generation != current_gen:
                    return

                # Check user's preferred TTS engine
                preferred_engine = Config.get("speech_tts_engine", "docker")

                # 1a. Docker/Chatterbox TTS (HTTP service – no local model, good for Docker stack)
                # "chatterbox" is also HTTP-based, treat it like docker.
                # A configured cloud provider (speech_tts_provider) also takes this
                # path even when the local engine is piper/system: synthesize_audio
                # consults the provider first and degrades to the engine on failure.
                _cloud_tts_configured = False
                try:
                    from vaf.core import speech_api
                    _cloud_tts_configured = bool(speech_api.select_tts_backend()[0])
                except Exception:
                    pass
                if preferred_engine in ("docker", "chatterbox") or _cloud_tts_configured:
                    audio_bytes = self.synthesize_audio(clean_text, lang)
                    if audio_bytes:
                        try:
                            import tempfile
                            fd, wav_path = tempfile.mkstemp(prefix="vaf_", suffix=".wav")
                            os.close(fd)
                            with open(wav_path, "wb") as f:
                                f.write(audio_bytes)
                            if self.on_speech_start:
                                try: self.on_speech_start(clean_text)
                                except: pass
                            self._play_audio(wav_path)
                            if self.on_speech_end:
                                try: self.on_speech_end()
                                except: pass
                            try: os.remove(wav_path)
                            except: pass
                            return
                        except Exception as e:
                            from vaf.cli.ui import UI
                            UI.event("Debug", f"Docker TTS play failed: {e}", style="dim")
                    # Fall through to system/piper
                
                # 1b. Piper (High Quality) - local binary. It downloads the binary and the
                # voices, so not on an installation without local models.
                from vaf.core import inference_profile as _profile
                if (preferred_engine == "piper" and _profile.local_inference_allowed()
                        and self._check_piper()):
                    # Attempt to get voice for requested language
                    model_path = self._ensure_voice_model(lang)
                    
                    # Do not fallback to English here. 
                    # If the requested language model is missing, we prefer to fall through 
                    # to pyttsx3 (System TTS) which might have the correct language installed,
                    # rather than speaking German/French text with an English accent.

                    if model_path:
                        try:
                            import tempfile
                            fd, wav_path = tempfile.mkstemp(prefix="vaf_", suffix=".wav")
                            os.close(fd)

                            binary = self._get_piper_binary()
                            
                            # Prepare flags for no window
                            popen_args = {}
                            if os.name == 'nt':
                                popen_args["creationflags"] = 0x08000000 # CREATE_NO_WINDOW
                                si = subprocess.STARTUPINFO()
                                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                                si.wShowWindow = subprocess.SW_HIDE
                                popen_args["startupinfo"] = si

                            # Run Piper
                            self._current_tts_process = subprocess.Popen(
                                [str(binary), "--model", str(model_path), "--output_file", wav_path],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                **popen_args
                            )
                            self._current_tts_process.communicate(input=clean_text.encode('utf-8'))
                            self._current_tts_process = None
                            
                            # Check if WAV file was generated (even if Piper crashed)
                            # Sometimes Piper returns non-zero but still produces valid audio
                            if os.path.exists(wav_path) and os.path.getsize(wav_path) > 1000:  # At least 1KB
                                if self.on_speech_start:
                                    try: self.on_speech_start(clean_text)
                                    except: pass
                                
                                self._play_audio(wav_path)
                                
                                if self.on_speech_end:
                                    try: self.on_speech_end()
                                    except: pass

                                try:
                                    os.remove(wav_path) 
                                except:
                                    pass
                                return # Success!
                            # else: Piper failed, silently fall through to system voice
                        except Exception as e:
                            # Piper error, silently fall through to system voice
                            from vaf.cli.ui import UI
                            UI.event("Debug", f"Piper TTS failed: {e}", style="dim")
                            pass

                # 2. Fallback: pyttsx3 (Robotic but reliable) - OR 'say' on macOS
                # macOS: pyttsx3 is unstable on background threads (NSSpeechSynthesizer requires main loop)
                # So we use the native 'say' command which is robust and thread-safe.
                if platform.system().lower() == "darwin":
                    try:
                        voice_arg = []
                        # Map lang to voice (basic mapping)
                        # German: 'Anna' (standard), 'Markus'
                        # English: 'Samantha' (standard), 'Alex'
                        if lang.startswith("de"):
                            voice_arg = ["-v", "Anna"]
                        elif lang.startswith("en"):
                            # Default is usually good, but we can enforce one
                            pass 
                            
                        cmd = ["say"] + voice_arg + [clean_text]
                        
                        if self.on_speech_start:
                            try: self.on_speech_start(clean_text)
                            except: pass
                            
                        subprocess.run(cmd, check=True)
                        
                        if self.on_speech_end:
                            try: self.on_speech_end()
                            except: pass
                            
                    except Exception as e:
                        # If 'say' fails, we are really out of options
                        pass
                
                else:
                    # NO LOCAL TTS! pyttsx3 causes 1-4GB RAM explosion via Windows SAPI/comtypes
                    # User must use Docker TTS (speech_tts_engine=docker or chatterbox)
                    from vaf.cli.ui import UI
                    UI.warning(tts_unavailable_message())
                    return

        threading.Thread(target=_speak_worker, daemon=True).start()

    def listen(self, prompt: str = "Listening...", timeout: int = 10, lang: str = None,
               on_state=None, should_stop=None) -> Optional[str]:
        """
        Listens to microphone (STT).
        If lang is None, uses config 'speech_language' or defaults to 'en-US'.

        `on_state` separates CAPTURE from PRESENTATION. Without it this method
        paints its own level meter with raw cursor codes on stdout - fine when
        the caller owns a plain terminal (the classic lane, the tray's mic
        test), fatal under an alternate screen, which is why no full-screen
        surface could ever host voice input. With a callback, nothing is
        painted: each phase change and level tick arrives as
        `on_state(phase, energy, threshold)` with phase one of "calibrating",
        "recording", "speaking", "processing", "timeout" - data the caller
        renders wherever it lives. The callback is an OBSERVER: it runs on the
        capture thread, and an exception in it is swallowed rather than ending
        the recording (a broken meter must not eat the utterance).

        `should_stop` is a cooperative cancel the painted path never had:
        checked once per chunk, a truthy answer abandons the capture and
        returns None. Errors and warnings keep going through UI.* either way -
        those are routed, not painted.
        """
        if not self.ensure_stt_capture():
            return None

        def _tell(phase: str, energy: float = 0.0, threshold: float = 0.0) -> None:
            if on_state is None:
                return
            try:
                on_state(phase, energy, threshold)
            except Exception:
                pass
        
        # CRITICAL: Stop TTS before starting STT to prevent interference
        # If we're currently speaking, the microphone will pick it up and cause feedback
        self.stop()

        # Determine language
        if not lang:
            # CRITICAL: Reload config to get latest language setting
            # (user may have changed it in settings after SpeechManager was initialized)
            from vaf.core.config import Config
            fresh_config = Config.load()
            lang = fresh_config.get("speech_language", "en-US")

        # ... (Locale mapping same as before) ...
        locale_map = {
            "de": "de-DE", "en": "en-US", "tr": "tr-TR", "zh": "zh-CN",
            "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "ru": "ru-RU"
        }
        if len(lang) == 2:
            locale = locale_map.get(lang, "en-US")
        else:
            locale = lang

        try:
            import math
            import struct
            
            # Show Language explicitly in UI (painted path only - the callback
            # caller renders its own chrome)
            if on_state is None:
                UI.event("Speech", f"Listening ({locale})...", style="dim")

            def _calculate_rms(data):
                """Calculate RMS amplitude for 16-bit PCM data."""
                count = len(data) // 2
                if count == 0: return 0
                shorts = struct.unpack(f"{count}h", data)
                sum_squares = sum(s**2 for s in shorts)
                return int(math.sqrt(sum_squares / count))

            with self.stt_mic as source:
                # 1. Calibration
                _tell("calibrating")
                if on_state is None:
                    # UI's console removes the CR of end="\r" like every control character;
                    # the recording line below starts with its own "\r\033[K" anyway.
                    UI.print(f"[dim]Calibrating noise...[/dim]", end="\r")
                self.stt_recognizer.adjust_for_ambient_noise(source, duration=1.0)
                threshold = self.stt_recognizer.energy_threshold

                # 2. Recording Loop with Visuals
                _tell("recording", 0, threshold)
                if on_state is None:
                    sys.stdout.write(f"\r\033[K● Recording ({locale})   ")
                    sys.stdout.flush()

                frames = []
                start_time = time.time()
                # Turn boundaries come from the shared TurnDetector (voice_vad.py) -
                # this loop used to carry its own inline silence timer (a bare 1.5s
                # literal), the THIRD hand-rolled copy of the same decision next to
                # the browser's SILENCE_MS and the never-called primitive. The energy
                # measurement (RMS vs the calibrated threshold) stays here; only the
                # boundary logic moved. min_speech stays 0: this lane's contract keeps
                # its own timeout for "no speech at all" and treats any voiced audio
                # as a turn, exactly as the inline version did.
                from vaf.core.voice_vad import TurnDetector
                detector = TurnDetector(min_speech_ms=0.0,
                                        max_utter_ms=max(timeout, 60.0) * 1000.0)
                has_spoken = False

                while True:
                    if should_stop is not None and should_stop():
                        if on_state is None:
                            sys.stdout.write("\r\033[K")
                        return None
                    # Check timeout (no speech start)
                    if not has_spoken and (time.time() - start_time > timeout):
                        _tell("timeout")
                        if on_state is None:
                            sys.stdout.write("\r\033[K\n") # Clear line
                            UI.print("[yellow]Timeout: No speech detected.[/yellow]")
                        return None

                    # Read Chunk
                    buffer = source.stream.read(source.CHUNK)
                    if len(buffer) == 0: break
                    frames.append(buffer)

                    # Calculate Energy (RMS)
                    energy = _calculate_rms(buffer)

                    # Visual Bar (Logarithmic scale)
                    bar_len = int(math.log(energy + 1) * 2)
                    bar = "█" * min(bar_len, 20)

                    # Visual feedback if speaking
                    voiced = energy > threshold
                    status = "● Recording"
                    if voiced:
                        status = "● SPEAKING "
                        has_spoken = True
                    event = detector.feed(voiced, time.time() * 1000.0)
                    if has_spoken and event in ("end", "discard"):
                        break

                    _tell("speaking" if voiced else "recording",
                          energy, threshold)
                    if on_state is None:
                        # Direct stdout for smooth animation without newline
                        # spam; \033[K clears the line from cursor to end.
                        # Plain text on purpose: this is a RAW fd, and the
                        # markup tags that used to sit here were printed
                        # literally, brackets and all.
                        sys.stdout.write(f"\r\033[K{status} [{bar:<20}] ({int(energy)}/{int(threshold)}) ")
                        sys.stdout.flush()

                _tell("processing")
                if on_state is None:
                    sys.stdout.write("\r\033[K") # Clear recording line
                    UI.print("[dim]Processing...[/dim]")
                
                # Convert frames to AudioData
                sr = _lazy_load_sr()
                if not sr:
                    UI.error("speech_recognition not available")
                    return None
                frame_data = b"".join(frames)
                audio = sr.AudioData(frame_data, source.SAMPLE_RATE, source.SAMPLE_WIDTH)

                # One resolver decides where this audio goes, the same one the
                # web mic asks: a configured cloud provider wins, otherwise the
                # engine the user chose. "docker" means the shared client
                # (cloud lane first, then the local Whisper container - the same
                # path Telegram and WhatsApp voice take). A failure is NAMED and
                # never silently rerouted to Google's free web API: the user
                # picked local processing.
                from vaf.core import speech_api as _speech_api
                if _speech_api.resolve_stt_engine() == "docker":
                    from vaf.core import speech_client
                    api_text, _api_lang = speech_client.transcribe(
                        audio.get_wav_data(), mime="audio/wav", filename="mic.wav")
                    if api_text:
                        self._play_success_sound()
                        return api_text
                    UI.warning(stt_failure_message())
                    return None

                # No cloud lane call here any more: a resolving provider already
                # sent this audio through the shared client above, which tries
                # the very same provider first and then the container. Reaching
                # this point means engine "local" AND no provider, so Google's
                # free Web API below is the only remaining recognizer.
                try:
                    text = self.stt_recognizer.recognize_google(audio, language=locale)

                    # SUCCESS! Play success sound
                    if text:
                        self._play_success_sound()

                    return text
                except sr.UnknownValueError:
                    UI.warning("Audio captured but not understood (UnknownValueError).")
                    return None
                except sr.RequestError as e:
                    UI.error(f"STT API Error: {e}")
                    return None
                    
        except Exception as e:
            UI.error(f"Mic Error: {e}")
            return None

    def _clean_markdown(self, text: str) -> str:
        """Removes markdown symbols, emojis, and thinking blocks for natural speech."""
        import re

        from vaf.core.completion import strip_think_blocks

        # 1. Remove the marked reasoning: never read aloud
        t = strip_think_blocks(text or "")
        
        # 2a. Strip filler phrases (Web UI TTS: don't read "Einen Moment", "Just a moment" etc.)
        try:
            from vaf.core.speech_fillers import THINKING_FILLERS
            all_fillers = []
            for lang_fillers in THINKING_FILLERS.values():
                all_fillers.extend(f for f in lang_fillers if f and isinstance(f, str))
            all_fillers = sorted(set(all_fillers), key=len, reverse=True)
            # Strip leading fillers
            t = _strip_leading_fillers(t, all_fillers)
            # Remove filler phrases anywhere (standalone)
            for phrase in all_fillers:
                if phrase:
                    t = _drop_standalone_filler(t, phrase)
            t = re.sub(r' +', ' ', t).strip()
        except Exception:
            pass

        # 3. Remove introductory labels (Answer:, Antwort:, etc.)
        # Only at the very start of the string
        start_patterns = [
            r'^(?:Here is the )?Answer:\s*',
            r'^(?:Hier ist die )?Antwort:\s*',
            r'^Response:\s*',
            r'^Solution:\s*',
            r'^Lösung:\s*',
        ]
        for pattern in start_patterns:
            t = re.sub(pattern, '', t, flags=re.IGNORECASE).strip()

        # 4. Handle Code Blocks and Inline Code intelligently
        # Multi-line code blocks: Remove completely (too long to speak)
        t = re.sub(r'```.*?```', '', t, flags=re.DOTALL)
        
        # Inline code: Keep short code, remove long code
        def replace_inline_code(match):
            code = match.group(1)
            # If code is short (< 20 chars) and looks like a variable/command, keep it
            if len(code) < 20 and not any(char in code for char in ['\n', '{', '}', '(', ')', '[', ']']):
                return code  # Keep short inline code (e.g., `vaf run`, `settings.py`)
            else:
                return ''  # Remove long/complex code
        
        t = re.sub(r'`([^`]+)`', replace_inline_code, t)
        
        # 5. Remove Emojis (all Unicode emoji ranges)
        # Comprehensive emoji removal covering all Unicode blocks
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags (iOS)
            "\U00002702-\U000027B0"  # dingbats
            "\U000024C2-\U0001F251"  # enclosed characters
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U0001FA00-\U0001FA6F"  # chess symbols
            "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-A
            "\U00002600-\U000026FF"  # miscellaneous symbols
            "\U00002700-\U000027BF"  # dingbats
            "\U0001F018-\U0001F270"  # various asian characters
            "\U0001F300-\U0001F5FF"  # misc symbols and pictographs
            "]+",
            flags=re.UNICODE
        )
        t = emoji_pattern.sub('', t)
        
        # 6. Convert dates to natural speech (DD.MM.YYYY or DD.MM.YY)
        def convert_date_to_speech(match):
            """Convert dates like 11.01.2026 to natural speech."""
            day = match.group(1)
            month = match.group(2)
            year = match.group(3)
            
            # Month names in German (since most of VAF is in German context)
            month_names_de = {
                "01": "Januar", "02": "Februar", "03": "März", "04": "April",
                "05": "Mai", "06": "Juni", "07": "Juli", "08": "August",
                "09": "September", "10": "Oktober", "11": "November", "12": "Dezember"
            }
            
            # Month names in English
            month_names_en = {
                "01": "January", "02": "February", "03": "March", "04": "April",
                "05": "May", "06": "June", "07": "July", "08": "August",
                "09": "September", "10": "October", "11": "November", "12": "December"
            }
            
            # Try to detect language from context (simple heuristic)
            # For now, default to German (can be enhanced later)
            month_names = month_names_de
            
            day_int = int(day)
            month_name = month_names.get(month, month)
            
            # Format: "elfter Januar zweitausendsechsundzwanzig" is too formal
            # Better: "11. Januar 2026" or just speak numbers naturally
            if len(year) == 4:
                # Full year: "11. Januar 2026"
                return f"{day_int}. {month_name} {year}"
            else:
                # Short year: "11. Januar 26"
                return f"{day_int}. {month_name} {year}"
        
        # Match dates: DD.MM.YYYY or DD.MM.YY
        t = re.sub(r'\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b', convert_date_to_speech, t)
        
        # 7. Clean Markdown & Structure for Natural Reading
        # Shorten Links: https://www.veyllo.io/test -> veyllo.io
        t = re.sub(r'https?://(?:www\.)?([^/\s]+)(?:/[^\s]*)?', r'\1', t)
        
        t = t.replace('**', '').replace('__', '') # Bold
        t = t.replace('*', '').replace('_', '')   # Italic (careful not to remove list bullets yet)
        
        # Remove Headers (## Title -> Title)
        t = re.sub(r'^#+\s+', '', t, flags=re.MULTILINE)
        
        # Clean List Items for natural pauses
        # "- Item" -> "Item." (replaces dash with nothing, ensures pause at end)
        lines = []
        for line in t.split('\n'):
            l = line.strip()
            # Remove list bullets
            if l.startswith(('-', '*', '•', '+')):
                l = l.lstrip('-*•+ ').strip()
                # Add period if missing for pause
                if l and not l[-1] in '.?!:;':
                    l += '.'
            lines.append(l)
        t = '\n'.join(lines)
        
        # Collapse multiple newlines to avoid too long silence
        t = re.sub(r'\n{3,}', '\n\n', t)
        
        # 8. Don't send literal backslash sequences to TTS (e.g. \n, \66 read aloud as "backslash n", "backslash 66")
        t = t.replace('\\n', '\n').replace('\\t', ' ').replace('\\r', ' ')
        t = re.sub(r'\\[0-7]{1,3}', ' ', t)   # octal \66 etc. -> space
        t = re.sub(r'\\x[0-9a-fA-F]{2}', ' ', t)  # \x41 etc. -> space
        t = re.sub(r'\\[a-zA-Z]', ' ', t)     # \n \t \s \d etc. (single letter) -> space
        t = re.sub(r'\\+', ' ', t)            # any remaining backslashes -> space
        # 9. Remove literal word "backslash" and "backslash X" so TTS doesn't say "backslash n", "backslash backslash"
        t = re.sub(r'\bbackslash\s+backslash\b', ' ', t, flags=re.IGNORECASE)
        t = re.sub(r'\bbackslash\s+(?:n|t|r|d|s|a|b|f|v)\b', ' ', t, flags=re.IGNORECASE)
        t = re.sub(r'\bbackslash\s+[0-9]+\b', ' ', t, flags=re.IGNORECASE)
        t = re.sub(r'\bbackslash\s+[a-z]\b', ' ', t, flags=re.IGNORECASE)
        t = re.sub(r'\bbackslash\b', ' ', t, flags=re.IGNORECASE)
        t = re.sub(r' +', ' ', t).strip()
        
        return t.strip()

def get_speech_manager() -> SpeechManager:
    return SpeechManager.get_instance()