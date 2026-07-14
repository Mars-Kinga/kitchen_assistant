"""Manual wrapper for the kitchen assistant's optional Doubao connection test.

This script is intentionally outside pytest discovery.  It makes a real request
only when the caller has explicitly exported ARK_API_KEY.
"""

from pathlib import Path
import runpy


if __name__ == "__main__":
    target = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "kitchen_assistant"
        / "scripts"
        / "test_doubao_connection.py"
    )
    runpy.run_path(str(target), run_name="__main__")
