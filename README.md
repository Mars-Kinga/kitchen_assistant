# Kitchen Assistant Robot Skill

A Python Robot Skill runtime for guided cooking. Deterministic session logic handles recipe selection, ingredient checks, cooking steps, timers, pause/resume, and completion. Optional Qwen services provide recipe generation, kitchen Q&A, and camera-based ingredient recognition.

The public code uses mock robot adapters. Speech can use an optional TTS backend; motion, display, lighting, and expressions are simulated. Physical robot SDK integration is not included.

[中文说明](docs/README.zh-CN.md) · [Architecture](docs/ARCHITECTURE.md) · [Integration requirements](docs/MISSING_INPUTS.md)

## Published version and local development

Documentation reviewed on **2026-11-03** against the public source snapshot [`e645b53`](https://github.com/Mars-Kinga/kitchen_assistant/tree/e645b53b2bd5b59bf69290f04e42e5729a9c88fc). This documentation update does not publish newer local application code.

| Area | Available in this public source snapshot | Newer local implementation, not yet published on `main` |
| --- | --- | --- |
| Interaction | Chinese CLI, optional voice input, camera ingredient questions | LAN web console, photo inventory confirmation, robot feedback events |
| Recipes | 100 fixed recipes, validated generated cache, one AI candidate per request | 440 fixed recipes; ingredient requests can return 1–3 complete options or meal combinations |
| Model configuration | Environment variables only | Allowed values in `config/qwen.env` override environment variables |
| Tutorial import | Not included | Video/image-text import, editable drafts, provenance and confirmation |
| Installation | Python virtual environment and CLI | Portable install/start/check scripts and port fallback |

Local implementation is not a release or a claim of device validation. The commands below apply to the code available in this repository.

## What the public runtime demonstrates

- Local intent routing and a single active cooking session with explicit states.
- Local-first recipe lookup, serving-size normalization, dietary checks, and validated AI generation when local results do not match.
- One complete AI candidate per request, with at most one targeted correction for eligible JSON/validation failures within the original time budget. SDK retries default to zero; correction is a separate request.
- JSON Schema and capability validation, per-Skill fault isolation, and independent output-channel fallback.
- Local timers with pause/resume and confirmation rules. Recipe-specific timer actions can request confirmation or advance a step; timers do not establish food safety or doneness.
- Python tests and a GitHub Actions matrix configured for Python 3.11, 3.12, and 3.13. The matrix configuration is not a statement that tests were rerun for this documentation update.

## Interaction flow

```text
Chinese text / optional ASR transcript
  -> explicit visual request, or SkillManager / local intent router
  -> KitchenSession
  -> validated five-channel feedback
  -> RuntimeExecutor
  -> MockRobotSDK + optional VoiceOutput
```

The session collects a request and preferences, finds candidates, confirms a recipe and relevant animal-ingredient conditions, then enters `COOKING`. It can move to `PAUSED`, `COMPLETED`, or `CANCELLED`. The model does not directly control hardware or session transitions.

## Quick start

Use Python 3.11–3.13. The current rule-based router and examples use Chinese; English cooking sentences are not equivalent routing examples.

```bash
git clone https://github.com/Mars-Kinga/kitchen_assistant.git
cd kitchen_assistant
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
python robot_main.py --no-play "我想做番茄炒蛋"
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1` instead. Install `requirements.txt` if you only need to run the application.

The local catalog, text interaction with `--no-play`, and default tests do not need an API key. This published snapshot does not have `--console`, tutorial import, or the portable installer.

## Optional Qwen configuration

This public version reads **environment variables only**. Copying the example to `.env` or `config/qwen.env` does not load it automatically. Set the variables in the terminal that starts the application.

macOS / Linux:

```bash
export DASHSCOPE_API_KEY='your_api_key'
export QWEN_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1'
export QWEN_TEXT_MODEL='qwen3-omni-flash'
export QWEN_VISION_MODEL='qwen3-vl-flash'
export QWEN_TIMEOUT_SECONDS='25'
export QWEN_VISION_TIMEOUT_SECONDS='20'
export QWEN_MAX_RETRIES='0'
```

Windows PowerShell uses `$env:NAME='value'`, for example `$env:DASHSCOPE_API_KEY='your_api_key'`. Use an endpoint and models your account can access; the names above are project defaults, not a guarantee of service availability. Restart the application after changing its launch environment. Never commit credentials.

The provider checks local recipes and validated cache before calling Qwen. `OnlineRecipeSearchProvider` remains a placeholder; generated recipes are not web search results. To explicitly make a real connection check:

```bash
python scripts/check_qwen_connection.py
```

## Text, vision and optional voice

```bash
# Interactive text mode without audio playback
python robot_main.py --no-play

# Ingredient-based request
python robot_main.py --no-play "我有胡萝卜和鸡肉，不知道做什么"

# Explicit visual question using the local camera
python robot_main.py --no-play "这是大葱还是小葱"

# Optional voice path, after installing requirements-voice.txt and configuring ASR
python robot_main.py --voice
python robot_main.py --voice --manual
```

ASR uses `MIMO_API_KEY` or `TOKEN_PLAN_API_KEY`, with `MIMO_BASE_URL` and `MIMO_ASR_MODEL`; these are independent of Qwen. Voice code includes VAD, transcription and TTS, but target-device recording, playback and conversational behavior still require verification. Recordings are written to `voice_outputs/`. Camera pictures are handled in memory and do not automatically modify the cooking session.

## Boundaries

This is a single-process demonstration with one active session, without user accounts or session/timer recovery after restart. Risk-related text is handled by local rules before ordinary model Q&A; those rules are not a validated emergency-response system. Camera output and timers cannot confirm food safety, allergens, doneness, or the safety of the physical environment. Real robot integration, live recipe search, and the newer local features listed above are not included in the published source snapshot.
