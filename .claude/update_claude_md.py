"""
Hook script — runs after every Edit/Write tool call.

Updates the "Last updated" timestamp in CLAUDE.md so future sessions know
how fresh the documentation is. Intentionally lightweight — it doesn't
regenerate the whole file, just stamps it.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

# Only update if a .py or .js file was the target (not CLAUDE.md itself).
tool_input = " ".join(sys.argv[1:])
if not any(ext in tool_input for ext in (".py", ".js", ".html", ".css")):
    sys.exit(0)

if not CLAUDE_MD.exists():
    sys.exit(0)

text = CLAUDE_MD.read_text(encoding="utf-8")
stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# Update or insert "Last updated" line near the top.
pattern = r"^_Last updated:.*$"
replacement = f"_Last updated: {stamp}_"
if re.search(pattern, text, flags=re.MULTILINE):
    text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
else:
    # Insert after the first heading.
    text = re.sub(
        r"(^# .+\n)",
        rf"\1\n{replacement}\n",
        text,
        count=1,
        flags=re.MULTILINE,
    )

CLAUDE_MD.write_text(text, encoding="utf-8")
