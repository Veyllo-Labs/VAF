# Installation profiles: local models, or none

VAF can run its chat, voice and speech models on the machine it is installed on, or leave
them to the API providers you set in Settings. Which of the two an installation does is
its **inference profile** (`inference_profile`, `vaf/core/inference_profile.py`). Two
small models run on the CPU in both: the memory's embedding model and the speaker
recognition.

| | Full (`local`, the default) | Lean (`api`) |
|---|---|---|
| Chat model | local model, downloaded on first use (2.7 to 17.9 GB, chosen by GPU memory) | none; the chat provider set under Settings > AI & Model |
| Speech-to-text | the Whisper container (`vaf-stt`, image 7.5 GB), or a provider | the provider in `speech_stt_provider` only |
| Text-to-speech | the Piper container (`vaf-tts`, image 1.1 GB), or a provider | the provider in `speech_tts_provider`, or a TTS server you run elsewhere (`chatterbox`), or the macOS voice |
| Voice-call model | local model on request (5.1 GB) | the call runs on an API |
| Database, cache, document conversion | yes | yes |
| Browser (agent tool) | yes | yes |
| Sandbox environments | yes | yes |
| Memory embedding model | yes (130 MB, CPU) | yes, the same (130 MB, CPU): every stored memory is a vector of it |
| Speaker recognition | yes (27 MB, CPU) | yes, the same (27 MB, CPU) |
| Compose memory limits | about 8.3 GB, plus the chat model in RAM or VRAM | about 4.3 GB |

The sizes are measured on the machine this was written on (images with `docker image
ls`, models on disk); the memory limits are the ones in `docker-compose.memory.yml`.
Limits are ceilings, not what the services use. What they use, measured idle with
`docker stats` right after a start: the lean stack about 0.43 GB (the browser 342 MB, the
database, cache and document conversion under 40 MB each), the full stack about 1.43 GB
(the speech-to-text container alone 931 MB with its model loaded), plus the chat model in
RAM or VRAM when it runs.

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

A re-run of `install.sh` offers the profile that is set as its default (it takes only
1, 2 or Enter), and a run without a flag and without a terminal keeps what is set, so an
upgrade never switches a lean server back to downloading models. Re-running it with
`api` on an installation that ran the speech containers stops them (through
`vaf profile api`), and a profile the installer cannot write stops it before any service
starts. On a server, `vaf server provision --profile api` switches the same way
(`inference_profile.apply`).

Afterwards:

```bash
vaf profile                # the profile, the services it runs, what contradicts it
vaf profile api            # switch: the local model server and the speech containers stop
vaf profile api --prune    # ... and their containers, images and volumes are removed
vaf profile local          # switch back: the speech containers start again
```

The profile is written first and holds from that moment, also when the speech containers
(Docker does not answer) or the model server cannot be stopped: the downloads and the local
server are refused either way. `vaf profile api` then names what failed and exits with 1, the installers warn
on that, and the same command run again finishes the stop.

`--prune` frees the disk the speech containers took (their images and the models in their
volumes). It does not delete chat models you downloaded earlier: `vaf profile` says how
much is left in the `models` folder, and deleting it is yours to do. The command is not
behind the terminal door, like `vaf server`: what it changes is the machine's, and an
account that could run it from the agent's shell has `host_bash`, which reaches the same
config and the same docker directly.

In the web app the profile sits at the top of **Settings > AI & Model** (admin only), with
the same switch (`POST /api/system/inference-profile`, the same `inference_profile.apply`
behind it). The switch runs as a job, like the repair: the request returns at once and the
section follows it on `GET`, because a switch back to Full can pull the 7.5 GB speech image
while the HTTPS proxy in front of a server ends a request after 60 seconds. A second switch,
or a repair, is refused until it is done. Under Lean every local choice stays listed but cannot be picked: the chat
provider, vision, sub-agents and the thinker, the voice call's model and the speech
engines; the failover chain shows no local link. The first-run wizard says that skipping
the provider step leaves no local model to fall back on.

The profile is admin-only (`GLOBAL_CONFIG_KEYS`): it starts and stops containers and
decides about downloads for every account at once. A Settings save never writes it
(`inference_profile.without_profile_key` in both save lanes): a page loaded before a switch
would otherwise write the old profile back without stopping or starting anything.

## What the lean profile guarantees, and where

No chat, voice or speech model is downloaded or started, whatever a setting says (the
two small CPU models above stay). The profile is decided at the places that exist once
each, not by checks spread over the code:

- `backend.ensure_model_available`, the one download entry for a model, returns nothing
  and downloads nothing. Every lane passes it: chat, the voice call, thinking runs,
  sub-agents, the local failover link. The two lanes that download a model chosen by hand
  (the model download in the terminal settings and in the web settings) refuse as well. A
  download that was already running when the profile was switched ends within two seconds
  (`inference_profile.download_guard` in each lane's progress hook), and a caller that was
  waiting on the download lock reads the profile again before it starts.
- `ServerManager.start_server`, the one start of the local server, refuses, also for the
  voice lane, which otherwise asks past the provider gate, and for both local engines
  (llama.cpp and the managed vLLM, [VLLM_ENGINE.md](../llm/VLLM_ENGINE.md)). The vLLM install
  (`vaf engine vllm`, `vllm_server.install`) and its model download (`vllm_server.ensure_model`)
  refuse as well, so under `api` neither engine installs or runs. A start that was waiting for a
  server still loading its model ends when the profile changes, instead of waiting out the
  readiness budget.
- `Agent.load_model` loads no local model; a chat turn that has no provider says so in the
  chat, closes the turn and stores it, instead of promising a download. The same goes for
  an API provider whose backend could not be built (no key, a failed setup), on either
  profile (`turn_refusal_reason` in `headless_runner.py`). An agent that loaded a local
  model before the profile was switched does not run its next turn on it, nor a one-shot
  completion (`Agent.complete`, the shared `completion.complete`). The switch stops the
  local server, llama.cpp or vLLM, from whichever process started it
  (`backend.stop_local_server`, which checks that the pid in its pid file really is a llama
  server, and for vLLM that its command line names the managed environment): the desktop tray unloads
  it too, but only the tray runs that watchdog, so under `vaf server` and the macOS app
  nothing else did. A model loaded inside the process (the llama-cpp library lane, which
  the installer's Python 3.12 venv on Linux takes) and the tokenizer cached for counting
  tokens are released by the switch itself, or at the next turn when the switch came from
  another process (`release_disallowed_local_backends`); a load that was still running
  when the profile changed reads it again before and after it loads.
- The failover chain leaves its local link out, also when the profile changes while a
  request waits for its primary; a backup API provider stays.
- `service_stack.needed_services` leaves the speech containers out. The stack start, the
  status, the repair, `vaf top` and the installers read this one list: a container the
  profile leaves out is shown as `off`, never as a fault, and a repair does not bring it
  back.
- Speech-to-text uses the provider's answer or none: without local models there is no
  container to fall back to, so a provider error is reported rather than retried against
  a service that does not run.
- Text-to-speech uses the provider first. When it gives no audio, the two local lanes
  stay unused: the container (`docker`) does not run, and the Piper lane, which downloads
  its binary and voices, is skipped. Two lanes that download nothing here stay usable: a
  TTS server you run elsewhere (`speech_tts_engine = chatterbox`), which also speaks the
  voice replies in the messengers (`speech_client.synthesize`, which on the Full profile
  falls back to the container), and the macOS voice.

## A setting that contradicts the profile

Nothing is refused when it is written: `config.json` stays editable by hand, and the
places above are the guarantee. A setting that names a local lane on a lean installation
(the chat provider still `local`, a voice, thinking, sub-agent or vision lane on `local`,
the failover level `basic`, whose only link is the local model, speech switched on
without a provider) is named by `vaf profile` and `vaf doctor`
(`inference_profile_conflict`), with what to do.

A fresh lean installation starts in exactly that state: no chat provider yet. Set one
under Settings > AI & Model, or give the Veyllo key in the first-run wizard.

## Named boundaries

- **One profile, not one switch per lane.** A machine that should run its own
  speech-to-text but chat through an API is a `local` installation with an API chat
  provider; the lean profile is for machines that run no chat, voice or speech model.
- **No browser-less profile yet.** The browser is an agent tool, not a model, and stays in
  both profiles. Whether a smaller server needs to go without it (about 2 GB of memory
  limits) is decided when a pilot measures the real use.
- **Not on the framework facade.** An embedder chooses the provider per agent; the profile
  is a rule about what an installation of the product carries.
