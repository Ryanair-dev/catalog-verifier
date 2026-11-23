"""Generate static/dark.css: the app-wide dark theme (Settings > Appearance).

Run after adding UI with new colours:   python scripts/build_dark_css.py

Why generated: the app has ~500 literal colours in its stylesheets plus ~850 inline
`style="..."` colour declarations in index.html / app.js / families.js / create_po.js.
A hand-written dark theme would miss most of them. This script reads them all and
writes a dark counterpart for each, using ONE colour rule (map_color below) that keeps
every colour's hue and only moves its lightness, by the colour's job:

  * background: white cards -> SURFACE; grey page/hover/borders -> darker greys;
                pale tints (badges, chips) -> deep tints of the same hue;
                solid / mid colours (purple buttons, navy headers) are kept.
  * text/icons: dark greys/navy -> light greys; coloured text is lifted to >= 64%
                lightness so it stays readable; light text (white on purple) is kept.
  * borders:    light greys -> dark greys; light tints -> mid-dark tints.

Output sections (all scoped to html[data-theme="dark"]):
  1. tokens      - the :root variables of styles.css, remapped by name/role.
  2. stylesheets - every colour declaration of styles.css and create_po.css, mirrored
                   with +1 class +1 type specificity, so the dark value wins over the
                   light one while the rules keep their order among themselves.
                   Declarations that use var(--x) are mirrored too (unchanged), so a
                   var-based rule that used to beat a literal one still does.
  3. inline      - attribute rules ([style*="color:#6b7480"]) with !important, which
                   is what it takes to beat an inline style. Longer strings come later
                   so "background:#fff7ed" wins over its prefix "background:#fff".
  4. manual      - the few things a rule can't infer (MANUAL below).
"""
from __future__ import annotations

import colorsys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
OUT = STATIC / "dark.css"
D = 'html[data-theme="dark"]'

STYLESHEETS = ["styles.css", "create_po.css"]
INLINE_SOURCES = ["index.html", "app.js", "families.js", "create_po.js"]

SURFACE = (0x1b, 0x1e, 0x25)  # cards / panels (white in light mode)

COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([^)]*\)|\bwhite\b", re.I)
COLOR_PROPS = re.compile(
    r"^(color|background|background-color|background-image|border|border-(top|right|bottom|left)"
    r"|border-(top-|right-|bottom-|left-)?color|outline|outline-color|fill|stroke|caret-color"
    r"|text-decoration-color|accent-color|column-rule-color)$", re.I)


# ---------------------------------------------------------------------------
# colour model
# ---------------------------------------------------------------------------

def parse_color(tok: str):
    t = tok.strip().lower()
    if t == "white":
        return (255, 255, 255, None)
    if t.startswith("#"):
        h = t[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), None)
    m = re.match(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)", t)
    if not m:
        return None
    a = m.group(4)
    if a is not None:
        a = float(a[:-1]) / 100 if a.endswith("%") else float(a)
    return (int(float(m.group(1))), int(float(m.group(2))), int(float(m.group(3))), a)


def fmt(r, g, b, a=None) -> str:
    r, g, b = (max(0, min(255, round(x))) for x in (r, g, b))
    if a is None:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"rgba({r}, {g}, {b}, {a:g})"


def hls_to_rgb(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h, max(0, min(1, l)), max(0, min(1, s)))
    return r * 255, g * 255, b * 255


def map_rgb(rgb, role: str):
    r, g, b = (x / 255 for x in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    # Neutral by chroma, not HLS saturation (which calls #f7f8fa 23% "saturated" near white).
    chroma = max(r, g, b) - min(r, g, b)
    neutral = (chroma < 0.041 if l > 0.85 else chroma < 0.12) or (l < 0.22 and role == "fg")
    if role == "bg":
        if neutral and l > 0.985:
            return SURFACE
        if neutral and l >= 0.97:         # page background (grey-50): darker than the cards
            return hls_to_rgb(h, 0.09, min(s, 0.14))
        if neutral and l > 0.6:           # zebra / hover / chips: a step lighter than the cards
            return hls_to_rgb(h, 0.13 + (0.97 - l) * 0.9, min(s, 0.14))
        if not neutral and l > 0.85:      # pale tint: badge / chip / highlight
            return hls_to_rgb(h, 0.15 + (1 - l) * 0.45, min(s, 0.55) * 0.75)
        if not neutral and l > 0.6:       # light-mid tint
            return hls_to_rgb(h, 0.27, s * 0.6)
        return tuple(x * 255 for x in (r, g, b))  # solid / mid colour: keep
    if role == "border":
        if neutral and l > 0.6:
            return hls_to_rgb(h, 0.17 + (0.99 - l) * 0.6, min(s, 0.12))
        if not neutral and l > 0.6:
            return hls_to_rgb(h, 0.33, s * 0.6)
        return tuple(x * 255 for x in (r, g, b))
    # fg: text, icons, fills, strokes
    if neutral:
        if l < 0.6:
            return hls_to_rgb(h, 0.93 - l * 0.5, min(s, 0.12))
        return tuple(x * 255 for x in (r, g, b))  # already light (white text on solid colour)
    if l < 0.62:
        return hls_to_rgb(h, max(0.62, min(l + 0.22, 0.72)), s * 0.8)
    return tuple(x * 255 for x in (r, g, b))


def map_value(value: str, role: str) -> str:
    """Map every colour inside a CSS value (keeps widths, gradients, var(), etc.)."""
    if role == "keep":
        return value
    def sub(m):
        c = parse_color(m.group(0))
        if c is None:
            return m.group(0)
        r, g, b, a = c
        if role == "bg" and a is not None and a < 0.7 and (r, g, b) != (255, 255, 255):
            # translucent dark overlays / tints over content: keep dark overlays dark
            hh, ll, ss = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            if ll < 0.35:
                return m.group(0)
        if a is not None and (r, g, b) == (255, 255, 255):
            # translucent white (glass panels, highlights) -> translucent surface
            return fmt(*SURFACE, a)
        nr, ng, nb = map_rgb((r, g, b), role)
        return fmt(nr, ng, nb, a)
    return COLOR_RE.sub(sub, value)


def role_of(prop: str) -> str:
    p = prop.lower()
    if p.startswith("background"):
        return "bg"
    if p.startswith("border") or p.startswith("outline") or p.startswith("column-rule"):
        return "border"
    return "fg"


# ---------------------------------------------------------------------------
# 1. tokens
# ---------------------------------------------------------------------------

def token_role(name: str, value: str) -> str | None:
    n = name.lstrip("-")
    if n.startswith("shadow"):
        return "shadow"
    m = re.match(r"([a-z]+)-(\d+)$", n)
    if not m:
        return None
    family, step = m.group(1), int(m.group(2))
    if family == "grey":
        # 50 is the page; 100-300 are mostly borders/dividers (and hover fills): lighter than cards.
        return "bg" if step == 50 else ("border" if step <= 300 else "fg")
    if family == "navy":
        return "fg"
    if step <= 100:
        return "bg"
    if step <= 300:
        return "border"
    if family == "purple" and step >= 800:
        return "keep"   # used as dark backgrounds (table headers, hover of the primary button)
    if step >= 600:
        return "fg"
    return "keep"       # -400/-500: solid accents (buttons, dots)


TOKENS: dict[str, str] = dict(
    re.findall(r"(--[\w-]+)\s*:\s*([^;]+);",
               re.search(r":root\s*\{([^}]*)\}", (STATIC / "styles.css").read_text(encoding="utf-8")).group(1)))
VAR_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)")


def resolve_vars(value: str, prop_role: str) -> str:
    """A token is remapped for ONE role (see token_role). Where it is used in the other role
    (e.g. --purple-800, a dark background token, used as heading text), swap in that colour
    mapped for this property's role instead, so the text is lifted / the background stays dark."""
    def sub(m):
        name, fallback = m.group(1), (m.group(2) or "").strip()
        light = TOKENS.get(name, "").strip()
        if name not in TOKENS and fallback and COLOR_RE.fullmatch(fallback) and not name.startswith("--vc"):
            return map_value(fallback, prop_role)  # undefined token: its fallback colour is what shows
        if not light or not COLOR_RE.fullmatch(light):
            return m.group(0)
        trole = token_role(name, light)
        text_prop, text_tok = prop_role == "fg", trole == "fg"
        if trole in ("bg", "border", "fg") and text_prop == text_tok:
            return m.group(0)       # used as intended: the dark token value applies
        if trole == "keep" and not text_prop:
            return m.group(0)       # solid accent used as a fill: keep
        return map_value(light, prop_role)
    return VAR_RE.sub(sub, value)


def build_tokens(css: str) -> list[str]:
    root = re.search(r":root\s*\{([^}]*)\}", css)
    out = []
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root.group(1)):
        role = token_role(name, value)
        if role is None or role == "keep":
            continue
        if role == "shadow":
            out.append(f"  {name}: {re.sub(r'rgba\(17, *24, *39, *', 'rgba(0, 0, 0, ', value.strip())};")
            continue
        new = map_value(value.strip(), role)
        if new != value.strip():
            out.append(f"  {name}: {new};")
    return out


# ---------------------------------------------------------------------------
# 2. stylesheets
# ---------------------------------------------------------------------------

def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_blocks(css: str, i: int = 0, depth: int = 0):
    """Yield (prelude, body) for rules and ('@media ...', children) for nested at-rules."""
    items = []
    n = len(css)
    while i < n:
        j = css.find("{", i)
        k = css.find("}", i)
        if k != -1 and (j == -1 or k < j):
            return items, k + 1
        if j == -1:
            break
        prelude = css[i:j].strip()
        if prelude.startswith("@media") or prelude.startswith("@supports"):
            children, i = parse_blocks(css, j + 1, depth + 1)
            items.append((prelude, children))
            continue
        # plain rule or @keyframes/@font-face (skipped by caller): find matching brace
        lvl, p = 1, j + 1
        while p < n and lvl:
            if css[p] == "{":
                lvl += 1
            elif css[p] == "}":
                lvl -= 1
            p += 1
        items.append((prelude, css[j + 1:p - 1]))
        i = p
    return items, i


def scope_selector(sel: str) -> str | None:
    s = sel.strip()
    if not s or s.startswith(":root"):
        return None
    if s.startswith("html"):
        return D + s[4:]
    return f"{D} {s}"


def mirror_rule(prelude: str, body: str) -> str | None:
    if prelude.startswith("@") or "%" in prelude.split(",")[0] and prelude.strip()[0].isdigit():
        return None
    decls = []
    for decl in body.split(";"):
        if ":" not in decl:
            continue
        prop, value = decl.split(":", 1)
        prop, value = prop.strip(), value.strip()
        if not COLOR_PROPS.match(prop) and not (prop.startswith("--") and COLOR_RE.search(value)):
            continue
        has_literal = bool(COLOR_RE.search(value))
        if not has_literal and "var(" not in value:
            continue
        important = "!important" in value
        value = value.replace("!important", "").strip()
        role = role_of(prop) if not prop.startswith("--") else guess_custom_role(prop)
        new = map_value(resolve_vars(value, role) if role != "keep" else value, role)
        decls.append(f"{prop}: {new}{' !important' if important else ''}")
    if not decls:
        return None
    sels = [scope_selector(s) for s in split_selectors(prelude)]
    sels = [s for s in sels if s]
    if not sels:
        return None
    return ",\n".join(sels) + " { " + "; ".join(decls) + "; }"


def guess_custom_role(prop: str) -> str:
    """Role of a component-scoped custom property (create_po.css's .cpo { --panel: ... })."""
    p = prop.lower()
    if "shadow" in p or "glow" in p:
        return "keep"
    if any(k in p for k in ("bg", "panel", "paper", "card", "band", "soft", "inset", "surface")):
        return "bg"
    if any(k in p for k in ("border", "rule", "line")):
        return "border"
    if any(k in p for k in ("ink", "text", "muted", "faint")):
        return "fg"
    return "keep"  # --primary, --accent, --mint...: solid accent colours


def split_selectors(prelude: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in prelude:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def build_stylesheet(css: str) -> list[str]:
    items, _ = parse_blocks(strip_comments(css))
    out = []
    for prelude, body in items:
        if isinstance(body, list):
            inner = [r for p, b in body if isinstance(b, str) and (r := mirror_rule(p, b))]
            if inner:
                out.append(prelude + " {\n" + "\n".join("  " + r for r in inner) + "\n}")
        elif not prelude.startswith("@"):
            r = mirror_rule(prelude, body)
            if r:
                out.append(r)
    return out


# ---------------------------------------------------------------------------
# 3. inline styles
# ---------------------------------------------------------------------------

INLINE_DECL = re.compile(
    r"(?<![\w-])(color|background-color|background|border-color|border(?:-top|-right|-bottom|-left)?"
    r"|outline|fill|stroke)\s*:\s*([^;\"'`{}<>]{1,120})", re.I)


def css_attr_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_inline() -> list[str]:
    exact: dict[str, str] = {}      # selector substring -> rule
    colors_in_js: set[str] = set()
    for name in INLINE_SOURCES:
        text = (STATIC / name).read_text(encoding="utf-8")
        for m in INLINE_DECL.finditer(text):
            prop, value = m.group(1), m.group(2).rstrip()
            value = re.sub(r"\s*!important$", "", value)
            if value.startswith("var(") and "${" not in value:
                # Token used inline, e.g. style="color: var(--purple-800)": only needs a rule when
                # the token is tuned for the other role (dark-background token used as text).
                full = VAR_RE.match(value)
                if full:
                    needle = m.group(0)[: m.group(0).find(full.group(0)) + len(full.group(0))]
                    resolved = resolve_vars(full.group(0), role_of(prop))
                    if resolved != full.group(0):
                        target = "background-color" if prop.lower() == "background" else prop.lower()
                        exact[needle] = f'{D} [style*="{css_attr_escape(needle)}"] {{ {target}: {resolved} !important; }}'
                continue
            colors = COLOR_RE.findall(value)
            if not colors:
                continue
            # Match the declaration as written, from the property name to the end of its last colour.
            last = value.rfind(colors[-1]) + len(colors[-1])
            needle = m.group(0)[: m.group(0).rfind(colors[-1]) + len(colors[-1])]
            if "${" in needle or "var(" in needle or needle.count("(") != needle.count(")"):
                continue  # runtime value, token-driven already, or cut mid-function (would break the CSS)
            mapped_value = map_value(value[:last], role_of(prop))
            if mapped_value == value[:last]:
                continue
            target_prop = prop.lower()
            if target_prop in ("border", "border-top", "border-right", "border-bottom", "border-left"):
                target_prop = target_prop + "-color" if target_prop != "border" else "border-color"
                mapped = ", ".join(map_value(c, "border") for c in colors[-1:])
            elif target_prop == "background" and not re.search(r"gradient|url\(", value, re.I):
                target_prop = "background-color"
                mapped = map_value(colors[-1], "bg")
            else:
                mapped = mapped_value
            exact[needle] = f'{D} [style*="{css_attr_escape(needle)}"] {{ {target_prop}: {mapped} !important; }}'
        if name.endswith(".js"):
            colors_in_js.update(c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", text))

    # Colours chosen at runtime (style="color:${c}") and set through el.style (which the
    # browser writes back as "color: rgb(r, g, b)"): generic rules per colour.
    generic: dict[str, str] = {}
    for c in sorted(colors_in_js):
        rgb = parse_color(c)
        rgb_txt = f"rgb({rgb[0]}, {rgb[1]}, {rgb[2]})"
        for prop, role, target in (("color", "fg", "color"), ("background", "bg", "background-color"),
                                   ("background-color", "bg", "background-color"),
                                   ("border-color", "border", "border-color")):
            mapped = map_value(c, role)
            if mapped == c:
                continue
            for needle in (f"{prop}:{c}", f"{prop}: {c}", f"{prop}: {rgb_txt}"):
                if needle not in exact:
                    generic[needle] = f'{D} [style*="{needle}" i] {{ {target}: {mapped} !important; }}'
    ordered = sorted(generic.items(), key=lambda kv: len(kv[0])) + sorted(exact.items(), key=lambda kv: len(kv[0]))
    return [rule for _, rule in ordered]


# ---------------------------------------------------------------------------
# 4. manual
# ---------------------------------------------------------------------------

MANUAL = f"""
/* Native controls, scrollbars and <select> lists follow the theme. */
{D} {{ color-scheme: dark; }}

/* The Offer Analysis loading car (lottie, near-black lines on transparent): draw it light. */
{D} lottie-player {{ filter: invert(1) hue-rotate(180deg); }}

/* Tailwind utility classes used for surfaces/text in index.html. */
{D} .bg-white {{ background-color: {fmt(*SURFACE)} !important; }}
{D} .text-\\[\\#7a8697\\] {{ color: {map_value('#7a8697', 'fg')} !important; }}

/* SVG icons with hard-coded stroke colours. */
{D} [stroke="#94a3b8"] {{ stroke: {map_value('#94a3b8', 'fg')}; }}
{D} [stroke="#4f46e5"] {{ stroke: {map_value('#4f46e5', 'fg')}; }}
{D} [stroke="#6366f1"] {{ stroke: {map_value('#6366f1', 'fg')}; }}
{D} [stroke="#0284c7"] {{ stroke: {map_value('#0284c7', 'fg')}; }}

/* Shadows: light-mode drop shadows are tuned for white; use deeper black ones. */
{D} .card, {D} .stat-card {{ box-shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 2px 8px rgba(0, 0, 0, .25); }}

/* Form fields without their own background (Tailwind preflight leaves them transparent/white). */
{D} input:not([type="checkbox"]):not([type="radio"]):not([type="range"]), {D} select, {D} textarea {{
  background-color: {fmt(*map_rgb((0xfb, 0xfc, 0xfd), 'bg'))}; color: {map_value('#37404c', 'fg')};
}}
{D} input::placeholder, {D} textarea::placeholder {{ color: {map_value('#9aa1ab', 'fg')}; }}

/* While the setting is being flipped, stop per-element colour transitions from animating
   the swap piecemeal (app.js applyTheme() adds this class for two frames). */
html.theme-switching, html.theme-switching *, html.theme-switching *::before, html.theme-switching *::after {{
  transition: none !important;
}}
"""


def check_balanced(text: str) -> None:
    """A stray "(" or "{" makes the browser drop every rule after it, silently. Fail loudly instead."""
    body = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)        # ignore quoted strings (attribute values)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)       # and comments
    if body.count("{") != body.count("}"):
        raise SystemExit("generated CSS has unbalanced braces")
    for line in body.splitlines():
        if line.count("(") != line.count(")"):
            raise SystemExit(f"generated CSS has unbalanced parentheses: {line[:160]!r}")


def main() -> None:
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")
    parts = [
        "/* GENERATED by scripts/build_dark_css.py -- do not edit by hand; edit the script and re-run.\n"
        "   App-wide dark theme, on when <html data-theme=\"dark\"> (Settings > Appearance; see\n"
        "   docs/infra/ui_theme.md). */",
        "\n/* 1. tokens */",
        f"{D} {{\n" + "\n".join(build_tokens(styles)) + "\n}",
    ]
    for name in STYLESHEETS:
        parts.append(f"\n/* 2. {name} */")
        parts += build_stylesheet((STATIC / name).read_text(encoding="utf-8"))
    parts.append("\n/* 3. inline styles (index.html, app.js, families.js, create_po.js) */")
    parts += build_inline()
    parts.append("\n/* 4. manual */" + MANUAL)
    text = "\n".join(parts) + "\n"
    check_balanced(text)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {OUT.stat().st_size // 1024} KB, {sum(p.count('{') for p in parts)} rules")


if __name__ == "__main__":
    main()
