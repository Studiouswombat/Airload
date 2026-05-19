"""
dashboard.py  —  AIRLoad A350-1000 Cargo Loading Dashboard  (Enhanced)
=======================================================================

NEW in this version
-------------------
  1. Live cargo-position tracking
       • Polls the loaded plan file every 10 seconds and redraws any
         cargo that has moved zones.  A "LIVE" indicator pulses in the
         status bar so the operator always knows tracking is active.

  2. CG + projected fuel-burn panel
       • Shows the current CG position, target deviation, estimated
         MACZFW (zero-fuel weight) CG, and a simple cruise fuel-burn
         projection that updates the CG step-by-step as fuel is consumed
         symmetrically from wing tanks.

  3. Interactive 2D cargo compartment
       • Clicking / tapping any zone slot opens a floating detail popup
         showing every cargo item in that zone, individual weights,
         cumulative CG after each placement, and a mini weight bar.

Dependencies  (same as before):
  pip install pyyaml
  tkinter  (stdlib)

Usage:
  python dashboard.py
  python dashboard.py --plan  output/optimised_loading_plan.yaml
  python dashboard.py --manifest input_file/my_flight.yaml
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import yaml, os, sys, argparse, math, time, threading

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from airload_person1_core_v2 import (
    A3501000ReferenceModel,
    AIRLoadPhysicsCoreV2,
    OperatingEmptyAircraft,
    CargoZone,
    ULD,
    PassengerCabinZone,
    FuelTank,
)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

CG_FWD  = A3501000ReferenceModel.CG_FORWARD_LIMIT_PERCENT_MAC   # 20.0
CG_AFT  = A3501000ReferenceModel.CG_AFT_LIMIT_PERCENT_MAC       # 35.0
CG_TGT  = A3501000ReferenceModel.CG_TARGET_PERCENT_MAC          # 28.0
LEMAC   = A3501000ReferenceModel.LEMAC_X_M                       # 25.0
MAC_LEN = A3501000ReferenceModel.MAC_LENGTH_M                    # 10.0

ZONE_DEFS  = A3501000ReferenceModel.cargo_zones()
FWD_ZONES  = ["FWD_01", "FWD_02", "FWD_03", "FWD_04"]
AFT_ZONES  = ["AFT_01", "AFT_02", "AFT_03", "AFT_04"]
BULK_ZONES = ["BULK_01"]
ALL_ZONES  = FWD_ZONES + AFT_ZONES + BULK_ZONES

POLL_INTERVAL_MS = 10_000   # 10 s live-update cycle

# Fuel burn model constants (simplified cruise)
FUEL_BURN_KG_PER_STEP  = 1_000   # kg fuel burned per projection step
FUEL_STEPS             = 60       # how many steps to project
WING_TANK_CG_X_M       = 27.5    # approximate wing-tank CG station (m from nose)

# ══════════════════════════════════════════════════════════════════════════════
# COLOUR PALETTE
# ══════════════════════════════════════════════════════════════════════════════

C = {
    "bg":          "#0d1117",
    "panel":       "#161b22",
    "panel2":      "#1c2128",
    "border":      "#30363d",
    "text":        "#e6edf3",
    "muted":       "#8b949e",
    "accent":      "#58a6ff",
    "accent2":     "#f78166",

    "zone_empty":  "#1c2128",
    "zone_border": "#30363d",
    "zone_hover":  "#21262d",

    "green":       "#3fb950",
    "yellow":      "#d29922",
    "red":         "#f85149",
    "purple":      "#bc8cff",

    "cargo_cols": [
        "#1f6feb", "#388bfd", "#58a6ff",
        "#79c0ff", "#a5d6ff", "#cae8ff",
        "#3dd68c", "#56d364", "#7ee787",
        "#ffa657", "#f78166", "#ffb3ae",
    ],

    "fuselage":    "#21262d",
    "wing":        "#1c2128",
    "tail":        "#21262d",
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_plan(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_manifest(path: str) -> dict:
    with open(path) as f:
        manifest = yaml.safe_load(f)
    items = manifest.get("cargo", [])
    loading_plan = [
        {"item": it["name"], "weight_kg": it["weight_kg"],
         "assigned_zone": None, "cg_after_placement": None}
        for it in items
    ]
    return {
        "aircraft":         "Airbus A350-1000",
        "input_manifest":   path,
        "status":           "UNASSIGNED",
        "cg_within_limits": None,
        "base_cg_pct_mac":  None,
        "final_cg_pct_mac": None,
        "cg_fwd_limit":     CG_FWD,
        "cg_aft_limit":     CG_AFT,
        "cg_target":        CG_TGT,
        "total_reward":     None,
        "loading_plan":     loading_plan,
    }


def build_zone_map(plan: dict) -> dict:
    zone_map = {z: [] for z in list(ZONE_DEFS.keys()) + [None]}
    for entry in plan.get("loading_plan", []):
        z = entry.get("assigned_zone")
        if z not in zone_map:
            z = None
        zone_map[z].append(entry)
    return zone_map


def compute_cg_status(cg_pct_mac) -> str:
    if cg_pct_mac is None:
        return "muted"
    if cg_pct_mac < CG_FWD or cg_pct_mac > CG_AFT:
        return "red"
    if abs(cg_pct_mac - CG_TGT) <= 2.0:
        return "green"
    return "yellow"


# ══════════════════════════════════════════════════════════════════════════════
# FUEL-BURN PROJECTION  (Requirement 2)
# ══════════════════════════════════════════════════════════════════════════════

def project_fuel_burn(plan: dict) -> list[dict]:
    """
    Returns a list of dicts, each representing one fuel-burn step:
      { 'fuel_remaining_kg', 'total_weight_kg', 'cg_pct_mac' }

    Uses the same moment-balance approach as the physics core:
      CG_x = Σ(mass_i × x_i) / Σ(mass_i)
    Wing-tank fuel is assumed to burn symmetrically; its CG station
    is fixed at WING_TANK_CG_X_M.
    """
    wb = plan.get("weight_breakdown", {})
    oew      = wb.get("empty_aircraft_kg",  155_000)
    pax      = wb.get("passengers_kg",       26_350)
    fuel_kg  = wb.get("fuel_kg",             65_000)
    cargo_kg = wb.get("cargo_kg",             3_300)

    # OEW moment — use base CG as proxy for non-fuel components
    base_cg  = plan.get("base_cg_pct_mac")
    if base_cg is None:
        base_cg = CG_TGT
    base_cg_x = LEMAC + (base_cg / 100.0) * MAC_LEN

    # Non-fuel moment (OEW + pax + cargo)
    non_fuel_mass = oew + pax + cargo_kg
    non_fuel_moment = non_fuel_mass * base_cg_x

    steps = []
    remaining_fuel = fuel_kg
    for _ in range(FUEL_STEPS + 1):
        total_mass   = non_fuel_mass + remaining_fuel
        total_moment = non_fuel_moment + remaining_fuel * WING_TANK_CG_X_M
        cg_x         = total_moment / total_mass if total_mass > 0 else LEMAC
        cg_pct       = (cg_x - LEMAC) / MAC_LEN * 100.0
        steps.append({
            "fuel_remaining_kg": remaining_fuel,
            "total_weight_kg":   total_mass,
            "cg_pct_mac":        cg_pct,
        })
        remaining_fuel = max(0, remaining_fuel - FUEL_BURN_KG_PER_STEP)
        if remaining_fuel <= 0:
            break

    return steps


# ══════════════════════════════════════════════════════════════════════════════
# ZONE DETAIL POPUP  (Requirement 3)
# ══════════════════════════════════════════════════════════════════════════════

class ZonePopup(tk.Toplevel):
    """Floating popup shown when a cargo zone is clicked."""

    def __init__(self, parent, zone_id: str, zone_items: list,
                 zone_def, item_colours: dict):
        super().__init__(parent)
        self.configure(bg=C["panel2"])
        self.overrideredirect(True)   # frameless window
        self.attributes("-topmost", True)

        self._build(zone_id, zone_items, zone_def, item_colours)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Escape>",   lambda e: self.destroy())

    def _build(self, zone_id, items, zone_def, item_colours):
        # ── Header ─────────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C["accent"], padx=10, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"  Zone: {zone_id}",
                 font=("Courier New", 11, "bold"),
                 fg=C["bg"], bg=C["accent"]).pack(side="left")
        tk.Label(hdr, text="✕",
                 font=("Courier New", 12, "bold"),
                 fg=C["bg"], bg=C["accent"],
                 cursor="hand2").pack(side="right")
        hdr.bind("<Button-1>", lambda e: self.destroy())
        for child in hdr.winfo_children():
            child.bind("<Button-1>", lambda e: self.destroy())

        # ── Zone metadata ──────────────────────────────────────────────────────
        meta = tk.Frame(self, bg=C["panel2"], padx=12, pady=6)
        meta.pack(fill="x")
        x_m = getattr(zone_def, "x_position_m", "—")
        cap = getattr(zone_def, "max_weight_kg", "—")
        tk.Label(meta, text=f"Station: {x_m} m  |  Capacity: {cap:,} kg",
                 font=("Courier New", 8), fg=C["muted"],
                 bg=C["panel2"]).pack(anchor="w")

        sep = tk.Frame(self, bg=C["border"], height=1)
        sep.pack(fill="x", padx=8)

        if not items:
            tk.Label(self, text="  — Empty zone —",
                     font=("Courier New", 9, "italic"),
                     fg=C["muted"], bg=C["panel2"],
                     pady=14).pack()
            return

        # ── Cargo items ────────────────────────────────────────────────────────
        total_w  = sum(it.get("weight_kg", 0) for it in items)
        capacity = getattr(zone_def, "max_weight_kg", total_w) or total_w or 1

        body = tk.Frame(self, bg=C["panel2"], padx=12, pady=8)
        body.pack(fill="x")

        for it in items:
            col  = item_colours.get(it["item"], C["accent"])
            wt   = it.get("weight_kg", 0)
            cg_a = it.get("cg_after_placement")

            row = tk.Frame(body, bg=C["panel2"], pady=3)
            row.pack(fill="x")

            # colour swatch
            tk.Label(row, text="▐ ", fg=col, bg=C["panel2"],
                     font=("Courier New", 12)).pack(side="left")

            info = tk.Frame(row, bg=C["panel2"])
            info.pack(side="left", fill="x", expand=True)

            tk.Label(info, text=it["item"],
                     font=("Courier New", 9, "bold"),
                     fg=C["text"], bg=C["panel2"],
                     anchor="w").pack(fill="x")
            tk.Label(info, text=f"{wt:,.1f} kg",
                     font=("Courier New", 8),
                     fg=C["muted"], bg=C["panel2"],
                     anchor="w").pack(fill="x")
            if cg_a is not None:
                s_col = C[compute_cg_status(cg_a)]
                tk.Label(info, text=f"CG after placement: {cg_a:.3f} % MAC",
                         font=("Courier New", 8),
                         fg=s_col, bg=C["panel2"],
                         anchor="w").pack(fill="x")

            # weight mini-bar
            bar_frame = tk.Frame(body, bg=C["panel2"])
            bar_frame.pack(fill="x", pady=(0, 2))
            tk.Label(bar_frame, text="Load ", font=("Courier New", 7),
                     fg=C["muted"], bg=C["panel2"]).pack(side="left")

            bar_outer = tk.Frame(bar_frame, bg=C["border"],
                                 height=6, width=160)
            bar_outer.pack(side="left")
            bar_outer.pack_propagate(False)
            fill_pct = min(1.0, wt / capacity)
            bar_col  = C["green"] if fill_pct < 0.8 else C["yellow"] if fill_pct < 1.0 else C["red"]
            tk.Frame(bar_outer, bg=bar_col,
                     height=6, width=int(160 * fill_pct)).pack(side="left")

            tk.Label(bar_frame, text=f" {fill_pct*100:.0f}%",
                     font=("Courier New", 7),
                     fg=C["muted"], bg=C["panel2"]).pack(side="left")

            sep2 = tk.Frame(body, bg=C["border"], height=1)
            sep2.pack(fill="x")

        # ── Zone total ─────────────────────────────────────────────────────────
        ft = tk.Frame(self, bg=C["panel"], padx=12, pady=6)
        ft.pack(fill="x")
        util_pct = min(100.0, total_w / capacity * 100) if capacity else 0
        util_col = C["green"] if util_pct < 80 else C["yellow"] if util_pct < 100 else C["red"]
        tk.Label(ft, text=f"Total: {total_w:,.1f} kg  /  {capacity:,.0f} kg  ({util_pct:.0f}% utilisation)",
                 font=("Courier New", 8, "bold"),
                 fg=util_col, bg=C["panel"]).pack(anchor="w")

    def _on_click(self, event):
        # close if clicking outside the popup
        pass

    def place_near(self, x, y, screen_w, screen_h):
        self.update_idletasks()
        pw = self.winfo_reqwidth()
        ph = self.winfo_reqheight()
        # nudge to stay on screen
        if x + pw + 20 > screen_w:
            x = max(0, x - pw - 10)
        else:
            x += 16
        if y + ph + 20 > screen_h:
            y = max(0, y - ph - 10)
        else:
            y += 16
        self.geometry(f"+{x}+{y}")


# ══════════════════════════════════════════════════════════════════════════════
# FUEL-BURN CHART WINDOW  (Requirement 2 — detail view)
# ══════════════════════════════════════════════════════════════════════════════

class FuelBurnWindow(tk.Toplevel):
    """Popup showing CG-vs-fuel step chart."""

    def __init__(self, parent, steps: list[dict]):
        super().__init__(parent)
        self.title("CG Projection — Fuel Burn")
        self.configure(bg=C["bg"])
        self.geometry("640x340")
        self.resizable(True, True)
        self._steps = steps
        self._build()

    def _build(self):
        tk.Label(self, text="CG Migration During Fuel Burn",
                 font=("Courier New", 11, "bold"),
                 fg=C["accent"], bg=C["bg"]).pack(pady=(10, 0))
        tk.Label(self, text="Each point = 1,000 kg fuel consumed",
                 font=("Courier New", 8),
                 fg=C["muted"], bg=C["bg"]).pack()

        self.chart = tk.Canvas(self, bg=C["panel"],
                               highlightthickness=0)
        self.chart.pack(fill="both", expand=True, padx=14, pady=10)
        self.chart.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _draw(self):
        c = self.chart
        c.delete("all")
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 10 or H < 10:
            return

        pad_l, pad_r, pad_t, pad_b = 60, 20, 20, 40
        plot_w = W - pad_l - pad_r
        plot_h = H - pad_t - pad_b

        steps  = self._steps
        n      = len(steps)
        if n < 2:
            return

        fuels  = [s["fuel_remaining_kg"] for s in steps]
        cgs    = [s["cg_pct_mac"]         for s in steps]
        weights= [s["total_weight_kg"]     for s in steps]

        max_f  = fuels[0]
        min_cg = min(cgs) - 0.5
        max_cg = max(cgs) + 0.5

        def px(fuel):
            return pad_l + (1 - fuel / max_f) * plot_w if max_f > 0 else pad_l

        def py(cg_pct):
            return pad_t + plot_h - (cg_pct - min_cg) / (max_cg - min_cg) * plot_h

        # CG limit bands
        def cy_lim(pct):
            return pad_t + plot_h - (pct - min_cg) / (max_cg - min_cg) * plot_h

        # Yellow band
        y_fwd = cy_lim(CG_FWD)
        y_aft = cy_lim(CG_AFT)
        y_tgt1 = cy_lim(CG_TGT - 2)
        y_tgt2 = cy_lim(CG_TGT + 2)

        c.create_rectangle(pad_l, max(pad_t, y_aft), pad_l + plot_w,
                           min(pad_t + plot_h, y_fwd),
                           fill="#2a2a1a", outline="")
        c.create_rectangle(pad_l, max(pad_t, y_tgt2), pad_l + plot_w,
                           min(pad_t + plot_h, y_tgt1),
                           fill="#1a2a1a", outline="")

        # Dashed limit lines
        for lim_pct, col, lbl in [
            (CG_FWD, C["red"],    f"FWD {CG_FWD:.0f}%"),
            (CG_AFT, C["red"],    f"AFT {CG_AFT:.0f}%"),
            (CG_TGT, C["green"],  f"TGT {CG_TGT:.0f}%"),
        ]:
            if min_cg <= lim_pct <= max_cg:
                yy = cy_lim(lim_pct)
                c.create_line(pad_l, yy, pad_l + plot_w, yy,
                              fill=col, dash=(4, 3), width=1)
                c.create_text(pad_l - 4, yy, text=lbl,
                              font=("Courier New", 6), fill=col, anchor="e")

        # Axes
        c.create_line(pad_l, pad_t, pad_l, pad_t + plot_h,
                      fill=C["border"], width=1)
        c.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h,
                      fill=C["border"], width=1)

        # X-axis ticks (fuel)
        for tick_f in range(0, int(max_f) + 1, 10_000):
            tx = px(tick_f)
            c.create_line(tx, pad_t + plot_h, tx, pad_t + plot_h + 4,
                          fill=C["muted"], width=1)
            c.create_text(tx, pad_t + plot_h + 12,
                          text=f"{tick_f//1000}k",
                          font=("Courier New", 6), fill=C["muted"])
        c.create_text(pad_l + plot_w / 2, H - 6,
                      text="Fuel Remaining (kg)",
                      font=("Courier New", 7), fill=C["muted"])

        # CG trace
        pts = [(px(fuels[i]), py(cgs[i])) for i in range(n)]
        for i in range(len(pts) - 1):
            col = C[compute_cg_status(cgs[i])]
            c.create_line(pts[i][0], pts[i][1],
                          pts[i+1][0], pts[i+1][1],
                          fill=col, width=2)

        # Start / end markers
        sx, sy = pts[0]
        ex, ey = pts[-1]
        c.create_oval(sx-5, sy-5, sx+5, sy+5,
                      fill=C["accent"], outline="")
        c.create_text(sx, sy - 12,
                      text=f"TOW\n{weights[0]/1000:.0f}t",
                      font=("Courier New", 6), fill=C["accent"])
        c.create_oval(ex-5, ey-5, ex+5, ey+5,
                      fill=C["accent2"], outline="")
        c.create_text(ex, ey - 12,
                      text=f"LDW\n{weights[-1]/1000:.0f}t",
                      font=("Courier New", 6), fill=C["accent2"])

        # Y-axis label
        c.create_text(10, pad_t + plot_h / 2,
                      text="CG % MAC",
                      font=("Courier New", 7), fill=C["muted"],
                      angle=90)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD  (Enhanced)
# ══════════════════════════════════════════════════════════════════════════════

class AIRLoadDashboard(tk.Tk):

    def __init__(self, plan: dict, source_path: str = None):
        super().__init__()
        self.plan        = plan
        self.zone_map    = build_zone_map(plan)
        self.source_path = source_path   # file to poll for live updates
        self._item_colours: dict[str, str] = {}
        self._assign_colours()

        # live-update state
        self._live_active  = source_path is not None
        self._live_blink   = False
        self._popup: ZonePopup | None = None

        # zone-hit-test registry  { zone_id: (x1,y1,x2,y2) }
        self._zone_rects: dict[str, tuple] = {}

        self.title("AIRLoad  ·  A350-1000 Cargo Loading Dashboard")
        self.configure(bg=C["bg"])
        self.resizable(True, True)
        self.minsize(1100, 760)

        self._build_ui()
        self._draw_all()

        if self._live_active:
            self._schedule_poll()
            self._blink_live()

    # ── colour assignment ──────────────────────────────────────────────────────

    def _assign_colours(self):
        palette = C["cargo_cols"]
        for i, entry in enumerate(self.plan.get("loading_plan", [])):
            self._item_colours[entry["item"]] = palette[i % len(palette)]

    # ── UI skeleton ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # top bar
        top = tk.Frame(self, bg=C["panel"], pady=10, padx=18)
        top.pack(fill="x", side="top")

        tk.Label(top, text="✈  AIRLoad",
                 font=("Courier New", 18, "bold"),
                 fg=C["accent"], bg=C["panel"]).pack(side="left")
        tk.Label(top, text="  A350-1000 Cargo Loading Plan",
                 font=("Courier New", 13), fg=C["muted"],
                 bg=C["panel"]).pack(side="left")

        # LIVE badge
        self._live_label = tk.Label(top, text="⬤ LIVE",
                                    font=("Courier New", 9, "bold"),
                                    fg=C["green"], bg=C["panel"],
                                    padx=8)
        if self._live_active:
            self._live_label.pack(side="left", padx=16)

        btn_frame = tk.Frame(top, bg=C["panel"])
        btn_frame.pack(side="right")
        tk.Button(btn_frame, text="Open Plan…",     command=self._open_plan,
                  bg=C["border"], fg=C["text"], relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_frame, text="Open Manifest…", command=self._open_manifest,
                  bg=C["border"], fg=C["text"], relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_frame, text="⛽ Fuel Projection…",
                  command=self._open_fuel_window,
                  bg=C["panel2"], fg=C["accent"], relief="flat",
                  padx=10, pady=4, cursor="hand2").pack(side="left", padx=4)

        # status bar
        self.status_bar = tk.Frame(self, bg=C["panel"], pady=8, padx=18)
        self.status_bar.pack(fill="x", side="top")
        self._build_status_bar()

        # ── CG + Weight info row (Requirement 2) ──────────────────────────────
        self.cg_info_bar = tk.Frame(self, bg=C["panel2"], pady=6, padx=18)
        self.cg_info_bar.pack(fill="x", side="top")
        self._build_cg_info_bar()

        # main body
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=8)

        canvas_frame = tk.Frame(body, bg=C["bg"])
        canvas_frame.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, bg=C["bg"],
                                highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw_all())
        self.canvas.bind("<Button-1>",  self._on_canvas_click)

        sidebar = tk.Frame(body, bg=C["panel"], width=270,
                           relief="flat", bd=0)
        sidebar.pack(side="right", fill="y", padx=(10, 0))
        sidebar.pack_propagate(False)
        self._sidebar_parent = sidebar
        self._build_sidebar(sidebar)

        # gauge
        gauge_frame = tk.Frame(self, bg=C["panel"], pady=10, padx=18)
        gauge_frame.pack(fill="x", side="bottom")
        self._build_cg_gauge(gauge_frame)

    # ── status bar ─────────────────────────────────────────────────────────────

    def _build_status_bar(self):
        for w in self.status_bar.winfo_children():
            w.destroy()
        plan     = self.plan
        status   = plan.get("status", "—")
        within   = plan.get("cg_within_limits")
        final_cg = plan.get("final_cg_pct_mac")
        cg_col   = C[compute_cg_status(final_cg)]

        pill_col = C["green"] if within else C["red"] if within is False else C["muted"]
        tk.Label(self.status_bar, text=f"  {status}  ",
                 font=("Courier New", 10, "bold"),
                 fg=C["bg"], bg=pill_col, padx=6, pady=2).pack(side="left")

        def _kv(label, val, col=C["text"]):
            f = tk.Frame(self.status_bar, bg=C["panel"])
            f.pack(side="left", padx=18)
            tk.Label(f, text=label, font=("Courier New", 9),
                     fg=C["muted"], bg=C["panel"]).pack(anchor="w")
            tk.Label(f, text=val, font=("Courier New", 11, "bold"),
                     fg=col, bg=C["panel"]).pack(anchor="w")

        _kv("Final CG",
            f"{final_cg:.3f} % MAC" if final_cg is not None else "—",
            cg_col)
        base_cg = plan.get("base_cg_pct_mac")
        _kv("Base CG",     f"{base_cg:.3f} % MAC" if base_cg else "—")
        _kv("CG Fwd Limit", f"{CG_FWD:.1f} % MAC")
        _kv("CG Aft Limit", f"{CG_AFT:.1f} % MAC")
        _kv("CG Target",    f"{CG_TGT:.1f} % MAC")
        reward = plan.get("total_reward")
        _kv("Reward", f"{reward:.2f}" if reward else "—", C["accent"])
        src = os.path.basename(plan.get("input_manifest", ""))
        _kv("Manifest", src or "—", C["muted"])

    # ── CG + weight info bar (Requirement 2) ──────────────────────────────────

    def _build_cg_info_bar(self):
        for w in self.cg_info_bar.winfo_children():
            w.destroy()

        plan     = self.plan
        final_cg = plan.get("final_cg_pct_mac")
        wb       = plan.get("weight_breakdown", {})

        def _tile(label, val, col=C["text"], bg=C["panel2"]):
            f = tk.Frame(self.cg_info_bar, bg=bg, padx=12, pady=4,
                         relief="flat")
            f.pack(side="left", padx=4)
            tk.Label(f, text=label, font=("Courier New", 7),
                     fg=C["muted"], bg=bg).pack(anchor="w")
            tk.Label(f, text=val, font=("Courier New", 10, "bold"),
                     fg=col, bg=bg).pack(anchor="w")

        # CG deviation from target
        if final_cg is not None:
            dev     = final_cg - CG_TGT
            dev_col = C[compute_cg_status(final_cg)]
            dev_str = f"{dev:+.3f} % vs TGT"
            _tile("CG Deviation", dev_str, dev_col)

        # fuel projection summary
        steps = project_fuel_burn(plan)
        if steps:
            cg_start = steps[0]["cg_pct_mac"]
            cg_end   = steps[-1]["cg_pct_mac"]
            fuel_used = steps[0]["fuel_remaining_kg"] - steps[-1]["fuel_remaining_kg"]
            _tile("CG @ TOW",  f"{cg_start:.2f} % MAC", C["accent"])
            _tile("CG @ LDW",  f"{cg_end:.2f} % MAC",   C["accent2"])
            _tile("Projected Fuel Burn", f"{fuel_used:,.0f} kg", C["muted"])

        # weight summary
        oew  = wb.get("empty_aircraft_kg")
        pax  = wb.get("passengers_kg")
        fuel = wb.get("fuel_kg")
        cgo  = wb.get("cargo_kg")
        if all(v is not None for v in [oew, pax, fuel, cgo]):
            mtow = oew + pax + fuel + cgo
            _tile("MTOW",            f"{mtow:,.0f} kg", C["text"])
            _tile("ZFW",             f"{oew+pax+cgo:,.0f} kg", C["text"])
            _tile("Fuel on Board",   f"{fuel:,.0f} kg",  C["muted"])

        # Last update timestamp
        ts = time.strftime("%H:%M:%S")
        self._ts_label = tk.Label(self.cg_info_bar,
                                  text=f"Updated: {ts}",
                                  font=("Courier New", 7),
                                  fg=C["border"], bg=C["panel2"])
        self._ts_label.pack(side="right", padx=10)

    # ── sidebar ────────────────────────────────────────────────────────────────

    def _build_sidebar(self, parent):
        tk.Label(parent, text="CARGO MANIFEST",
                 font=("Courier New", 10, "bold"),
                 fg=C["accent"], bg=C["panel"],
                 pady=10).pack(fill="x", padx=12)
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=8)

        scroll_frame = tk.Frame(parent, bg=C["panel"])
        scroll_frame.pack(fill="both", expand=True)
        vsb = tk.Scrollbar(scroll_frame, orient="vertical")
        vsb.pack(side="right", fill="y")
        lbc = tk.Canvas(scroll_frame, bg=C["panel"],
                        highlightthickness=0, yscrollcommand=vsb.set)
        lbc.pack(side="left", fill="both", expand=True)
        vsb.config(command=lbc.yview)
        inner = tk.Frame(lbc, bg=C["panel"])
        win_id = lbc.create_window((0, 0), window=inner, anchor="nw")
        lbc.bind("<Configure>", lambda e: lbc.itemconfig(win_id, width=e.width))
        inner.bind("<Configure>",
                   lambda e: lbc.configure(scrollregion=lbc.bbox("all")))

        total_kg = 0
        for entry in self.plan.get("loading_plan", []):
            col  = self._item_colours.get(entry["item"], C["accent"])
            zone = entry.get("assigned_zone") or "—"
            wt   = entry.get("weight_kg", 0)
            total_kg += wt

            row = tk.Frame(inner, bg=C["panel"], pady=4, padx=10)
            row.pack(fill="x")
            tk.Label(row, text="●", fg=col, bg=C["panel"],
                     font=("Courier New", 10)).pack(side="left")
            info = tk.Frame(row, bg=C["panel"])
            info.pack(side="left", padx=6)
            tk.Label(info, text=entry["item"],
                     font=("Courier New", 9, "bold"),
                     fg=C["text"], bg=C["panel"], anchor="w").pack(fill="x")
            tk.Label(info, text=f"{wt:.1f} kg  →  {zone}",
                     font=("Courier New", 8),
                     fg=C["muted"], bg=C["panel"], anchor="w").pack(fill="x")
            cg_a = entry.get("cg_after_placement")
            if cg_a is not None:
                sc = C[compute_cg_status(cg_a)]
                tk.Label(info, text=f"CG after: {cg_a:.3f} % MAC",
                         font=("Courier New", 8),
                         fg=sc, bg=C["panel"], anchor="w").pack(fill="x")
            tk.Frame(inner, bg=C["border"], height=1).pack(fill="x", padx=10)

        tk.Label(parent, text=f"Total cargo: {total_kg:,.1f} kg",
                 font=("Courier New", 9, "bold"),
                 fg=C["muted"], bg=C["panel"], pady=8).pack()

        wt_brk = self.plan.get("weight_breakdown")
        if wt_brk:
            tk.Label(parent, text="WEIGHT BREAKDOWN",
                     font=("Courier New", 9, "bold"),
                     fg=C["accent"], bg=C["panel"]).pack()
            for key, lbl in [
                ("empty_aircraft_kg", "Empty Aircraft"),
                ("passengers_kg",     "Passengers"),
                ("fuel_kg",           "Fuel"),
                ("cargo_kg",          "Cargo"),
            ]:
                val = wt_brk.get(key)
                if val is not None:
                    tk.Label(parent,
                             text=f"{lbl:<18} {val:>9,.0f} kg",
                             font=("Courier New", 8),
                             fg=C["muted"], bg=C["panel"]).pack(anchor="w",
                                                                 padx=12)

    # ── CG gauge bar ───────────────────────────────────────────────────────────

    def _build_cg_gauge(self, parent):
        self.gauge_canvas = tk.Canvas(parent, bg=C["panel"],
                                      height=54, highlightthickness=0)
        self.gauge_canvas.pack(fill="x")
        self.gauge_canvas.bind("<Configure>", lambda e: self._draw_gauge())
        self._draw_gauge()

    # ── DRAWING ────────────────────────────────────────────────────────────────

    def _draw_all(self):
        self._draw_aircraft()

    def _draw_aircraft(self):
        c = self.canvas
        c.delete("all")
        self._zone_rects.clear()

        W = c.winfo_width()
        H = c.winfo_height()
        if W < 10 or H < 10:
            return

        X_NOSE, X_TAIL = 3.0, 52.0
        mx, my = 40, 30
        usable_w = W - 2 * mx

        def ac_x(x_m):
            return mx + (x_m - X_NOSE) / (X_TAIL - X_NOSE) * usable_w

        cy = H / 2

        def fus_half_width(x_m):
            rel = (x_m - X_NOSE) / (X_TAIL - X_NOSE)
            if rel < 0.05:
                return rel / 0.05 * 22
            elif rel > 0.85:
                return max(3, 22 * (1 - (rel - 0.85) / 0.15))
            return 22

        # Fuselage
        fus_pts = []
        for i in range(81):
            x_m = X_NOSE + i / 80 * (X_TAIL - X_NOSE)
            fus_pts.append((ac_x(x_m), cy - fus_half_width(x_m)))
        for i in range(81):
            x_m = X_TAIL - i / 80 * (X_TAIL - X_NOSE)
            fus_pts.append((ac_x(x_m), cy + fus_half_width(x_m)))
        c.create_polygon([v for pt in fus_pts for v in pt],
                         fill=C["fuselage"], outline=C["border"], width=1.5)

        # Wings
        usable_h = H - 2 * my
        wx1 = ac_x(22); wx2 = ac_x(33)
        wing_tip_y = min(usable_h * 0.44, 190)
        for sign in (-1, 1):
            c.create_polygon(
                wx1, cy + sign * 22,
                wx2, cy + sign * 22,
                wx2 + 30, cy + sign * wing_tip_y,
                wx1 - 15, cy + sign * wing_tip_y,
                fill=C["wing"], outline=C["border"], width=1)

        # Stabilisers
        sx1 = ac_x(44); sx2 = ac_x(49)
        stab_tip = min(usable_h * 0.18, 80)
        for sign in (-1, 1):
            c.create_polygon(
                sx1, cy + sign * 10,
                sx2, cy + sign * 10,
                sx2 + 12, cy + sign * stab_tip,
                sx1, cy + sign * stab_tip,
                fill=C["tail"], outline=C["border"], width=1)

        c.create_line(ac_x(X_NOSE), cy, ac_x(X_TAIL), cy,
                      fill=C["border"], dash=(6, 6), width=1)

        # ── Zone slots ─────────────────────────────────────────────────────────
        zone_h = 38

        def draw_zone_slot(zone_id, row):
            zdef   = ZONE_DEFS[zone_id]
            x_m    = zdef.x_position_m
            cxc    = ac_x(x_m)
            slot_w = max(30, usable_w / (X_TAIL - X_NOSE) * 1.8)

            x1 = cxc - slot_w / 2
            x2 = cxc + slot_w / 2
            y1 = cy + row * 28
            y2 = y1 + row * zone_h
            if y1 > y2:
                y1, y2 = y2, y1

            # store hit-test rect
            self._zone_rects[zone_id] = (x1, y1, x2, y2)

            items_here = self.zone_map.get(zone_id, [])
            occupied   = len(items_here) > 0
            fill_col   = (self._item_colours.get(items_here[0]["item"], C["accent"])
                          if occupied else C["zone_empty"])

            # draw slot body (rounded-rect simulation)
            r = 6
            c.create_rectangle(x1+r, y1, x2-r, y2, fill=fill_col, outline="")
            c.create_rectangle(x1, y1+r, x2, y2-r, fill=fill_col, outline="")
            for cx_c, cy_c in [(x1+r, y1+r), (x2-r, y1+r),
                                (x1+r, y2-r), (x2-r, y2-r)]:
                c.create_oval(cx_c-r, cy_c-r, cx_c+r, cy_c+r,
                              fill=fill_col, outline="")
            c.create_rectangle(x1, y1, x2, y2,
                                outline=C["border"] if not occupied else fill_col,
                                width=1, fill="")

            # text
            c.create_text(cxc, (y1+y2)/2 - (6 if occupied else 0),
                          text=zone_id,
                          font=("Courier New", 7, "bold"),
                          fill=C["bg"] if occupied else C["muted"])

            if occupied:
                item  = items_here[0]
                short = item["item"][:10] + ("…" if len(item["item"]) > 10 else "")
                c.create_text(cxc, (y1+y2)/2 + 7,
                              text=short, font=("Courier New", 6), fill=C["bg"])
                c.create_text(cxc, (y1+y2)/2 + 17,
                              text=f"{item['weight_kg']:.0f}kg",
                              font=("Courier New", 6), fill=C["bg"])
                cg_a = item.get("cg_after_placement")
                if cg_a is not None:
                    dot_col = C[compute_cg_status(cg_a)]
                    c.create_oval(x2-10, y1+2, x2-2, y1+10,
                                  fill=dot_col, outline="")

            # hover tooltip hint
            c.create_text(cxc, y2 + 3,
                          text="▲" if row > 0 else "▼",
                          font=("Courier New", 6),
                          fill=C["border"])

            c.create_line(cxc, cy, cxc, y1 if row > 0 else y2,
                          fill=C["border"], dash=(3, 4), width=1)

        for z in FWD_ZONES:
            draw_zone_slot(z, row=1)
        for z in AFT_ZONES:
            draw_zone_slot(z, row=1)
        for z in BULK_ZONES:
            draw_zone_slot(z, row=-1)

        # hold labels
        fwd_cx  = (ac_x(15) + ac_x(21)) / 2
        aft_cx  = (ac_x(31) + ac_x(37)) / 2
        bulk_cx = ac_x(40)
        label_y = cy + zone_h + 48
        for lbl, lx in [("◀  FORWARD HOLD", fwd_cx),
                         ("AFT HOLD  ▶",    aft_cx),
                         ("BULK",            bulk_cx)]:
            c.create_text(lx, label_y, text=lbl,
                          font=("Courier New", 9, "bold"), fill=C["muted"])

        # CG position
        final_cg = self.plan.get("final_cg_pct_mac")
        if final_cg is not None:
            cg_x_m = LEMAC + (final_cg / 100) * MAC_LEN
            cg_px  = ac_x(cg_x_m)
            col    = C[compute_cg_status(final_cg)]
            c.create_line(cg_px, cy - 24, cg_px, cy + 24, fill=col, width=2)
            d = 7
            c.create_polygon(cg_px, cy - 24 - d,
                             cg_px + d, cy - 24,
                             cg_px, cy - 24 + d,
                             fill=col, outline="")
            c.create_text(cg_px, cy - 24 - d - 10,
                          text=f"CG {final_cg:.2f}%",
                          font=("Courier New", 8, "bold"), fill=col)

        # Target CG tick
        tgt_x_m = LEMAC + (CG_TGT / 100) * MAC_LEN
        tgt_px  = ac_x(tgt_x_m)
        c.create_line(tgt_px, cy-15, tgt_px, cy+15,
                      fill=C["muted"], width=1, dash=(4, 3))
        c.create_text(tgt_px, cy - 22,
                      text=f"TGT {CG_TGT:.0f}%",
                      font=("Courier New", 7), fill=C["muted"])

    def _draw_gauge(self):
        gc = self.gauge_canvas
        gc.delete("all")
        W = gc.winfo_width()
        if W < 10:
            return

        bar_h, bar_y, pad = 14, 28, 60
        bar_w = W - 2 * pad
        total_range = CG_AFT - CG_FWD

        def cg_to_x(pct):
            return pad + (pct - CG_FWD) / total_range * bar_w

        gc.create_rectangle(pad, bar_y, pad+bar_w, bar_y+bar_h,
                            fill=C["zone_empty"], outline=C["border"])

        gx1 = cg_to_x(CG_TGT - 2); gx2 = cg_to_x(CG_TGT + 2)
        gc.create_rectangle(max(pad, gx1), bar_y,
                            min(pad+bar_w, gx2), bar_y+bar_h,
                            fill=C["green"], outline="")
        gc.create_rectangle(pad, bar_y, max(pad, gx1), bar_y+bar_h,
                            fill=C["yellow"], outline="")
        gc.create_rectangle(min(pad+bar_w, gx2), bar_y,
                            pad+bar_w, bar_y+bar_h,
                            fill=C["yellow"], outline="")
        gc.create_text(pad-4, bar_y+bar_h/2,
                       text=f"◀ {CG_FWD:.0f}%",
                       font=("Courier New", 8), fill=C["red"], anchor="e")
        gc.create_text(pad+bar_w+4, bar_y+bar_h/2,
                       text=f"{CG_AFT:.0f}% ▶",
                       font=("Courier New", 8), fill=C["red"], anchor="w")

        tx = cg_to_x(CG_TGT)
        gc.create_line(tx, bar_y-3, tx, bar_y+bar_h+3,
                       fill=C["bg"], width=2, dash=(3, 2))
        gc.create_text(tx, bar_y-8, text=f"TGT {CG_TGT:.0f}%",
                       font=("Courier New", 7), fill=C["muted"])

        final_cg = self.plan.get("final_cg_pct_mac")
        if final_cg is not None:
            col = C[compute_cg_status(final_cg)]
            cgx = max(pad, min(pad+bar_w, cg_to_x(final_cg)))
            d   = 7
            gc.create_polygon(cgx, bar_y-2,
                              cgx-d, bar_y-2-d*1.5,
                              cgx+d, bar_y-2-d*1.5,
                              fill=col, outline="")
            gc.create_line(cgx, bar_y-2, cgx, bar_y+bar_h,
                           fill=col, width=2)
            gc.create_text(cgx, bar_y+bar_h+10,
                           text=f"{final_cg:.3f} % MAC",
                           font=("Courier New", 8, "bold"), fill=col)

        gc.create_text(W/2, 8, text="CG ENVELOPE  —  % MAC",
                       font=("Courier New", 8), fill=C["muted"])

    # ── Interactive zone click  (Requirement 3) ────────────────────────────────

    def _on_canvas_click(self, event):
        x, y = event.x, event.y
        for zone_id, (x1, y1, x2, y2) in self._zone_rects.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                self._show_zone_popup(zone_id, event.x_root, event.y_root)
                return
        # clicked outside all zones — close any open popup
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
            self._popup = None

    def _show_zone_popup(self, zone_id: str, sx: int, sy: int):
        if self._popup and self._popup.winfo_exists():
            self._popup.destroy()
        items    = self.zone_map.get(zone_id, [])
        zone_def = ZONE_DEFS.get(zone_id)
        self._popup = ZonePopup(self, zone_id, items,
                                zone_def, self._item_colours)
        self._popup.place_near(sx, sy,
                               self.winfo_screenwidth(),
                               self.winfo_screenheight())

    # ── Fuel projection window ─────────────────────────────────────────────────

    def _open_fuel_window(self):
        steps = project_fuel_burn(self.plan)
        FuelBurnWindow(self, steps)

    # ── Live update  (Requirement 1) ──────────────────────────────────────────

    def _schedule_poll(self):
        """Schedule a file-poll in POLL_INTERVAL_MS."""
        self._poll_job = self.after(POLL_INTERVAL_MS, self._poll_source)

    def _poll_source(self):
        if not self.source_path or not os.path.exists(self.source_path):
            self._schedule_poll()
            return
        try:
            if "loading_plan" in open(self.source_path).read():
                new_plan = load_plan(self.source_path)
            else:
                new_plan = load_manifest(self.source_path)

            # detect changes
            old_assignments = {
                e["item"]: e.get("assigned_zone")
                for e in self.plan.get("loading_plan", [])
            }
            new_assignments = {
                e["item"]: e.get("assigned_zone")
                for e in new_plan.get("loading_plan", [])
            }
            moved = [item for item, zone in new_assignments.items()
                     if old_assignments.get(item) != zone]

            self.plan     = new_plan
            self.zone_map = build_zone_map(new_plan)
            self._assign_colours()
            self._build_status_bar()
            self._build_cg_info_bar()
            self._draw_all()
            self._draw_gauge()
            self._rebuild_sidebar()

            if moved:
                self._flash_moved(moved)

        except Exception:
            pass   # silently retry next cycle

        self._schedule_poll()

    def _flash_moved(self, items: list[str]):
        """Brief visual notification when cargo moves zones."""
        msg = f"⟳ Updated: {', '.join(items[:3])}{'…' if len(items) > 3 else ''}"
        flash = tk.Label(self, text=msg,
                         font=("Courier New", 9, "bold"),
                         fg=C["bg"], bg=C["yellow"],
                         padx=10, pady=4)
        flash.place(relx=0.5, rely=0.06, anchor="center")
        self.after(2500, flash.destroy)

    def _blink_live(self):
        """Pulse the LIVE indicator every second."""
        if not self._live_active:
            return
        self._live_blink = not self._live_blink
        self._live_label.config(fg=C["green"] if self._live_blink else C["border"])
        self.after(1000, self._blink_live)

    # ── File open actions ──────────────────────────────────────────────────────

    def _open_plan(self):
        path = filedialog.askopenfilename(
            title="Open Optimised Loading Plan",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")])
        if path:
            try:
                self.source_path = path
                self.plan        = load_plan(path)
                self.zone_map    = build_zone_map(self.plan)
                self._item_colours = {}
                self._assign_colours()
                self._build_status_bar()
                self._build_cg_info_bar()
                self._rebuild_sidebar()
                self._draw_all()
                self._draw_gauge()
                if not self._live_active:
                    self._live_active = True
                    self._live_label.pack(side="left", padx=16)
                    self._schedule_poll()
                    self._blink_live()
            except Exception as ex:
                messagebox.showerror("Load Error", str(ex))

    def _open_manifest(self):
        path = filedialog.askopenfilename(
            title="Open Cargo Manifest",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")])
        if path:
            try:
                self.source_path = path
                self.plan        = load_manifest(path)
                self.zone_map    = build_zone_map(self.plan)
                self._item_colours = {}
                self._assign_colours()
                self._build_status_bar()
                self._build_cg_info_bar()
                self._rebuild_sidebar()
                self._draw_all()
                self._draw_gauge()
                if not self._live_active:
                    self._live_active = True
                    self._live_label.pack(side="left", padx=16)
                    self._schedule_poll()
                    self._blink_live()
            except Exception as ex:
                messagebox.showerror("Load Error", str(ex))

    def _rebuild_sidebar(self):
        for w in self._sidebar_parent.winfo_children():
            w.destroy()
        self._build_sidebar(self._sidebar_parent)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="AIRLoad A350-1000 Cargo Loading Dashboard")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan",     type=str, default=None)
    group.add_argument("--manifest", type=str, default=None)
    args = parser.parse_args()

    source_path = None
    if args.plan:
        plan        = load_plan(args.plan)
        source_path = args.plan
    elif args.manifest:
        plan        = load_manifest(args.manifest)
        source_path = args.manifest
    else:
        candidates = [
            "output/optimised_loading_plan.yaml",
            "optimised_loading_plan.yaml",
            "input_file/my_flight.yaml",
            "my_flight.yaml",
        ]
        plan = None
        for cand in candidates:
            if os.path.exists(cand):
                try:
                    if "loading_plan" in open(cand).read():
                        plan = load_plan(cand)
                    else:
                        plan = load_manifest(cand)
                    source_path = cand
                    print(f"Auto-loaded: {cand}")
                    break
                except Exception:
                    continue

        if plan is None:
            plan = {
                "aircraft":         "Airbus A350-1000",
                "input_manifest":   "—",
                "status":           "NO FILE LOADED",
                "cg_within_limits": None,
                "base_cg_pct_mac":  None,
                "final_cg_pct_mac": None,
                "cg_fwd_limit":     CG_FWD,
                "cg_aft_limit":     CG_AFT,
                "cg_target":        CG_TGT,
                "total_reward":     None,
                "loading_plan":     [],
            }
            print("No default file found — use File > Open Plan… or --plan / --manifest flags.")

    app = AIRLoadDashboard(plan, source_path=source_path)
    app.mainloop()


if __name__ == "__main__":
    main()