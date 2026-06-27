# macOS Setup

VAF runs on macOS (Apple Silicon and Intel) as a desktop app, a headless server, or a
terminal interface. The automated installer ([install.sh](../../install.sh)) handles the
whole setup. This page covers the macOS specifics; for the cross-platform overview see the
main [README](../../README.md).

## Prerequisites

The installer provisions what it can, so a bare machine works:

- **macOS** on Apple Silicon (M1/M2/M3/…) or Intel.
- **Homebrew** — detected; the installer offers to install it if missing.
- **Python** — not required up front: if no suitable Python is found, the installer installs
  [uv](https://docs.astral.sh/uv/) and provisions Python itself. A system `python@3.12` is used if present.
- **Node.js** — not required up front: if missing, the installer downloads a portable Node into
  `~/.vaf/node` for the web UI. A system `brew install node` is used if present.
- **Docker** *(optional)* — only for the memory/RAG system and the code sandbox. The installer
  **detects** an existing runtime (Docker Desktop, Engine, Colima, Podman) and uses it; it does not
  install one. To add it without Docker Desktop: `brew install colima docker && colima start`.
  See [DOCKER_SERVICES.md](DOCKER_SERVICES.md).

## Install

```bash
git clone https://github.com/Veyllo-Labs/VAF.git && cd VAF
chmod +x install.sh && ./install.sh
```

The installer asks once whether to set up **Desktop** (personal, local, system tray) or
**Server** (always-on, LAN over HTTPS) mode, then:

- installs the system dependencies via Homebrew: `portaudio`, `git`, `ffmpeg`;
- creates a Python virtual environment (via uv when used) and installs the Python dependencies;
- installs the web UI dependencies (the production build runs on first launch);
- detects an existing Docker runtime and, if present, starts the memory stack;
- adds the `vaf` command to your shell.

## GPU acceleration (Metal)

GPU handling is automatic and depends on your chip:

| Mac | Acceleration |
|-----|--------------|
| Apple Silicon (`arm64`) | **Metal** GPU acceleration — detected and enabled automatically. |
| Intel | CPU mode (Metal is not used). |

No manual driver setup is required on macOS. For cloud providers (OpenAI, Anthropic, …)
the local GPU is irrelevant — see [LLM_BACKEND_FACTS.md](../llm/LLM_BACKEND_FACTS.md).

## Running

```bash
vaf start      # start in the chosen mode (Desktop tray or Server)
vaf status     # show what's running
vaf stop       # stop
vaf tray       # run the desktop tray in the foreground
vaf run        # interactive terminal session
```

First launch opens the setup wizard in your browser — see [FIRST_RUN.md](FIRST_RUN.md).

## Troubleshooting

- **"Homebrew is required"** — the installer can install Homebrew for you; or install it
  manually from <https://brew.sh> and re-run `./install.sh`.
- **Python too old / missing** — the installer provisions Python via uv automatically; or run
  `brew install python@3.12` and re-run.
- **Docker features unavailable** — start your Docker runtime so the daemon is reachable
  (Docker Desktop, or `colima start`); the installer and `vaf` print a reminder when Docker is
  installed but not running.
- **Desktop window doesn't open** — make sure you ran the installer in Desktop mode; the
  tray/window uses native macOS APIs (no extra GTK packages are needed as on Linux).

For services, networking, and integrations see the [documentation index](../README.md).
