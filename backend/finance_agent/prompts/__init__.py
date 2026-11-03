"""Version-controlled model instructions."""

from pathlib import Path


PROMPT_ROOT = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load one named Markdown prompt without allowing path traversal."""
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz-" for char in name):
        raise ValueError("prompt names may contain only lowercase letters and hyphens")
    return (PROMPT_ROOT / f"{name}.md").read_text().strip()
