# Installation profiles: local models, or none

VAF can run its models on the machine it is installed on, or leave all inference to the
API providers you set in Settings. Which of the two an installation does is its
**inference profile** (`inference_profile`, `vaf/core/inference_profile.py`):

| | Full (`local`, the default) | Lean (`api`) |
|---|---|---|
| Chat model | local model, downloaded on first use (2.7 to 17.9 GB, chosen by GPU memory) | none; the chat provider set under Settings > AI & Model |
| Speech-to-text | the Whisper container (`vaf-stt`, image 7.5 GB), or a provider | the provider in `speech_stt_provider` only |
| Text-to-speech | the Piper container (`vaf-tts`, image 1.1 GB), or a provider | the provider in `speech_tts_provider`, or a TTS server you run elsewhere (`chatterbox`), or the macOS voice |
| Voice-call model | local model on request (5.1 GB) | the call runs on an API |
| Database, cache, document conversion | yes | yes |
| Browser (agent tool) | yes | yes |
| Sandbox environments | yes | yes |
| Memory embedding model | yes (130 MB, CPU) | yes (130 MB, CPU) |
| Speaker recognition | yes (27 MB, CPU) | yes (27 MB, CPU) |
| Compose memory limits | about 8.3 GB, plus the chat model in RAM or VRAM | about 4.3 GB |

The sizes are measured on the machine this was written on (images with `docker image
ls`, models on disk); the memory limits are the ones in `docker-compose.memory.yml`.
Limits are ceilings, not what the services use.

The lean profile is meant for a small rented server (a VPS) that should carry VAF without
a GPU and without 15 GB of models: see
[SERVER_MODE.md](SERVER_MODE.md#on-a-rented-server-vps). It works on a desktop too.

## Choosing it

At installation:

```bash
./install.sh                        # asks: [1] Full (local)  [2] Lean (API)
./install.sh --profile api          # without asking
./install.sh --server --profile api # a server install without local models
```

```powershell
.\install.ps1 -InferenceProfile api
# through the one-line bootstrap, which forwards no parameters:
$env:VAF_INFERENCE_PROFILE = "api"; irm https://raw.githubusercontent.com/Veyllo-Labs/VAF/main/packaging/install/bootstrap.ps1 | iex
```

A re-run of `install.sh` offers the profile that is set as its default, and a run
without a flag and without a terminal keeps what is set, so an upgrade never switches a
lean server back to downloading models. On a server, `vaf server provision --profile api`
sets it too.

Afterwards:

```bash
vaf profile                # the profile, the services it runs, what contradicts it
vaf profile api            # switch: the speech containers stop
vaf profile api --prune    # ... and their containers, images and volumes are removed
vaf profile local          # switch back: the speech containers start again
```

`--prune` frees the disk the speech containers took (their images and the models in their
volumes). It does not delete chat models you downloaded earlier: `vaf profile` says how
much is left in the `models` folder, and deleting it is yours to do. The command is not
behind the terminal door, like `vaf server`: what it changes is the machine's, and an
account that could run it from the agent's shell has `host_bash`, which reaches the same
config and the same docker directly.

In the web app the profile sits at the top of **Settings > AI & Model** (admin only), with
the same switch (`POST /api/system/inference-profile`, the same `inference_profile.apply`
behind it). Under Lean the local choices there and under **Voice & Speech** stay listed but
cannot be picked, and the first-run wizard says that skipping the provider step leaves no
local model to fall back on.

The profile is admin-only (`GLOBAL_CONFIG_KEYS`): it starts and stops containers and
decides about downloads for every account at once.

## What the lean profile guarantees, and where

Nothing that infers is downloaded or started, whatever a setting says. The profile is
decided at the places that exist once each, not by checks spread over the code:

- `backend.ensure_model_available`, the one download entry for a model, returns nothing
  and downloads nothing. Every lane passes it: chat, the voice call, thinking runs,
  sub-agents, the local failover link. The two lanes that download a model chosen by hand
  (the model download in the terminal settings and in the web settings) refuse as well.
- `ServerManager.start_server`, the one start of the local server, refuses, also for the
  voice lane, which otherwise asks past the provider gate.
- `Agent.load_model` loads no local model; a chat turn that has no provider says so in the
  chat instead of promising a download.
- `service_stack.needed_services` leaves the speech containers out. The stack start, the
  status, the repair, `vaf top` and the installers read this one list: a container the
  profile leaves out is shown as `off`, never as a fault, and a repair does not bring it
  back.
- Speech-to-text and text-to-speech use the provider's answer or none: without local
  models there is no container to fall back to, so a provider error is reported rather
  than retried against a service that does not run.

## A setting that contradicts the profile

Nothing is refused when it is written: `config.json` stays editable by hand, and the
places above are the guarantee. A setting that names a local lane on a lean installation
(the chat provider still `local`, a voice, thinking, sub-agent or vision lane on `local`,
the local failover link, speech switched on without a provider) is named by `vaf profile`
and `vaf doctor` (`inference_profile_conflict`), with what to do.

A fresh lean installation starts in exactly that state: no chat provider yet. Set one
under Settings > AI & Model, or give the Veyllo key in the first-run wizard.

## Named boundaries

- **One profile, not one switch per lane.** A machine that should run its own
  speech-to-text but chat through an API is a `local` installation with an API chat
  provider; the lean profile is for machines that run no model at all.
- **No browser-less profile yet.** The browser is an agent tool, not a model, and stays in
  both profiles. Whether a smaller server needs to go without it (about 2 GB of memory
  limits) is decided when a pilot measures the real use.
- **Not on the framework facade.** An embedder chooses the provider per agent; the profile
  is a rule about what an installation of the product carries.
