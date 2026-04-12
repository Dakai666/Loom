"""
Memory Palace CSS theme constants.

Purple palette — visually distinct from the parchment chat TUI.
"""

# ── Backgrounds ──────────────────────────────────────────────────────────────

BG = "#0d0a1a"          # Screen background  — deep purple-black
SURFACE = "#150d28"     # Panel surface      — dark purple
RAISED = "#1e1238"      # Cards / raised     — purple-grey
BORDER = "#3d2a6b"      # Borders / dividers — medium purple
BORDER_DIM = "#2a1e50"  # Dim borders

# ── Text ──────────────────────────────────────────────────────────────────────

TEXT = "#e8deff"        # Primary text       — pale lavender
MUTED = "#9b87b5"       # Secondary text     — grey-purple
GOLD = "#d4a853"        # Headings / accent — gold (Loom signature)

# ── Semantic confidence colours ────────────────────────────────────────────────

HIGH = "#a78bfa"        # confidence > 0.7
MID = "#c084fc"         # confidence 0.4–0.7
LOW = "#7c3aed"         # confidence < 0.4
DECAY = "#4c1d95"       # decayed / stale    — very dark purple

# ── Status colours ────────────────────────────────────────────────────────────

SUCCESS = "#86efac"     # green-ish
WARNING = "#fbbf24"     # amber
ERROR = "#f87171"       # rose-red
INFO = "#60a5fa"       # sky blue

# ── Component-specific ─────────────────────────────────────────────────────────

NAV_ACTIVE_BG = "#2a1e50"
NAV_HOVER_BG = "#1e1238"
SELECTED_ITEM = "#3d2a6b"

# ── Full palace CSS (used in PalaceApp.CSS) ───────────────────────────────────

CSS = f"""
Screen {{
    background: {BG};
    color: {TEXT};
}}

/* ── Scrollbar — purple palette ─────────────────────────────────────── */
Screen {{
    scrollbar-background: {BG};
    scrollbar-color: {BORDER};
    scrollbar-color-hover: {HIGH};
    scrollbar-background-hover: {BG};
    scrollbar-corner-color: {BG};
}}

#content-area, #semantic-view, #health-view, #relational-view,
#episodic-view, #skills-view {{
    scrollbar-background: {BG};
    scrollbar-color: {BORDER};
    scrollbar-color-hover: {HIGH};
}}

/* ── Header ──────────────────────────────────────────────────────────── */
#header {{
    dock: top;
    height: 2;
    background: {SURFACE};
    border-bottom: solid {BORDER};
}}

#header-title {{
    width: auto;
    content-align: left middle;
    padding: 0 1;
}}

#header-hints {{
    width: 1fr;
    content-align: right middle;
    padding: 0 1;
}}

/* ── Body layout ─────────────────────────────────────────────────────── */
#body {{
    height: 1fr;
}}

#nav-sidebar {{
    width: 38;
    background: {SURFACE};
    border-right: solid {BORDER};
}}

#nav-item {{
    height: 3;
    padding: 0 1;
    content-align: left middle;
}}

#nav-item:hover {{
    background: {NAV_HOVER_BG};
}}

.nav-active {{
    background: {NAV_ACTIVE_BG};
    color: {HIGH};
}}

.nav-label {{
    color: {MUTED};
}}

/* ── Content area ─────────────────────────────────────────────────────── */
#content-area {{
    width: 1fr;
    background: {BG};
    padding: 1 2;
    overflow-y: auto;
}}

.content-title {{
    color: {GOLD};
}}

.content-subtitle {{
    color: {MUTED};
}}

/* ── Semantic view ───────────────────────────────────────────────────── */
#semantic-view {{
    height: 1fr;
}}

.semantic-entry {{
    height: auto;
    padding: 1 1;
    border-bottom: solid {BORDER_DIM};
}}

.semantic-key {{
    color: {HIGH};
}}

.semantic-value {{
    color: {TEXT};
}}

.semantic-meta {{
    color: {MUTED};
}}

.conf-high {{ color: {HIGH}; }}
.conf-mid  {{ color: {MID};  }}
.conf-low  {{ color: {LOW};  }}
.conf-decay {{ color: {DECAY}; }}

.stat-card {{
    background: {RAISED};
    border: solid {BORDER};
    padding: 1 2;
}}

.stat-value {{
    color: {GOLD};
}}

.stat-label {{
    color: {MUTED};
}}

/* ── Health view — 2×2 grid ─────────────────────────────────────────── */
#health-grid {{
    layout: grid;
    grid-size: 2 2;
    grid-gutter: 1 2;
    height: auto;
}}

.health-card {{
    background: {RAISED};
    border: solid {BORDER};
    padding: 1 2;
}}

.health-card-title {{
    color: {GOLD};
}}

.health-card-value {{
    color: {TEXT};
}}

.health-card-sub {{
    color: {MUTED};
}}

.health-warning {{ color: {WARNING}; }}
.health-good    {{ color: {SUCCESS}; }}
.health-danger  {{ color: {ERROR};   }}

/* ── Relational view ─────────────────────────────────────────────────── */
.rel-subject {{
    color: {HIGH};
    text-style: bold;
}}

.rel-predicate {{
    color: {MID};
}}

.rel-object {{
    color: {TEXT};
}}

/* ── Episodic / Skills views ─────────────────────────────────────────── */
.session-item {{
    height: 3;
    padding: 0 1;
    border-bottom: solid {BORDER_DIM};
}}

.skill-card {{
    background: {RAISED};
    border: solid {BORDER};
    padding: 1 2;
    margin-bottom: 1;
}}

/* ── Status bar ──────────────────────────────────────────────────────── */
#status-bar {{
    dock: bottom;
    height: 1;
    background: {SURFACE};
    border-top: solid {BORDER};
}}

#status-bar Static {{
    color: {MUTED};
}}

/* ── Input / search ──────────────────────────────────────────────────── */
PalaceInput {{
    background: {RAISED};
    color: {TEXT};
    border: solid {BORDER};
}}

PalaceInput:focus {{
    border: solid {HIGH};
}}

/* ── Dividers / decorators ──────────────────────────────────────────── */
.decorator {{
    color: {BORDER};
}}

"""
