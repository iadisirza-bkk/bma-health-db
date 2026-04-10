"""Shared LaTeX utilities -- escape, template dir, font registration.

All LaTeX-related helpers MUST import from here.
Do NOT copy latex_escape() to other files.
"""
from __future__ import annotations

from pathlib import Path

# Shared template directory
TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "latex"


def latex_escape(text: str) -> str:
    """Escape special LaTeX characters including backslash.

    Order matters: backslash must be replaced first, but we use a
    placeholder to avoid double-escaping braces in \\textbackslash{}.
    """
    # Step 1: Replace backslash with placeholder
    text = text.replace("\\", "\x00BACKSLASH\x00")
    # Step 2: Replace other specials
    for char, replacement in {
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }.items():
        text = text.replace(char, replacement)
    # Step 3: Replace placeholder with final backslash escape
    text = text.replace("\x00BACKSLASH\x00", r"\textbackslash{}")
    return text


def register_thai_font() -> None:
    """Register Google Sans font for matplotlib Thai text rendering."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.font_manager as fm

        font_path = TEMPLATE_DIR / "assets" / "GoogleSans-Regular.ttf"
        if font_path.exists():
            fm.fontManager.addfont(str(font_path))
            import matplotlib.pyplot as plt
            plt.rcParams["font.family"] = "Google Sans"
    except Exception:
        pass  # matplotlib not available or font not found
