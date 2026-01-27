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
            # macOS: Robust architecture detection
            # platform.machine() can return x86_64 even on M1/M2 if Python runs under Rosetta 2
            # Use sysctl to get the REAL hardware architecture
            is_arm = False
            try:
                import subprocess
                result = subprocess.run(['sysctl', '-n', 'hw.optional.arm64'], 
                                      capture_output=True, text=True, timeout=2)
                is_arm = result.returncode == 0 and result.stdout.strip() == '1'
            except:
                # Fallback: Check platform.machine()
                is_arm = "arm64" in arch
            
            if is_arm:
                asset = "piper_macos_aarch64.tar.gz"  # M1/M2/M3
                UI.event("System", "Detected Apple Silicon (ARM64) - downloading ARM binary", style="dim")
            else:
                asset = "piper_macos_x64.tar.gz"  # Intel
                UI.event("System", "Detected Intel Mac (x86_64) - downloading x64 binary", style="dim")
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
                    # Try mpg123 first, then mpv, then ffplay
                    players = ['mpg123', 'mpv', 'ffplay']
                    for player in players:
                        try:
                            if player == 'mpv':
                                cmd = [player, '--no-video', '--really-quiet', file_path]
                            elif player == 'ffplay':
                                cmd = [player, '-nodisp', '-autoexit', '-loglevel', 'quiet', file_path]
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
            sound_path = self.base_dir / "sounds" / "sst.mp3"
            
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
            sound_path = self.base_dir / "sounds" / "tts01.mp3"
            
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
                preferred_engine = self.config.get("speech_tts_engine", "piper")
                
                # 1. Try Piper (High Quality) - only if user prefers it
                if preferred_engine == "piper" and self._check_piper():
                    # Attempt to get voice for requested language
                    model_path = self._ensure_voice_model(lang)
                    
                    # Do not fallback to English here. 
                    # If the requested language model is missing, we prefer to fall through 
                    # to pyttsx3 (System TTS) which might have the correct language installed,
                    # rather than speaking German/French text with an English accent.

                    if model_path:
                        try:
                            import tempfile
                            fd, wav_path = tempfile.mkstemp(suffix=".wav")
                            os.close(fd)
                            
                            binary = self._get_piper_binary()
                            
                            # Run Piper
                            self._current_tts_process = subprocess.Popen(
                                [str(binary), "--model", str(model_path), "--output_file", wav_path],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL
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
                        except Exception:
                            # Piper error, silently fall through to system voice
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
                
                elif HAS_TTS:
                    try:
                        import pythoncom
                        pythoncom.CoInitialize()
                    except ImportError: pass

                    # Note: _lock is already held by _tts_lock, so we use _is_speaking flag instead
                    self._is_speaking = True
                    try:
                        engine = pyttsx3.init()
                        self.tts_engine = engine  # Store reference for stop()
                        engine.setProperty('rate', 160)
                        
                        # Voice selection - platform-specific logic
                        voices = engine.getProperty('voices')
                        target = None
                        
                        # macOS: Prefer high-quality Apple voices
                        if platform.system().lower() == "darwin":
                            # Best voices on macOS (in order of preference)
                            if lang.startswith("de"):
                                # German: Anna (compact, high quality) > others
                                preferred = ["anna", "compact.de", "german"]
                            else:
                                # English: Samantha (compact, high quality) > Siri > Alex
                                preferred = ["samantha", "compact.en-us", "siri", "alex", "english"]
                            
                            # Try to find preferred voice
                            for pref in preferred:
                                for v in voices:
                                    if pref in v.name.lower() or pref in str(v.id).lower():
                                        target = v.id
                                        break
                                if target:
                                    break
                        else:
                            # Windows/Linux: Use existing logic
                            search = ["german", "de_DE"] if lang.startswith("de") else ["english", "en_US"]
                            for v in voices:
                                if any(s in v.name.lower() or s in str(v.id).lower() for s in search):
                                    target = v.id
                                    break
                        
                        if target:
                            engine.setProperty('voice', target)
                        
                        if self.on_speech_start:
                            try: self.on_speech_start(clean_text)
                            except: pass

                        engine.say(clean_text)
                        engine.runAndWait()
                        
                        if self.on_speech_end:
                            try: self.on_speech_end()
                            except: pass

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
            
            # Show Language explicitly in UI
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
            # Loop to remove multiple paragraphs of thinking
            while re.search(pattern, t, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE):
                t = re.sub(pattern, '', t, count=1, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE).strip()

        # 2b. Aggressive Reasoning Filter: "Answer in [Lang]"
        # If the model explicitly tells itself to answer in a language, 
        # everything BEFORE that instruction is likely reasoning/garbage.
        answer_instruction = re.search(r'(?:Answer|Antworte|Respond) (?:in|auf) [A-Z][a-z]+(?: \([A-Z][a-z]+\))?[\.:]?\s*', t, flags=re.IGNORECASE)
        if answer_instruction:
            # Keep only what comes AFTER "Answer in German."
            t = t[answer_instruction.end():].strip()

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
        
        return t.strip()

def get_speech_manager() -> SpeechManager:
    return SpeechManager.get_instance()


class WakeWordManager:
    """
    Manages "Wake Word" detection using openWakeWord (100% local & free).
    Runs in a background thread and triggers a callback when the keyword is detected.

    Available wake words: hey_jarvis, alexa, hey_mycroft, hey_rhasspy
    """
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.config = Config.load()
        self.oww_model = None
        self.audio_stream = None
        self._is_listening = False
        self._thread = None
        self._stop_event = threading.Event()
        self._callback = None
        
        # Ensure models are downloaded (fixes NO_SUCHFILE error)
        if self.is_available():
            try:
                import openwakeword.utils
                # This ensures default models (hey_jarvis etc.) are in the package dir
                openwakeword.utils.download_models()
            except Exception:
                pass  # Ignore download errors (offline mode or already exists)

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_available(self) -> bool:
        """Check if openWakeWord dependencies are available."""
        try:
            import openwakeword
            import pyaudio
            return True
        except ImportError:
            return False

    def get_available_models(self) -> list:
        """Get list of available wake word models."""
        return [
            "hey_jarvis",
            "alexa",
            "hey_mycroft",
            "hey_rhasspy",
            "timer"  # Can also be used
        ]

    def start_listening(self, callback):
        """
        Start listening for the wake word in a background thread.

        Args:
            callback: Function to call when wake word is detected.
        """
        if self._is_listening:
            return

        if not self.is_available():
            UI.warning("Wake Word requires 'openwakeword' and 'pyaudio'. Install: pip install openwakeword pyaudio")
            return

        self._callback = callback
        self._stop_event.clear()
        self._is_listening = True

        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop_listening(self):
        """Stop the background listening thread."""
        if not self._is_listening:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

        self._is_listening = False
        self._cleanup()

    def is_listening(self) -> bool:
        return self._is_listening

    def _cleanup(self):
        """Release resources."""
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except: pass
            self.audio_stream = None

        self.oww_model = None

    def _listen_loop(self):
        """Background loop for wake word detection."""
        try:
            from openwakeword.model import Model
            import pyaudio
            import numpy as np
        except ImportError as e:
            UI.error(f"Missing dependency: {e}. Install: pip install openwakeword pyaudio")
            self._is_listening = False
            return

        wake_word = self.config.get("stt_wake_word", "hey_jarvis")

        # Validate wake word
        available_models = self.get_available_models()
        if wake_word not in available_models:
            UI.warning(f"Invalid wake word '{wake_word}'. Defaulting to 'hey_jarvis'.")
            wake_word = "hey_jarvis"

        try:
            # Initialize openWakeWord model (silent - no UI spam)
            self.oww_model = Model(wakeword_models=[wake_word], inference_framework="onnx")

            # Initialize PyAudio stream
            mic_index = self.config.get("speech_mic_index", None)
            audio = pyaudio.PyAudio()
            self.audio_stream = audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,  # openWakeWord uses 16kHz
                input=True,
                frames_per_buffer=1280,  # 80ms chunks
                input_device_index=mic_index
            )

            # Wake word ready - no message needed (shown in status bar)

            while not self._stop_event.is_set():
                # Read audio chunk
                audio_data = self.audio_stream.read(1280, exception_on_overflow=False)
                audio_array = np.frombuffer(audio_data, dtype=np.int16)

                # Process with openWakeWord
                prediction = self.oww_model.predict(audio_array)

                # Check if wake word detected (threshold: 0.5)
                if prediction[wake_word] > 0.5:
                    # Wake word detected - DEBUG: Log it
                    try:
                        log_path = os.path.join(os.path.expanduser("~"), "wake_word_debug.log")
                        with open(log_path, "a", encoding="utf-8") as f:
                            import datetime
                            f.write(f"{datetime.datetime.now()}: Wake word detected! Score: {prediction[wake_word]:.2f}\n")
                            f.write(f"  Callback exists: {self._callback is not None}\n")
                            f.flush()
                    except Exception as e:
                        # If logging fails, at least print to console
                        print(f"WAKE WORD DEBUG: Detected! Score: {prediction[wake_word]:.2f}, Callback: {self._callback is not None}, Error: {e}")

                    # Trigger callback (CRITICAL for STT start)
                    if self._callback:
                        try:
                            self._callback()
                            # DEBUG: Log callback success
                            try:
                                log_path = os.path.join(os.path.expanduser("~"), "wake_word_debug.log")
                                with open(log_path, "a", encoding="utf-8") as f:
                                    f.write(f"  Callback executed successfully!\n")
                                    f.flush()
                            except:
                                pass
                        except Exception as e:
                            # DEBUG: Log callback error
                            try:
                                log_path = os.path.join(os.path.expanduser("~"), "wake_word_debug.log")
                                with open(log_path, "a", encoding="utf-8") as f:
                                    f.write(f"  Callback ERROR: {e}\n")
                                    f.flush()
                            except:
                                pass
                            print(f"WAKE WORD DEBUG: Callback error: {e}")

                    # Reset model state to avoid immediate re-triggering
                    self.oww_model.reset()

                    # Pause briefly to avoid self-triggering
                    import time
                    time.sleep(1.0)

        except Exception as e:
            UI.error(f"Wake Word Error: {e}")
            self._is_listening = False
        finally:
            self._cleanup()