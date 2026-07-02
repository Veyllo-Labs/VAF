# Desktop Startup Splash (Loading Screen)

When the VAF desktop app launches, the native window now opens on a small,
self-contained **splash / loading screen** — a sleeping agent on a screen that
powers up — and only switches to the real Web UI once the frontend is actually
serving. This replaces the old behaviour where the window briefly showed
whatever happened to be on `http://127.0.0.1:3000`.

## The problem it fixes (the `:3000` flash edge case)

The desktop window (`vaf/core/desktop_window.py`) is created on the **main
thread** during startup, while the Next.js frontend boots in a **background
thread** (`vaf/tray.py` → `start_frontend_bg`). Previously the window was
created pointing at a hardcoded startup URL:

```python
_default_port = Config.get("local_network_port_frontend", 3000)  # 3000
_startup_url  = f"http://127.0.0.1:{_default_port}"
_dw.init(_startup_url, title="VAF")
```

Two things then go wrong at the moment the window opens:

1. **The frontend usually isn't listening yet** — it takes a moment (or, on a
   first run, minutes: `npm install`, `next build`, model download) before
   `:3000` answers.
2. **The frontend may not even be on `:3000`.** `FrontendManager` picks the
   first *free* port starting at 3000. If another process already holds 3000,
   VAF's frontend lands on 3001 (etc.), but the window still opened on
   `http://127.0.0.1:3000` — i.e. on **whatever other app owns that port**.

The frontend thread only calls `desktop_window.navigate(f"http://127.0.0.1:{port}")`
with the *resolved* port once it's ready — so until then the window showed the
hardcoded `:3000` page.

Concrete repro: with the `veyllo-web` marketing-site dev server running on
`:3000`, launching VAF made the window flash the **veyllo.app landing page**
before snapping to the real VAF frontend on `:3001`. Nothing in the code opens
the veyllo website on purpose — it was purely "load `localhost:3000`, and
something else was sitting there."

## Why a splash (UX rationale)

- **Startup is not instant**, especially first run (dependency install +
  production build + optional model download). A blank window, a white flash, or
  a stray foreign page all read as "the app is broken."
- A branded loading screen communicates **"VAF is starting"** and keeps the
  first impression on-brand (the agent, not a random page).
- It **removes the dependency on `:3000` being VAF.** The window shows local,
  self-owned content until the real frontend URL is known, so no other process
  can leak into VAF's window during boot.
- The splash covers the **frontend** boot only. On a fresh machine the
  **database** may still be starting after the UI appears — the login page then
  shows "Starting the database..." and switches to the first-run setup wizard on
  its own once PostgreSQL is ready.

## How it works

1. On startup, `vaf/tray.py` reads `vaf/media/splash.html` and passes it to the
   window as an HTML string:
   ```python
   _splash_html = (Path(__file__).parent / "media" / "splash.html").read_text("utf-8")
   _dw.init(_startup_url, title="VAF", html=_splash_html)
   ```
2. `desktop_window.init()` gained an optional `html` parameter. When present it
   creates the window with pywebview's `html=` instead of a `url=`, so the
   splash renders **immediately, with no server**:
   ```python
   if html is not None:
       _window = _wv.create_window(title, html=html, **_create_kwargs)
   else:
       _window = _wv.create_window(title, url, **_create_kwargs)
   ```
3. When `start_frontend_bg` finishes, it calls
   `desktop_window.navigate("http://127.0.0.1:<resolved-port>")` and the window
   swaps from the splash to the live Web UI.

There is **no artificial minimum display time** — the splash is visible exactly
as long as boot takes. On a warm start that can be a fraction of a second; on a
first run it stays up for the whole build/download. (Adding a forced minimum was
considered and rejected: it would delay the app for no functional reason.)

## The animation

The splash reuses VAF's own agent identity (dark rounded square = body, white
dot = eye) in a **sleeping** pose, so the loading screen is "the agent taking a
nap while it wakes up":

- Screen **powers on**: black → light (`bgOn`), the agent blooms in (`stageIn`).
- The agent **sleeps**: gentle breathing (`bodyNap`) with the eye held shut as a
  thin slit (`eyeSleep`) — adapted from the canonical `nap` scene in
  `animations/agent_avatar/agent-all-animations.html`, except the eye does **not
  reopen** each cycle (the file's `eyeNap` blinks back to fully open; a loading
  screen should stay asleep).
- Rising **`z z z`** (`awayZzz`, three staggered sizes), taken 1:1 from the same
  nap scene.
- A `prefers-reduced-motion` fallback renders the lit screen + closed-eye agent
  statically, with no motion and no `z`'s.

The agent (body + eye) is built as **siblings** (not eye-nested-in-body) exactly
like the source scene, so the eye keeps its own transform origin and stays
centred instead of being dragged by the body's breathing transform.

## Files

| File | Change |
|---|---|
| `vaf/media/splash.html` | **new** — self-contained splash (inline CSS only, no external assets, no JS) |
| `vaf/core/desktop_window.py` | `init()` gains an optional `html` param; `create_window` uses `html=` when provided |
| `vaf/tray.py` | loads `splash.html` and passes it to `init(..., html=...)` on startup |

## Constraints / notes

- **The splash must stay fully self-contained.** pywebview's `html=` has no base
  URL, so relative asset references (`/logo.png`, external fonts, …) will not
  resolve. Keep everything inline: pure CSS / shape-based art, no `<img src>`,
  no external `<link>`/`<script>`.
- The hardcoded `http://127.0.0.1:3000` in the network-restart path
  (`vaf/tray.py`, `desktop_window.navigate(...)` after a TLS/LAN change) is a
  separate flow and is intentionally left as-is; it is not part of cold startup.
- The window's exposed native bridges (`save_file_as`, etc.) persist across the
  splash → frontend navigation, since they are bound to the window, not the page.

## Future ideas

- Rotating status text ("Waking the agent…", "Loading model…") if a longer,
  more informative boot screen is wanted — would require reading boot state into
  the splash (currently intentionally text-free per design).
- Reuse the same splash HTML on the network-restart navigation for consistency.
