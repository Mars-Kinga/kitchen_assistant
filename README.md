# Kitchen Assistant Robot Skill

A local guided-cooking runtime with a LAN web console, 440 fixed recipes, optional Qwen text and vision services, and video/image-text tutorial import. A deterministic session state machine controls recipe selection, ingredient confirmation, cooking steps, timers, pause/resume, and completion.

Robot motion, display, lighting, and expressions use mock adapters. Speech can use an optional TTS backend. The project does not connect to a physical robot SDK or provide live web recipe search.

[中文详细说明](docs/README.zh-CN.md) · [Run on another computer](START_HERE.md) · [Architecture](docs/ARCHITECTURE.md) · [Configuration and integration requirements](docs/MISSING_INPUTS.md)

## Features

- **Local-first recipes:** 440 fixed recipes plus validated generated cache. Named-dish generation returns one complete recipe; ingredient requests can return 1–3 complete options, including meal combinations. Results are checked locally before use.
- **Photo inventory:** Upload a picture or capture a camera frame, edit the recognized ingredients, then confirm them before requesting recipes.
- **Guided cooking:** Serving-size adjustment, animal-ingredient confirmation, step navigation, explicit console timer start, local pause/resume, and independent preparation suggestions.
- **LAN console:** Browse complete recipes and follow cooking, camera, timer and simulated robot status from a phone or computer. Polling runs every 500 ms without overlapping requests; the page displays the latest feedback and its execution stage.
- **Tutorial import:** Import a supported public Xiaohongshu video/image-text post or upload MP4/MOV. Review editable drafts with source evidence, AI suggestions and user edits before saving. Background import does not change the active cooking task.
- **Portable setup:** Windows and macOS/Linux install, start and check scripts, automatic port fallback, and an explicit package allowlist.

## Quick start

Python 3.11–3.13 is covered by the Python CI matrix. The portable scripts accept Python 3.11–3.14. Chinese examples below match the current local intent router.

```bash
git clone https://github.com/Mars-Kinga/kitchen_assistant.git
cd kitchen_assistant
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python robot_main.py --console-only --no-play
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. Alternatively, use `install.cmd` followed by `start.cmd`; on macOS/Linux use `bash install.sh` and `bash start.sh`. See [START_HERE.md](START_HERE.md) for target-computer setup.

Open the address printed at startup. The preferred port is **18765**; when occupied, the server tries the next ports, up to 20 attempts by default. LAN devices use the computer's printed LAN address, not `127.0.0.1`. The console shares one active session across all connected devices.

```bash
# Text and console together
python robot_main.py --console --no-play

# One text request, without audio playback
python robot_main.py --no-play "我想做番茄炒蛋"

# Camera ingredient question
python robot_main.py --no-play "这是大葱还是小葱"
```

The local catalog and text cooking workflow work without an API key. Photo recognition, AI generation and tutorial understanding require your own working model configuration and network access.

## Model configuration

Copy `config/qwen.env.example` to `config/qwen.env`, then fill in credentials and an endpoint/models your account can access. The installation scripts create this file when it is absent. The local file is ignored by Git.

```ini
DASHSCOPE_API_KEY=your_api_key
QWEN_BASE_URL=your_authorized_OpenAI_compatible_endpoint
QWEN_TEXT_MODEL=qwen3-omni-flash
QWEN_VISION_MODEL=qwen3-vl-flash
QWEN_TIMEOUT_SECONDS=45
QWEN_VISION_TIMEOUT_SECONDS=20
QWEN_MAX_RETRIES=0
```

The seven allowed, nonempty values in `config/qwen.env` override the corresponding environment variables. The root `.env` is not loaded, and configuration is not executed as a script. Restart after changes. The model names shown are project defaults, not a guarantee of account access.

Recipes use local results first unless the request explicitly bypasses cache. SDK retries default to zero; eligible invalid model output can trigger at most one targeted correction within the original time budget. Valid complete candidates can be retained when other candidates are invalid or truncated. AI generation is distinct from web search.

Video model overrides use environment variables `QWEN_VIDEO_MODEL` and `QWEN_VIDEO_TIMEOUT_SECONDS`; they are not among the local file's allowed keys. Image-text import uses the vision model. The optional third-party video acquisition channel requires separate credentials. Details are in [configuration notes](docs/MISSING_INPUTS.md).

## Cooking and import behavior

The console's main button follows the current state: confirm fresh/thawed ingredients, resume guidance, start the timer, then navigate or complete. Leaving a step through console navigation clears its timer. Natural-language requests to skip an unstarted or running timer follow confirmation rules instead. Specific recipe workflows may have their own timer-end confirmation or advancement actions; timer expiry is not proof of doneness.

Video uploads are limited to 100 MB and 180 seconds. Image-text import supports up to 16 images with per-image and total prepared-data limits. Imported recipes remain editable drafts until confirmed. Starting another imported recipe is rejected while a cooking task is active. Temporary media is cleaned after processing; confirmed recipes are stored separately under `recipes/imported/`.

## Optional voice

The code includes VAD recording, ASR transcription and Edge/pyttsx3 speech output. Install `requirements-voice.txt`, configure the independent ASR variables `MIMO_API_KEY` (or `TOKEN_PLAN_API_KEY`), `MIMO_BASE_URL` and `MIMO_ASR_MODEL`, then use `python robot_main.py --voice` or `--voice --manual`.

`--no-play` skips speech playback. Recordings are written to `voice_outputs/`; camera pictures are handled in memory. Complete recording, service, playback and conversational behavior still require target-device validation.

## Development checks

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
node tests/test_kitchen_console_recommendations.cjs
node tests/test_robot_feedback_ui.cjs
node tests/test_video_console_ui.cjs
python scripts/validate_recipe_catalog.py
```

Default tests use local substitutes for model/network services. Python HTTP tests bind temporary loopback ports. Node is needed for the UI regression checks, not for running the application. GitHub Actions includes the Python matrix and a separate console UI job.

On 2026-11-03, local validation on macOS / Python 3.14.6 passed all 487 Python tests and all three Node UI regression scripts. The 440-recipe audit, text startup, and extracted-package HTTP smoke check also passed. Cloud services, physical hardware, Windows installation and interactive browser behavior were not revalidated in this run.

## Packaging and data

```bash
python scripts/build_portable.py
# Optional: include personal generated/imported recipes in a private migration package
python scripts/build_portable.py --include-local-recipes
```

The package includes code, fixed recipes, shared guides and configuration examples, with a manifest and SHA-256 checksum. Credentials, host configuration, virtual environments, logs, media and local `doc/` requirements are excluded. Generated and imported recipes are runtime data; do not add newly generated files to Git. Session state, active timers and unconfirmed drafts are not restored after restart.

## Boundaries

This is a single-process demonstration with one active kitchen session and no user accounts. The console's same-origin check is not authentication. Mock and offline tests do not establish real robot behavior, Windows device compatibility, cloud access or browser usability. The assistant cannot verify food safety, allergens, doneness or emergency conditions, and provides no physical cooking-device controls.
