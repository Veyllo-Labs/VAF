# Installation Guide

A detailed, beginner-friendly walkthrough of installing VAF. Each operating system has its own complete section below — just follow the one for your computer. If you only want the short version, see [Installation in the README](../../README.md#installation).

**Jump to your operating system:** [Linux](#linux) · [macOS](#macos) · [Windows](#windows)

---

## Before you start

**You need:**
- A 64-bit computer running Linux, macOS, or Windows 10/11.
- An internet connection — the installer downloads a few GB.
- Roughly 10 GB of free disk space.
- `git`, a small tool used to download the code (each section below installs it first). If you would rather not use git, see the [ZIP alternative](#alternative-install-without-git).
- A phone with an authenticator app (Google Authenticator, Authy, ...) for the final two-factor step on first launch.

**You do NOT need to install these yourself** — the installer sets them all up:
- Python and Node.js are provisioned for you (user-scoped, no admin needed) — don't pre-install them.
- A container engine (Docker) is handled too. If you already have Docker installed, the installer uses it; otherwise it sets up a free engine for you. You *can* install Docker yourself if you prefer (it will be reused), but you don't have to — and the free engines avoid Docker Desktop's paid-licensing requirement for business/organizational use.

**Two rules that apply on every operating system:**
1. **Run the installer as your normal user** — not as administrator, and not with `sudo`. VAF installs into your own user account; running it elevated puts files in the wrong place and breaks the `vaf` command and Docker access. (The installer asks for a password or approval only for the few sub-steps that genuinely need it — that's expected; just approve those.)
2. **Keep the window open until it says it's finished, and don't let the computer go to sleep.** The installer downloads a lot; closing the window aborts it.

**Roughly how long it takes** (mostly downloading): Linux 5-20 min, macOS 5-15 min, Windows 15-40 min.

---

## Linux

Works on Ubuntu / Debian / Mint / Pop!_OS, Fedora / RHEL / Rocky / Alma, Arch / Manjaro, and openSUSE. You need a normal user account that can use `sudo` (the main user on most personal installs already can).

### 1. Install Git

Open a terminal and install git with your package manager:

```bash
sudo apt-get update && sudo apt-get install -y git   # Debian / Ubuntu / Mint / Pop!_OS
sudo dnf install -y git                               # Fedora / RHEL / Rocky / Alma
sudo pacman -S git                                    # Arch / Manjaro
sudo zypper install -y git                            # openSUSE
```

Check it worked:

```bash
git --version
```

You should see something like `git version 2.43.0`.

### 2. Pick a folder and download VAF

`git clone` creates a `VAF` folder inside the folder you are currently in, so move to a good location first. Use a folder inside your home directory that you own and that has **no spaces** in the path — for example `~/VAF` or `~/Projects/VAF`. Avoid system locations like `/opt` or `/usr`.

```bash
cd ~
git clone https://github.com/Veyllo-Labs/VAF.git
cd VAF
```

> The installer bakes this folder's path into the `vaf` command and the desktop launcher, so pick a stable location. If you move the folder later, run the installer again so the paths update.

### 3. Run the installer

```bash
chmod +x install.sh && ./install.sh
```

- **Do not use `sudo`.** The script calls `sudo` itself only for the parts that need it (system packages, installing/starting Docker, adding you to the `docker` group) and will prompt for your password — type it when asked.
- **Keep the terminal open** until you see the green `[OK] INSTALLATION COMPLETE!` banner. Expect about 5-20 minutes.

What it sets up for you: the distribution's Docker (with systemd + your user added to the `docker` group), Python (via `uv`), Node.js, the build/audio/desktop system packages, a Python virtual environment, the web UI, a `vaf` command in your shell, and an application-menu entry.

### 4. Choose Desktop or Server

Linux is the one platform where the installer asks, once:

```
[1] Desktop  — personal use, local only, system tray (default)
[2] Server   — always-on service, LAN accessible via HTTPS, starts at boot
```

Press **Enter** for **Desktop** (using VAF on this computer). Choose **Server** only for a home server / NAS / headless box that other devices should reach — see [SERVER_MODE.md](SERVER_MODE.md).

### 5. Start VAF

**Important, first time only:** the installer added you to the `docker` group, but that only takes effect after you **log out and back in once** (or run `newgrp docker`). Until then, Docker may need `sudo`.

Then start VAF — two ways:
- **Easiest:** launch **VAF** from your application menu.
- **From a terminal:** open a new terminal (so the `vaf` command is loaded), then run `vaf tray`.

(`vaf` is a shortcut the installer added to your shell; running `./run_vaf.sh` from inside the folder does the same thing.) VAF opens its own desktop window with the dashboard — if that window can't start, it falls back to your browser, and you can always open `http://localhost:3000` in any browser. Continue with [First-run setup](#first-run-setup-all-platforms).

### Common Linux problems

- **`git: command not found`** — install git (step 1), then retry the clone.
- **"permission denied ... Docker" right after install** — log out and back in once, or run `newgrp docker`, so the new `docker` group takes effect.
- **`./install.sh: Permission denied`** — make it executable first: `chmod +x install.sh && ./install.sh` (don't prefix with `sudo`).
- **`vaf: command not found`** — open a new terminal (the installer adds `vaf` to your shell's startup file, and a fresh terminal loads it). Or run `./run_vaf.sh` from inside the VAF folder.
- **Tray icon doesn't appear** — non-fatal (the window and web UI still work). Install the WebKitGTK typelib if you want it: `gir1.2-webkit2-4.1` on Ubuntu 24.04 (the `-4.0` variant on 22.04).
- **You ran `./install.sh` with `sudo` by mistake** — fix ownership with `sudo chown -R $USER:$USER ~/VAF ~/.vaf` and re-run `./install.sh` as your normal user.

---

## macOS

Works on Apple Silicon (M1/M2/M3 and newer) and Intel Macs. The macOS installer always sets up **Desktop** mode (it does not ask Desktop vs Server — the always-on Server profile is Linux-only).

### 1. Install Git

Open the **Terminal** app (press Cmd+Space, type `Terminal`, press Enter) and run:

```bash
git --version
```

If Git is not installed, macOS may offer to install the Xcode Command Line Tools (which include Git) — accept it and wait a few minutes. You can also run `brew install git` if you have Homebrew. No paid Apple Developer account is needed.

### 2. Pick a folder and download VAF

`git clone` creates a `VAF` folder inside the folder you are currently in. Terminal opens in your home folder by default, which is a good place (`~/VAF`). Avoid `/Applications`, iCloud-synced folders, and any path with spaces.

```bash
cd ~
git clone https://github.com/Veyllo-Labs/VAF.git
cd VAF
```

> The folder is self-contained, but if you move it after installing, run the installer again so the `vaf` command points to the new path.

### 3. Run the installer

```bash
chmod +x install.sh && ./install.sh
```

- **Do not use `sudo`.** The installer relies on Homebrew, which refuses to run as root, and it writes into your home folder — running it as root breaks the install. If macOS asks for your password during a Homebrew/Colima step, that's normal.
- If **Homebrew** is missing, the installer offers to install it — press **Y**. If it then can't find `brew`, open a **new** Terminal window and run `./install.sh` again (Homebrew needs a fresh shell on PATH).
- **Keep Terminal open** and the Mac awake until you see `INSTALLATION COMPLETE!`. Expect about 5-15 minutes.

What it sets up for you: Homebrew (if missing), Python (via `uv` or an existing one), Node.js, a free container engine (Colima — or your Docker Desktop if you already have it), system libraries (portaudio, ffmpeg), a Python virtual environment, the web UI, a `vaf` command, and a **VAF.app** in your personal `~/Applications` folder.

### 4. Start VAF

Start VAF — two ways:
- **Easiest:** open **VAF.app** (it's in your `~/Applications` folder — find it with Spotlight or Launchpad by typing "VAF").
- **From a terminal:** close and reopen Terminal (or run `source ~/.zshrc`) so the `vaf` command is available, then run `vaf tray`.

(`./run_vaf.sh` from inside the folder does the same thing.) The web UI is built on the **first** launch (on every Mac), so that first start takes a few extra minutes — this is normal. VAF then shows its dashboard (a desktop app window, or in your browser) at `http://localhost:3000` — continue with [First-run setup](#first-run-setup-all-platforms).

### Common macOS problems

- **`git: command not found`** (or a "command line developer tools" popup) — accept the install dialog, or run `xcode-select --install`, then retry the clone.
- **"Homebrew is required" and the installer exits** — press **Y** to install it; if `brew` still isn't found, open a new Terminal and re-run `./install.sh`.
- **You ran it with `sudo`** — don't; run `./install.sh` as your normal user (re-run without sudo to fix).
- **`vaf: command not found`** — run `source ~/.zshrc` or reopen Terminal; or run `./run_vaf.sh` from the folder.
- **"Container engine not ready" / Docker features unavailable** — Colima's VM may still be starting. Run `colima start` (or open Docker Desktop), then `vaf start` again. Check with `docker ps`.
- **Microphone / voice doesn't work in the desktop window** — a known limitation of the desktop window. Allow the app under System Settings -> Privacy & Security -> Microphone, or open `http://localhost:3000` in a normal browser.

---

## Windows

Works on Windows 10 and 11 (64-bit). The Windows installer always sets up **Desktop** mode (the always-on Server profile is Linux-only).

### 1. Install Git

Download Git for Windows from <https://git-scm.com/download/win> and run it — clicking **Next** through the wizard with the defaults is fine. (Or, if you have the Windows Package Manager, run `winget install Git.Git` in PowerShell.)

**Close and reopen** PowerShell afterwards, then check it worked:

```powershell
git --version
```

You should see something like `git version 2.x.x`. If you get "git is not recognized", reopen the terminal (or reboot) so it picks up git.

### 2. Pick a folder and download VAF

`git clone` creates a `VAF` folder inside the folder you are currently in. Use a **short, simple path with no spaces** — for example `C:\Users\<you>\VAF`. Avoid `C:\Program Files` (needs admin) and OneDrive- or Desktop-synced folders (syncing can lock files and corrupt the install).

```powershell
cd C:\Users\<you>
git clone https://github.com/Veyllo-Labs/VAF.git
cd VAF
```

> If you move the folder after installing, run the installer again so the launcher paths update.

### 3. Run the installer

The easiest way is to **double-click `install.bat`** in the VAF folder (in File Explorer). Or, in PowerShell from inside the folder:

```powershell
.\install.bat
```

- **Do not "Run as Administrator".** Run it as your normal user. If the Visual C++ runtime or Rancher Desktop still need installing, each shows a standard Windows UAC pop-up — just click **Yes**. (If your PC already has them, you won't see those prompts.) You never launch the whole installer elevated.
- If you try `.\install.ps1` directly and PowerShell says *"running scripts is disabled on this system"*, use `install.bat` instead (it handles that for you), or run `powershell -ExecutionPolicy Bypass -File .\install.ps1`.
- **Keep the window open** until it prints `INSTALLATION COMPLETE!`, and don't let the PC sleep. Expect about 15-40 minutes.

What it sets up for you: Python (via `uv`), a portable Node.js, the Visual C++ runtime, **Rancher Desktop** (a free container engine — an existing Docker is reused if you have one), the Python virtual environment, the web UI, and **VAF Agent** shortcuts on your Desktop and Start Menu.

### 4. Start VAF

Start VAF — two ways:
- **Easiest:** double-click the **VAF Agent** shortcut on your Desktop or in the Start Menu.
- **From a terminal:** open the VAF folder in PowerShell and run `.\run_vaf.bat tray`.

On macOS/Linux the terminal command is `vaf tray`; Windows doesn't add `vaf` to your PATH, so the equivalent is `.\run_vaf.bat tray` (use `.\run_vaf.bat start` to run it in the background instead). It works regardless of PowerShell's script policy. The web UI was already built during install, so the first launch is fast. VAF shows its dashboard (a desktop app window, or in your browser) at `http://localhost:3000` — continue with [First-run setup](#first-run-setup-all-platforms). The tray icon may be hidden under the **^** (up-arrow) near the clock.

### Common Windows problems

- **"running scripts is disabled on this system"** — use `install.bat` (and `run_vaf.bat` to start), or run `powershell -ExecutionPolicy Bypass -File .\install.ps1`. You don't need to change your machine's policy permanently.
- **`git` is not recognized** — install Git for Windows (step 1), then close and reopen the terminal and retry the clone.
- **The container engine never starts / "WSL2 is not enabled"** — open an **admin** PowerShell (right-click -> Run as administrator), run `wsl --install`, **reboot**, then run the normal (non-admin) installer again. This is the only step that needs admin and a reboot.
- **A UAC "Run as Administrator?" pop-up appears mid-install** — expected when the Visual C++ runtime or Rancher Desktop need installing; just click **Yes**.
- **`vaf` is not recognized** — use the **VAF Agent** shortcut or `run_vaf.bat <command>` (e.g. `run_vaf.bat tray`). The bare `vaf` command needs the venv active (`.\venv\Scripts\Activate.ps1`); if that activation is blocked by the script policy, stick with `run_vaf.bat`, which isn't affected.
- **Tray icon doesn't appear** — it's probably hidden; click the **^** up-arrow near the clock to reveal it.
- **Ports 3000 or 8001 already in use** — find the process with `netstat -ano | findstr :3000`, then `taskkill /F /PID <PID> /T`.

---

## First-run setup (all platforms)

The first time VAF launches, it shows a **setup wizard** (not a login screen) at `http://localhost:3000`. On a desktop install this appears in VAF's own app window; you can also open that address in any browser. The web UI is built on this first launch and the default model is downloaded, so the first start does some extra work.

The wizard walks you through:
1. **Language** — English or German (this only affects the UI).
2. **Admin account** — choose your username and password; this is the owner account.
3. **Soul** — a short questionnaire that defines the agent's personality. Every field is editable.
4. **Veyllo API key** (optional) — paste one to use the hosted Veyllo models, or skip it to use the local model. You can add providers later under Settings -> AI & Model.
5. **Two-factor authentication (required, and the last step)** — scan the QR code with your authenticator app and enter the 6-digit code. The earlier steps are saved only once 2FA succeeds.

After 2FA you land in the web UI, logged in, with your agent live. For a screen-by-screen walkthrough see [FIRST_RUN.md](FIRST_RUN.md).

> In **Server** mode (Linux) there is no window opened for you: open the LAN HTTPS URL shown by the installer or by `vaf status` (the certificate is self-signed, so your browser warns once — accept it).

---

## Everyday commands

Once installed, these are the commands you'll use day to day:

```
vaf start      # start in the background
vaf stop       # stop
vaf status     # show what is running
vaf tray       # desktop tray + dashboard window
vaf run        # interactive terminal session
vaf update     # update to the latest release
```

On Linux and macOS, `vaf` is added to your shell — open a new terminal if it isn't found yet. On Windows, use the **VAF Agent** shortcut or `run_vaf.bat <command>` (for example `run_vaf.bat status`); the bare `vaf` command needs the virtual environment active first. In Server mode these same commands wrap the background service.

---

## Alternative: install without git

If you cannot or do not want to install git:

1. Open the VAF repository on GitHub and click **Code -> Download ZIP**.
2. Extract it into a good folder (see the "Pick a folder" step in your OS section for where to put it and what to avoid).
3. Continue from the **Run the installer** step for your operating system.

`git clone` is still the recommended method, because `vaf update` (one-command upgrades) works best from a git checkout.
