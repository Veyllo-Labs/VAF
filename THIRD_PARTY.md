# Third-Party Dependencies and Licenses

This document is a manually maintained inventory of VAF's direct third-party
dependencies and their licenses. It is **generated and maintained by hand and may
lag the lockfiles** (`requirements.lock`, `web/package-lock.json`). When in doubt,
the lockfiles and each package's own `LICENSE`/`METADATA` are authoritative.

VAF itself is licensed under **AGPL-3.0-or-later** (see `LICENSE` and `LICENSING.md`).
This inventory covers **direct** dependencies only; transitive dependencies are not
enumerated here. Licenses were taken from the installed `*.dist-info/METADATA`
(Python) and `node_modules/*/package.json` (Node) where available, or from the
package's well-known upstream license otherwise (marked accordingly). Entries that
could not be confirmed are marked `unverified`.

Scope of this file:
- Python runtime deps from `setup.py` `BASE_REQUIRES` and the named `EXTRAS`.
- Python deps from `requirements.txt` (the canonical full installer list).
- Web UI deps from `web/package.json`.
- WhatsApp bridge deps from `vaf/whatsapp_node/package.json`.
- Runtime vs build/dev tooling are listed separately.

Versions shown are the minimum pins from the source files, not the resolved
lockfile versions.

---

## Known license concerns

These are flagged here for visibility. This document is **not a legal opinion**;
it is engineering documentation to support an informed review.

1. **`html2text` (GPL-3.0) replaced by `markdownify` (MIT) — RESOLVED.** The base
   library previously depended on the GPL-3.0 `html2text` for HTML-to-Markdown
   conversion (used by `webfetch` and the document editor). GPL-3.0 is strong
   copyleft and is incompatible with closed-source/commercial redistribution. It
   has been replaced with `markdownify` (MIT) in `setup.py`, `requirements.txt`,
   and the code, so the base install no longer ships any strong-copyleft runtime
   dependency.

2. **`requirements.lock` regenerated to pin PySide6 (LGPL-3.0), not PyQt6 (GPL-3.0)
   — RESOLVED.** Both `setup.py` (`desktop` extra) and `requirements.txt`
   intentionally specify **PySide6** (LGPL-3.0, weak copyleft) for the Linux Qt
   WebEngine window. The lockfile had drifted and still pinned `pyqt6*` (GPL-3.0);
   it has been regenerated from the current `requirements.txt`, so it now pins
   `pyside6` (+ `shiboken6`) and contains no `pyqt6*`.

---

## Python — runtime dependencies (base library)

Source: `setup.py` `BASE_REQUIRES` (these are installed by `pip install vaf`).
Also present in `requirements.txt`.

| Package | Min version | License |
|---|---|---|
| typer | >=0.9.0 | MIT |
| rich | >=13.0.0 | MIT |
| prompt_toolkit | >=3.0.0 | BSD-3-Clause |
| colorama | >=0.4.0 | BSD-3-Clause |
| shellingham | >=1.5.0 | ISC |
| psutil | >=5.9.0 | BSD-3-Clause |
| requests | >=2.31.0 | Apache-2.0 |
| httpx | >=0.27.0 | BSD-3-Clause |
| PyGithub | >=2.1.1 | LGPL-3.0 |
| beautifulsoup4 | >=4.12.0 | MIT |
| markdownify | >=1.0.0 | MIT |
| huggingface_hub[hf_xet] | >=0.20.0 | Apache-2.0 |
| tqdm | >=4.65.0 | MPL-2.0 AND MIT |
| openai | >=1.12.0 | Apache-2.0 |
| anthropic | >=0.18.0 | MIT |
| google-genai | >=1.0.0 | Apache-2.0 |
| schedule | >=1.2.0 | MIT |
| inquirer | >=3.1.0 | MIT |
| ruff | >=0.1.0 | MIT |
| argon2-cffi | >=23.1.0 | MIT |
| keyring | >=24.0.0 | MIT |
| filelock | >=3.12.0 | MIT (Unlicense / public-domain dedication upstream) |
| PyJWT | >=2.8.0 | MIT |
| pyotp | >=2.9.0 | MIT |
| qrcode[pil] | >=7.4.0 | BSD |
| cryptography | >=41.0.0 | Apache-2.0 OR BSD-3-Clause |

## Python — runtime dependencies (optional extras)

Source: `setup.py` `EXTRAS` and `requirements.txt`. Installed via
`pip install vaf[<extra>]`, `pip install vaf[all]`, or the full
`requirements.txt` installer flow.

### server
| Package | Min version | License |
|---|---|---|
| fastapi | >=0.109.0 | MIT |
| uvicorn[standard] | >=0.27.0 | BSD-3-Clause |
| websockets | >=12.0 | BSD-3-Clause |
| pydantic | >=2.0.0 | MIT |

### discord
| Package | Min version | License |
|---|---|---|
| discord.py | >=2.3.2 | MIT |

### telegram
| Package | Min version | License |
|---|---|---|
| python-telegram-bot | >=21.0 | LGPL-3.0-only |

### desktop
| Package | Min version | License |
|---|---|---|
| pystray | >=0.19.5 | LGPL-3.0 |
| pillow | >=10.0.0 | MIT-CMU (HPND) |
| pywebview | >=4.3.0 | BSD-3-Clause |
| pyobjc-framework-Cocoa (darwin) | >=9.0 | MIT (well-known; not installed in this venv) |
| **PySide6 (linux)** | >=6.7.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only (used under **LGPL-3.0**) |
| qtpy (linux) | >=2.0.0 | MIT (well-known; not installed in this venv) |

Note: the `desktop` extra intentionally uses **PySide6 (LGPL-3.0)**, not PyQt6
(GPL-3.0); `requirements.lock` matches this (no `pyqt6*`).

### memory
| Package | Min version | License |
|---|---|---|
| sqlalchemy[asyncio] | >=2.0.0 | MIT |
| asyncpg | >=0.29.0 | Apache-2.0 |
| pgvector | >=0.2.0 | MIT |
| sentence-transformers | >=2.2.0 | Apache-2.0 |
| onnxruntime | >=1.16.0 | MIT |
| tokenizers | >=0.15.0 | Apache-2.0 |
| numpy | >=1.24.0 | BSD-3-Clause (AND 0BSD, MIT, Zlib, CC0-1.0 for bundled components) |
| redis | >=5.0.0 | MIT |

### speech
| Package | Min version | License |
|---|---|---|
| SpeechRecognition | >=3.10.0 | BSD-3-Clause |
| pyaudio | >=0.2.14 | MIT |

### browser (python_version >= 3.11)
| Package | Min version | License |
|---|---|---|
| browser-use | >=0.12.9 | MIT |
| playwright | >=1.49.0 | Apache-2.0 |

### pdf
| Package | Min version | License |
|---|---|---|
| PyPDF2 | >=3.0.0 | BSD-3-Clause |
| pdfplumber | >=0.11.0 | MIT |
| pycryptodome | >=3.15.0 | BSD-2-Clause AND Public Domain |
| pdf2image | >=1.16.0 | MIT |
| pytesseract | >=0.3.10 | Apache-2.0 |

### docs
| Package | Min version | License |
|---|---|---|
| python-docx | >=1.1.0 | MIT |
| openpyxl | >=3.1.0 | MIT |
| python-pptx | >=0.6.21 | MIT |

## Python — build / dev / test tooling

Source: `setup.py` `dev` extra and `requirements.txt` testing section.
Not required at runtime.

| Package | Min version | License |
|---|---|---|
| pytest | >=7.0.0 | MIT |
| pytest-mock | >=3.10.0 | MIT |

Note: `ruff` (MIT) is listed under runtime above because VAF invokes it from the
built-in linter tool at runtime, not only as dev tooling.

---

## Web UI (Next.js) — `web/package.json`

VAF Web UI is itself licensed AGPL-3.0-or-later (per `web/package.json`).

### Runtime dependencies
| Package | Range | License |
|---|---|---|
| @monaco-editor/react | ^4.7.0 | MIT |
| chart.js | ^4.5.1 | MIT |
| clsx | ^2.1.0 | MIT |
| framer-motion | ^10.18.0 | MIT |
| html2pdf.js | ^0.14.0 | MIT |
| lucide-react | ^0.300.0 | ISC |
| mammoth | ^1.11.0 | BSD-2-Clause |
| marked | ^17.0.2 | MIT |
| monaco-editor | ^0.55.1 | MIT |
| next | ^16.1.6 | MIT |
| next-intl | ^4.8.3 | MIT |
| react | ^18 | MIT |
| react-chartjs-2 | ^5.3.1 | MIT |
| react-dom | ^18 | MIT |
| react-markdown | ^10.1.0 | MIT |
| react-pdf | ^10.3.0 | MIT |
| reactflow | ^11.11.4 | MIT |
| remark-gfm | ^4.0.1 | MIT |
| styled-jsx | ^5.1.7 | MIT |
| tailwind-merge | ^2.2.0 | MIT |
| zustand | ^5.0.10 | MIT |

### Build / dev tooling (devDependencies)
| Package | Range | License |
|---|---|---|
| @types/node | ^20 | MIT |
| @types/react | ^18 | MIT |
| @types/react-dom | ^18 | MIT |
| autoprefixer | ^10.0.1 | MIT |
| eslint | ^9.39.2 | MIT |
| eslint-config-next | ^16.1.6 | MIT |
| postcss | ^8 | MIT |
| tailwindcss | ^3.3.0 | MIT |
| typescript | ^5 | Apache-2.0 |
| cross-env | (used in `dev:insecure` script) | MIT (well-known; not in `package.json` deps) |

---

## WhatsApp bridge (Node) — `vaf/whatsapp_node/package.json`

Standalone Baileys-based bridge invoked over stdin/stdout JSON IPC.

### Runtime dependencies
| Package | Range | License |
|---|---|---|
| @whiskeysockets/baileys | ^6.7.21 | MIT (well-known; node_modules not installed) |

### Build / dev tooling (devDependencies)
| Package | Range | License |
|---|---|---|
| patch-package | ^8.0.1 | MIT (well-known; node_modules not installed) |

---

## Notes and caveats

- **Qt binding in `requirements.lock`:** the lockfile pins **PySide6 (LGPL-3.0)**
  and `shiboken6`, matching `setup.py`/`requirements.txt`; it contains no
  `pyqt6*` (GPL-3.0).
- **Dual/multi-licensed packages** (e.g. `cryptography`, `PySide6`, `numpy`,
  `pycryptodome`) are used under the most permissive applicable option; the table
  records the upstream-declared expression.
- **System tools** required at runtime but installed outside Python/Node (Git,
  Poppler, Tesseract, Docker, and the model/runtime stack pulled by
  Hugging Face) are out of scope for this dependency inventory; see
  `requirements.txt` and the setup docs for those.
- Packages marked "well-known; not installed in this venv" had their license
  taken from the documented upstream license rather than a local `METADATA`
  file, because they were not present in the inspected environment (platform- or
  install-specific).
