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
import zipfile
import tarfile
import requests
from pathlib import Path
from typing import Optional

from vaf.core.config import Config
from vaf.cli.ui import UI

# Check for dependencies
try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

try:
    import speech_recognition as sr
    HAS_STT = True
except ImportError:
    HAS_STT = False

class SpeechManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.config = Config.load()
        self.tts_engine = None
        self.stt_recognizer = None
        self.stt_mic = None
        self._is_speaking = False
        self._piper_checked = False
        self._has_piper = False
        
        # Base paths
        self.base_dir = Path(__file__).parents[2]
        self.bin_dir = self.base_dir / "bin" / "piper"
        self.models_dir = self.base_dir / "models" / "voices"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)

        if self.is_stt_enabled() and HAS_STT:
            self.stt_recognizer = sr.Recognizer()
            self._init_mic()

    def _init_mic(self):
        """Initialize microphone based on config."""
        try:
            mic_index = self.config.get("speech_mic_index", None)
            # Ensure index is integer if set
            if mic_index is not None:
                mic_index = int(mic_index)
            self.stt_mic = sr.Microphone(device_index=mic_index)
        except Exception:
            self.stt_mic = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def list_microphones(self) -> list:
        """List available microphones (MME only on Windows for clean list)."""
        if not HAS_STT: return []
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
        return self.config.get("speech_tts_enabled", False)

    def is_stt_enabled(self) -> bool:
        return self.config.get("speech_stt_enabled", False)

    def _get_piper_binary(self) -> Optional[Path]:
        """Get path to piper binary depending on OS."""
        system = platform.system().lower()
        if system == "windows":
            return self.bin_dir / "piper.exe"
        else:
            return self.bin_dir / "piper"

    def _check_piper(self) -> bool:
        """Check if Piper is installed, try to install if missing."""
        if self._piper_checked:
            return self._has_piper
        
        binary = self._get_piper_binary()
        if binary.exists():
            self._has_piper = True
            self._piper_checked = True
            return True
            
        # Attempt Auto-Install
        UI.event("Speech", "Installing Piper TTS (Offline Neural Voice)...", style="warning")
        try:
            self._install_piper()
            self._has_piper = binary.exists()
        except Exception as e:
            UI.error(f"Piper install failed: {e}")
            self._has_piper = False
        
        self._piper_checked = True
        return self._has_piper

    def _install_piper(self):
        """Download and extract Piper binary from GitHub."""
        system = platform.system().lower()
        arch = platform.machine().lower()
        
        # Mapping to GitHub release assets (v1.2.0)
        # url pattern: https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip
        base_url = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2"
        
        asset = ""
        if system == "windows":
            if "64" in arch: asset = "piper_windows_amd64.zip"
            else: raise Exception("Windows 32-bit not supported by auto-installer")
        elif system == "linux":
            if "x86_64" in arch: asset = "piper_linux_x86_64.tar.gz"
            elif "aarch64" in arch: asset = "piper_linux_aarch64.tar.gz" # Pi 64
            elif "armv7" in arch: asset = "piper_linux_armv7l.tar.gz"   # Pi 32
            else: raise Exception(f"Linux arch {arch} not supported")
        elif system == "darwin":
            if "arm64" in arch: asset = "piper_macos_aarch64.tar.gz" # M1/M2
            else: asset = "piper_macos_x64.tar.gz" # Intel
        else:
            raise Exception(f"OS {system} not supported")

        url = f"{base_url}/{asset}"
        temp_zip = self.bin_dir / asset
        
        # Download
        UI.event("Download", f"Fetching {asset}...", style="dim")
        response = requests.get(url, stream=True)
        response.raise_for_status()
        with open(temp_zip, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        # Extract
        UI.event("System", "Extracting...", style="dim")
        if asset.endswith(".zip"):
            with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                zip_ref.extractall(self.bin_dir.parent) # Piper zip contains 'piper/' folder
        else:
            with tarfile.open(temp_zip, 'r:gz') as tar_ref:
                tar_ref.extractall(self.bin_dir.parent)
        
        # Cleanup
        try:
            os.remove(temp_zip) 
        except:
            pass
        
        # Set executable permissions on Linux/Mac
        if system != "windows":
            binary = self._get_piper_binary()
            if binary.exists():
                st = os.stat(binary)
                os.chmod(binary, st.st_mode | 0o111)
        
        UI.success("Piper TTS installed.")

    def _ensure_voice_model(self, lang: str) -> Optional[Path]:
        """Ensure the ONNX model for the language exists."""
        # Handle 'auto' -> default to English
        if lang == "auto":
            lang = "en"
            
        # Mapping lang -> model name
        # Using high/medium quality models where available
        models = {
            # Major Languages
            "en": "en_US-lessac-high",
            "de": "de_DE-thorsten-high",
            "fr": "fr_FR-siwis-medium",
            "es": "es_ES-sharvard-medium",
            "zh": "zh_CN-huayan-medium",
            "it": "it_IT-paola-medium",
            "pt": "pt_PT-tugao-medium",
            "ru": "ru_RU-ruslan-medium",
            
            # Nordic
            "da": "da_DK-talesyntese-medium",
            "fi": "fi_FI-harri-medium",
            "is": "is_IS-bui-medium",
            "no": "no_NO-talesyntese-medium",
            "sv": "sv_SE-talesyntese-medium",
            
            # Eastern Europe / Slavic
            "cs": "cs_CZ-jirka-medium",
            "hu": "hu_HU-anna-medium",
            "pl": "pl_PL-darkman-medium",
            "ro": "ro_RO-mihai-medium",
            "sk": "sk_SK-lili-medium",
            "sl": "sl_SI-artur-medium",
            "sr": "sr_RS-serbski_institut-medium",
            "uk": "uk_UA-ukrainian_tts-medium",
            "bg": "bg_BG-ls-medium",
            
            # Middle East / Asia
            "ar": "ar_JO-kareem-low",
            "fa": "fa_IR-amir-medium",
            "tr": "tr_TR-dfki-medium",
            "ka": "ka_GE-natia-medium",
            "kk": "kk_KZ-iseke-medium",
            "ne": "ne_NP-google-medium",
            "vi": "vi_VN-vivos-x_low",
            
            # Others
            "ca": "ca_ES-upc_ona-medium",
            "cy": "cy_GB-gwryw_gogleddol-medium",
            "el": "el_GR-rappcha-medium",
            "lb": "lb_LU-mary-medium",
            "nl": "nl_NL-rdh-medium",
            "sw": "sw_CD-lanafrica-medium",
        }
        
        # Default to English if lang not supported
        short_lang = lang[:2].lower()
        model_name = models.get(short_lang, models["en"])
        
        onnx_file = self.models_dir / f"{model_name}.onnx"
        json_file = self.models_dir / f"{model_name}.onnx.json"
        
        if onnx_file.exists() and json_file.exists():
            return onnx_file
            
        # Download
        UI.event("Download", f"Downloading voice: {model_name}...", style="warning")
        try:
            # Hugging Face URLs for Piper Voices (rhasspy/piper-voices)
            # Structure: lang_code/locale/voice_name/quality/file
            # Example: de/de_DE/thorsten/high/de_DE-thorsten-high.onnx
            
            parts = model_name.split('-')
            if len(parts) >= 3:
                locale = parts[0]      # de_DE
                voice_name = parts[1]  # thorsten
                quality = parts[2]     # high
                
                base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{short_lang}/{locale}/{voice_name}/{quality}/{model_name}.onnx"
            else:
                # Fallback for simpler names
                base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{short_lang}/{model_name}/{model_name}.onnx"
            
            # Download ONNX
            r = requests.get(base_url, stream=True)
            if r.status_code == 404:
                UI.error(f"Voice not found at {base_url}")
                return None
            r.raise_for_status()
            with open(onnx_file, 'wb') as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            
            # Download JSON
            r = requests.get(base_url + ".json")
            r.raise_for_status()
            with open(json_file, 'wb') as f:
                f.write(r.content)
                
            UI.success(f"Voice {model_name} ready.")
            return onnx_file
        except Exception as e:
            UI.error(f"Failed to download voice: {e}")
            return None

    def _play_audio(self, file_path: str):
        """Play WAV/MP3 file."""
        system = platform.system().lower()
        try:
            if system == "windows":
                # PowerShell Player for WAV (Piper outputs WAV)
                cmd = f"(New-Object Media.SoundPlayer '{file_path}').PlaySync()"
                subprocess.run(['powershell', '-c', cmd], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            elif system == "darwin":
                subprocess.run(['afplay', file_path], check=True)
            else:
                subprocess.run(['aplay', file_path], check=True)
        except:
            pass

    def stop(self):
        """Stop speaking immediately."""
        self._is_speaking = False
        # Kill player process if running (Windows PowerShell specific)
        if platform.system().lower() == "windows":
            try:
                subprocess.run(['taskkill', '/F', '/IM', 'powershell.exe'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                pass
        # pyttsx3 stop
        if self.tts_engine:
            try:
                self.tts_engine.stop()
            except:
                pass

    def speak(self, text: str, lang: str = "auto"):
        """Speak text using Piper (High Quality Offline) or pyttsx3 (Fallback)."""
        if not self.is_tts_enabled(): return

        clean_text = self._clean_markdown(text)
        if not clean_text.strip(): return

        # DEBUG: Show what is actually being sent to TTS
        # UI.event("TTS Debug", f"Speaking ({len(clean_text)} chars): {clean_text[:100]}...", style="dim")

        def _speak_worker():
            # 1. Try Piper (High Quality)
            if self._check_piper():
                # Attempt to get voice for requested language
                model_path = self._ensure_voice_model(lang)
                
                # Fallback to English if specific language fails
                if not model_path and lang != "en":
                    UI.warning(f"Voice for '{lang}' not available/failed. Falling back to English.")
                    model_path = self._ensure_voice_model("en")

                if model_path:
                    try:
                        import tempfile
                        fd, wav_path = tempfile.mkstemp(suffix=".wav")
                        os.close(fd)
                        
                        binary = self._get_piper_binary()
                        
                        # Run Piper
                        proc = subprocess.Popen(
                            [str(binary), "--model", str(model_path), "--output_file", wav_path],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL
                        )
                        proc.communicate(input=clean_text.encode('utf-8'))
                        
                        if proc.returncode == 0 and os.path.exists(wav_path):
                            self._play_audio(wav_path)
                            try:
                                os.remove(wav_path) 
                            except:
                                pass
                            return # Success!
                    except Exception as e:
                        pass

            # 2. Fallback: pyttsx3 (Robotic but reliable)
            if HAS_TTS:
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                except ImportError: pass

                with self._lock:
                    self._is_speaking = True
                    try:
                        engine = pyttsx3.init()
                        engine.setProperty('rate', 160)
                        # Switch language logic for SAPI5
                        if lang != "auto":
                            voices = engine.getProperty('voices')
                            target = None
                            search = ["german", "de_DE"] if lang.startswith("de") else ["english", "en_US"]
                            for v in voices:
                                if any(s in v.name.lower() or s in str(v.id).lower() for s in search):
                                    target = v.id
                                    break
                            if target: engine.setProperty('voice', target)
                        
                        engine.say(clean_text)
                        engine.runAndWait()
                    except: pass
                    finally:
                        self._is_speaking = False
                        try:
                            pythoncom.CoUninitialize()
                        except:
                            pass

        threading.Thread(target=_speak_worker, daemon=True).start()

    def listen(self, prompt: str = "Listening...", timeout: int = 10, lang: str = None) -> Optional[str]:
        """
        Listens to microphone (STT).
        If lang is None, uses config 'speech_language' or defaults to 'en-US'.
        """
        if not HAS_STT: return None
        if not self.stt_mic: return None

        # Determine language
        if not lang:
            lang = self.config.get("speech_language", "en-US")

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
            
            def _calculate_rms(data):
                """Calculate RMS amplitude for 16-bit PCM data."""
                count = len(data) // 2
                if count == 0: return 0
                shorts = struct.unpack(f"{count}h", data)
                sum_squares = sum(s**2 for s in shorts)
                return int(math.sqrt(sum_squares / count))

            with self.stt_mic as source:
                # 1. Calibration
                UI.print(f"[dim]Calibrating noise...[/dim]", end="\r")
                self.stt_recognizer.adjust_for_ambient_noise(source, duration=1.0)
                threshold = self.stt_recognizer.energy_threshold
                
                # 2. Recording Loop with Visuals
                sys.stdout.write(f"\r\033[K● Recording ({locale})   ")
                sys.stdout.flush()
                
                frames = []
                start_time = time.time()
                silence_start = None
                has_spoken = False
                
                while True:
                    # Check timeout (no speech start)
                    if not has_spoken and (time.time() - start_time > timeout):
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
                    status = "● Recording"
                    if energy > threshold:
                        status = "● SPEAKING "
                        has_spoken = True
                        silence_start = None
                    else:
                        if has_spoken:
                            if silence_start is None:
                                silence_start = time.time()
                            elif time.time() - silence_start > 1.5: # 1.5s silence = End
                                break
                    
                    # Direct stdout for smooth animation without newline spam
                    # \033[K clears the line from cursor to end
                    sys.stdout.write(f"\r\033[K[bold red]{status}[/bold red] [{bar:<20}] ({int(energy)}/{int(threshold)}) ")
                    sys.stdout.flush()
                
                sys.stdout.write("\r\033[K") # Clear recording line
                UI.print("[dim]Processing...[/dim]")
                
                # Convert frames to AudioData
                frame_data = b"".join(frames)
                audio = sr.AudioData(frame_data, source.SAMPLE_RATE, source.SAMPLE_WIDTH)
                
                try:
                    text = self.stt_recognizer.recognize_google(audio, language=locale)
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
        t = text
        
        # 1. Remove XML-style thinking blocks
        t = re.sub(r'<think>.*?</think>', '', t, flags=re.DOTALL)
        t = re.sub(r'<redacted_reasoning>.*?</redacted_reasoning>', '', t, flags=re.DOTALL)
        
        # 2. Remove VQ-1 specific thinking patterns
        thought_patterns = [
            r'^Okay, the user.*?(?:\n\n|\n|\Z)',
            r'^First, I should.*?(?:\n\n|\n|\Z)',
            r'^Let me check.*?(?:\n\n|\n|\Z)',
            r'^I need to.*?(?:\n\n|\n|\Z)',
            r'^I will.*?(?:\n\n|\n|\Z)',
            r'^The user wants.*?(?:\n\n|\n|\Z)',
        ]
        for pattern in thought_patterns:
            t = re.sub(pattern, '', t, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE)

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

        # 4. Remove Code Blocks (replace with brief pause hint)
        t = re.sub(r'```.*?```', '.', t, flags=re.DOTALL)
        t = re.sub(r'`.*?`', 'code', t)
        
        # 4. Remove Emojis (Disabled for now - caused text loss)
        # t = re.sub(r'[\U00010000-\U0010ffff]', '', t)
        
        # 5. Clean Markdown & Structure for Natural Reading
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
        
        return t.strip()

def get_speech_manager() -> SpeechManager:
    return SpeechManager.get_instance()