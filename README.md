# Kitchen Assistant Robot Skill

A Python Robot Skill runtime for guided cooking interactions. The project combines deterministic state-machine orchestration with optional Qwen-powered recipe generation, kitchen Q&A, and camera-based ingredient recognition.

The public repository runs locally with mock adapters for five robot capabilities: speech, display, motion, lighting, and facial expression. It is structured so those adapters can be replaced by a robot SDK during deployment; proprietary hardware integration is not included here.

[中文说明](docs/README.zh-CN.md)

## What it demonstrates

- A complete interaction loop from intent routing to safe completion or cancellation.
- A state machine for preference collection, recipe selection, confirmation, thawing checks, cooking guidance, pausing, timers, and feedback.
- Optional Qwen integration through an OpenAI-compatible API for recipe generation, kitchen questions, and visual ingredient recognition.
- A validated local recipe catalog with serving-size normalization and offline fallback behavior.
- JSON Schema validation, capability checks, fault isolation, and graceful degradation for robot actions.
- Offline unit, scenario, and regression tests across Python 3.11, 3.12, and 3.13 in GitHub Actions.

## Interaction flow

```text
User input
  -> intent router
  -> Kitchen Skill state machine
  -> validated action plan
  -> runtime executor
  -> speech / display / motion / lighting / expression adapters
  -> user feedback or safe termination
```

Typical states:

```text
IDLE
  -> COLLECTING_REQUEST / COLLECTING_PREFERENCES
  -> SEARCHING_RECIPES
  -> PRESENTING_CANDIDATES
  -> WAITING_RECIPE_CONFIRMATION
  -> WAITING_MEAT_THAW (when raw meat is used)
  -> COOKING
  -> COMPLETED / CANCELLED
```

## Architecture

```text
robot_main.py                  Main CLI and runtime composition
runtime_core/
  agent.py                     Intent routing
  skill_manager.py             Manifest, schema, and capability validation
  executor.py                  Five-channel action execution and fallback
  ingredient_vision.py         Vision intent and result validation
  mock_robot_sdk.py            Replaceable robot capability adapters
skills/kitchen_assistant/
  kitchen/                     Deterministic state machine and safety rules
  providers/                   Local, AI, cache, and future search providers
  llm/                         Qwen text and vision clients
  recipes/                     Curated catalog and generated cache
  schemas/                     Input and output contracts
tests/                         Unit, scenario, and entry-point regression tests
```

More detail is available in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

Python 3.11-3.13 is recommended.

```bash
git clone https://github.com/Mars-Kinga/kitchen_assistant.git
cd kitchen_assistant

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt

python -m pytest -q
python robot_main.py --no-play "I want to make tomato and eggs"
```

Text interaction, the mock robot capabilities, the local catalog, and the default test suite do not require an API key.

## Optional Qwen features

Copy the example configuration and add your own credentials:

```bash
cp config/qwen.env.example .env
```

The relevant variables are:

```bash
DASHSCOPE_API_KEY=your_api_key
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
QWEN_TEXT_MODEL=qwen3-omni-flash
QWEN_VISION_MODEL=qwen3-vl-flash
```

The application checks the local catalog and validated cache first. It calls the model only when no local recipe satisfies the request. Default tests remain offline; the explicit connection check is:

```bash
python scripts/check_qwen_connection.py
```

## Example commands

```bash
# Interactive text mode
python robot_main.py

# One-shot text input without audio playback
python robot_main.py --no-play "I have chicken and carrots"

# Camera-based ingredient question on macOS
python robot_main.py --no-play "Is this a scallion or a leek?"
```

## Safety and reliability

- Timer completion is treated as a minimum reference; the assistant still asks the user to verify observable doneness.
- Raw meat and seafood flows include thawing and handling checks.
- Fire, gas, smoke, burns, and electrical hazards take priority over the cooking flow.
- Invalid manifests, missing capabilities, malformed model output, and individual output-channel failures degrade safely instead of crashing the full runtime.
- The assistant never claims to see the physical environment unless a camera result is available.

## Current boundary

This repository contains a locally runnable demonstration runtime. It does not connect to a production AgiBot device, and `OnlineRecipeSearchProvider` is currently an extension point rather than a live web-search implementation.
