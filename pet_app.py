"""Pet Farm Helper: a desktop app around pet_sync.

Runs pet sync inside it (keeps the Numpad 8 Pet Farm list in chant2.ini up to date, loots after
kills, watches your souls) and adds a window you can click around in:
  Map      - pan (drag) / zoom (mouse wheel) any zone; click a monster dot to Hunt / Skip it
  Pets     - every pet with level, souls and where it drops; double-click to show it on the map
  Nearby   - what you are targeting and the needed monsters around you right now
  Settings - search range and looting, saved to pet_settings.json
  Log      - what pet sync has been doing
The small always-on-top overlay can be switched on from the top bar for while you play.
Live data comes from the Questlog app, so keep Questlog running.

Start it with start_pet_app.bat.
"""
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import pet_sync as ps

BG, PANEL, INK, MUTED, LINE = "#121715", "#18201c", "#e3e9e4", "#93a19a", "#2a352f"
ACC, SOUL, BLUE, GOLD, PINK, DIM = "#4cc39a", "#f08a4b", "#6aa9ff", "#e5c25b", "#d778c7", "#5b6a62"
FONT = ("Segoe UI", 9)


class App:
    def __init__(self):
        ps.claim_instance()
        ps.log("Pet Farm Helper started")
        threading.Thread(target=ps.combat_watcher, daemon=True).start()
        self.sync = ps.Sync()
        self.md = self.sync.mapdata or {"worlds": {}, "spawns": {}, "landmarks": [], "npc_names": {}}
        self.overlay = None
        self.state_key = None
        self.log_mtime = None
        self.code_stamp = self.code_times()

        self.root = root = tk.Tk()
        root.title("Pet Farm Helper")
        root.geometry("1180x780")
        root.configure(bg=BG)
        root.minsize(900, 600)
        self._style()

        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=12, pady=(10, 6))
        self.who = tk.Label(top, text="Waiting for Questlog…", bg=BG, fg=ACC, font=("Segoe UI", 12, "bold"))
        self.who.pack(side="left")
        self.target_lbl = tk.Label(top, text="", bg=BG, fg=INK, font=FONT)
        self.target_lbl.pack(side="left", padx=18)
        # Shown by show_focus() while you picked monsters with "Farm only this"
        self.focus_top = ttk.Button(top, text="", command=self.clear_focus)
        self.overlay_btn = ttk.Button(top, text="Show overlay", command=self.toggle_overlay)
        self.overlay_btn.pack(side="right")
        self.ql_lbl = tk.Label(top, text="", bg=BG, fg=SOUL, font=FONT)
        self.ql_lbl.pack(side="right", padx=12)
        # Shown by tick() once the app's code has changed on disk
        self.update_btn = ttk.Button(top, text="Update ready - click to apply", style="Accent.TButton", command=self.apply_update)
        self.next_lbl = tk.Label(root, text="", bg=BG, fg=INK, font=FONT, anchor="w", justify="left")
        self.next_lbl.pack(fill="x", padx=12)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.nb = nb
        self._map_tab(nb)
        self._pets_tab(nb)
        self._nearby_tab(nb)
        self._settings_tab(nb)
        self._log_tab(nb)

        if ps.SHOW_OVERLAY:
            root.after(500, self.toggle_overlay)
        self.show_focus()
        root.after(100, self.tick)
        root.after(150, self.hotkeys)

    # ------------------------------------------------------------------ look
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=INK, fieldbackground=PANEL, font=FONT, bordercolor=LINE)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 6))
        st.map("TNotebook.Tab", background=[("selected", LINE)], foreground=[("selected", INK)])
        st.configure("TButton", background=LINE, foreground=INK, padding=(10, 4), borderwidth=0)
        st.map("TButton", background=[("active", "#3c4a43"), ("disabled", PANEL)], foreground=[("disabled", DIM)])
        st.configure("Accent.TButton", background=ACC, foreground="#0d1411")
        st.map("Accent.TButton", background=[("active", "#3fa986")])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=INK, rowheight=22, borderwidth=0)
        st.configure("Treeview.Heading", background=LINE, foreground=MUTED, relief="flat")
        st.map("Treeview", background=[("selected", "#1d3a2f")])
        st.configure("TCheckbutton", background=BG, foreground=INK)
        st.configure("TCombobox", fieldbackground=PANEL, background=LINE, foreground=INK, arrowcolor=INK)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", INK)],
               selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", INK)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        st.configure("TEntry", fieldbackground=PANEL, foreground=INK, insertcolor=INK)

    # ------------------------------------------------------------------ helpers
    def levels_souls(self):
        return self.sync.last if self.sync.last else ({}, {})

    def status_of(self, m):
        if m in ps.CLASHED:
            return "look-alike"
        if m in ps.SKIPPED:
            return "skipped"
        if m in self.sync.needed_all:
            return "hunting"
        return "not needed"

    def pets_for(self, m):
        levels, souls = self.levels_souls()
        out = []
        for pid, src in self.sync.sources.items():
            if m in src["monsters"]:
                lv = levels.get(pid, 0)
                have, need = ps.progress(src, lv, souls.get(pid, 0))
                out.append((src["name"], lv, have, need))
        return out

    def toggle_monster(self, m):
        if m in ps.CLASHED:
            return
        self.sync.toggle(m)
        self.sync.step()
        self.redraw_map()
        self.refresh_nearby(force=True)
        self.refresh_pets()
        self.show_info(m)

    def set_focus(self, names):
        ps.save_settings({"FOCUS": names})
        ps.log("Farming only: %s" % ", ".join(names) if names else "Farming everything needed again")
        self.sync.force = True
        self.sync.step()
        self.show_focus()
        self.redraw_map()
        if self.selected:
            self.show_info(self.selected)

    def toggle_focus(self, m):
        self.set_focus([x for x in ps.FOCUS if x != m] if m in ps.FOCUS else ps.FOCUS + [m])

    def clear_focus(self):
        self.set_focus([])

    def show_focus(self):
        self.focus_top.pack_forget()
        if ps.FOCUS:
            self.focus_top.config(text="Only: %s  ×  farm everything" % ", ".join(ps.FOCUS))
            self.focus_top.pack(side="left", after=self.target_lbl)

    # ------------------------------------------------------------------ Map tab
    def _map_tab(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="Map")
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=(8, 6))
        tk.Label(bar, text="Zone", bg=BG, fg=MUTED).pack(side="left")
        # Morheim and Eltnen are left out: they have almost no spawn data and you do not farm there.
        self.world_ids = [w for w in self.md["worlds"] if self.md["worlds"][w]["name"] not in ("Morheim", "Eltnen")]
        self.zone = ttk.Combobox(bar, state="readonly", width=26,
                                 values=[self.md["worlds"][w]["name"] for w in self.world_ids])
        self.zone.pack(side="left", padx=6)
        self.zone.bind("<<ComboboxSelected>>", lambda e: self.set_world(self.world_ids[self.zone.current()]))
        ttk.Button(bar, text="Go to farm spot", style="Accent.TButton", command=self.focus_spot).pack(side="left", padx=4)
        ttk.Button(bar, text="Whole zone", command=self.fit_world).pack(side="left", padx=4)
        self.show_lm = tk.BooleanVar(value=True)
        self.show_skipped = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Teleports & portals", variable=self.show_lm, command=self.redraw_map).pack(side="left", padx=10)
        ttk.Checkbutton(bar, text="Skipped monsters", variable=self.show_skipped, command=self.redraw_map).pack(side="left")
        tk.Label(bar, text="drag = move · wheel = zoom · click a dot = details", bg=BG, fg=MUTED).pack(side="right")

        row = tk.Frame(tab, bg=BG)
        row.pack(fill="x", pady=(0, 6))
        self.area_lbl = tk.Label(row, text="", bg=BG, fg=INK, anchor="w")
        self.area_lbl.pack(side="left")
        self.area_btn = ttk.Button(row, text="Clear chosen area", command=self.clear_area)

        body = tk.Frame(tab, bg=BG)
        body.pack(fill="both", expand=True)
        self.cv = tk.Canvas(body, bg="#0e1311", highlightthickness=0, cursor="fleur")
        self.cv.pack(side="left", fill="both", expand=True)
        side = tk.Frame(body, bg=PANEL, width=290)
        side.pack(side="right", fill="y", padx=(8, 0))
        side.pack_propagate(False)
        self.info_title = tk.Label(side, text="Click a monster dot", bg=PANEL, fg=INK, font=("Segoe UI", 11, "bold"),
                                   wraplength=270, justify="left", anchor="w")
        self.info_title.pack(fill="x", padx=10, pady=(10, 2))
        self.info_body = tk.Label(side, text="", bg=PANEL, fg=MUTED, wraplength=270, justify="left", anchor="w", font=FONT)
        self.info_body.pack(fill="x", padx=10)
        self.info_btn = ttk.Button(side, text="", command=lambda: None)
        self.focus_btn = ttk.Button(side, text="", style="Accent.TButton", command=lambda: None)
        tk.Label(side, bg=PANEL, fg=MUTED, justify="left", anchor="w", font=("Segoe UI", 8), text=(
            "●  orange   hunting (Numpad 8 attacks it)\n"
            "●  grey      skipped by you\n"
            "●  pink      look-alike name, not hunted here\n"
            "▲  blue      teleport point\n"
            "○  yellow    flight circle      □  portal\n"
            "◎  green     where the bot farms\n\n"
            "Distances assume map units are centimetres.")).pack(side="bottom", fill="x", padx=10, pady=10)

        self.view = {"world": None, "cx": 0.0, "cy": 0.0, "s": 0.001}
        self.hits = []
        self.selected = None
        self.drag = None
        self.area_job = None
        self.cv.bind("<Configure>", lambda e: self.redraw_map())
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<Button-3>", self.on_right)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<MouseWheel>", self.on_wheel)
        self.cv.bind("<Motion>", self.on_motion)
        if self.world_ids:
            self.set_world(self.world_ids[0])

    def set_world(self, w, fit=True):
        self.view["world"] = w
        if w in self.world_ids:
            self.zone.current(self.world_ids.index(w))
        if fit:
            self.fit_world()

    def fit_box(self, x0, y0, x1, y1):
        W, H = max(self.cv.winfo_width(), 200), max(self.cv.winfo_height(), 200)
        self.view["cx"], self.view["cy"] = (x0 + x1) / 2, (y0 + y1) / 2
        self.view["s"] = min((W - 30) / max(x1 - x0, 2000), (H - 30) / max(y1 - y0, 2000))
        self.redraw_map()

    def fit_world(self):
        w = self.md["worlds"].get(self.view["world"])
        if w:
            self.fit_box(*w["b"])

    def bot_area(self):
        """Where the bot really goes: (map, x, y), or None. This is the only place the map marks."""
        return tuple(ps.FARM_SPOT) if ps.FARM_SPOT else None

    def focus_spot(self):
        a = self.bot_area()
        if not a:
            return
        self.set_world(a[0], fit=False)
        self.fit_box(a[1] - 22000, a[2] - 22000, a[1] + 22000, a[2] + 22000)

    def on_right(self, e):
        # Save and redraw now, but rewrite the bot script only once the clicking stops: every rewrite
        # makes the bot reload and stops Pet Farm.
        x, y = self.to_world(e.x, e.y)
        ps.save_settings({"FARM_SPOT": [self.view["world"], round(x), round(y)]})
        self.redraw_map()
        if self.area_job:
            self.root.after_cancel(self.area_job)
        self.area_job = self.root.after(1500, lambda: self.set_area(ps.FARM_SPOT))

    def clear_area(self):
        self.set_area([])

    def set_area(self, spot):
        self.area_job = None
        ps.save_settings({"FARM_SPOT": spot})
        ps.log("Bot farm area chosen on the map" if spot else "Bot farm area cleared: hunting around you again")
        self.sync.force = True
        self.sync.step()
        self.show_area()
        self.redraw_map()

    def show_area(self):
        name = lambda w: self.md["worlds"].get(w, {}).get("name", w)
        here = getattr(self.sync, "here", None)
        spot, r = ps.FARM_SPOT, ps.ROUTE
        self.area_btn.pack_forget()
        if spot:
            self.area_btn.pack(side="left", padx=8)
        if spot and here and spot[0] != here:
            text = "Your chosen area is on %s but you are on %s. The bot can't change maps, so go there first." % (
                name(spot[0]), name(here))
        elif spot and r and not r["spawns"]:
            text = "No needed monsters spawn in your chosen area, so the bot will just wait. Right-click somewhere with coloured dots."
        elif spot and r:
            text = "Bot only hunts inside this circle (%d needed spawns). It won't travel here: walk here yourself." % r["spawns"]
        else:
            text = "Bot hunts around where you are. Right-click the map to limit it to one area."
        self.area_lbl.config(text=text)

    def to_screen(self, x, y):
        W, H = self.cv.winfo_width(), self.cv.winfo_height()
        return W / 2 + (x - self.view["cx"]) * self.view["s"], H / 2 - (y - self.view["cy"]) * self.view["s"]

    def to_world(self, sx, sy):
        W, H = self.cv.winfo_width(), self.cv.winfo_height()
        return self.view["cx"] + (sx - W / 2) / self.view["s"], self.view["cy"] - (sy - H / 2) / self.view["s"]

    def redraw_map(self):
        cv, w = self.cv, self.view["world"]
        cv.delete("all")
        self.hits = []
        world = self.md["worlds"].get(w)
        if not world:
            return
        W, H = cv.winfo_width(), cv.winfo_height()
        s = self.view["s"]
        x0, y1 = self.to_world(0, 0)
        x1, y0 = self.to_world(W, H)
        # areas
        for r in world["regions"]:
            fill = "#16211c" if r["k"] == "battlefield" else ""
            for poly in r["p"]:
                if len(poly) > 2:
                    cv.create_polygon(*[c for p in poly for c in self.to_screen(*p)], outline="#3c4a43", fill=fill)
        for r in world["regions"]:
            if r.get("l") and x0 < r["l"][0] < x1 and y0 < r["l"][1] < y1 and (r["k"] == "battlefield" or s > 0.004):
                lx, ly = self.to_screen(*r["l"])
                cv.create_text(lx, ly, text=r["n"], fill="#6f8178", font=("Segoe UI", 8 if s > 0.003 else 7))
        # landmarks
        if self.show_lm.get():
            for wl, lx_, ly_, kind, name in self.md.get("landmarks", []):
                if wl != w or not (x0 <= lx_ <= x1 and y0 <= ly_ <= y1):
                    continue
                qx, qy = self.to_screen(lx_, ly_)
                if kind == "teleport":
                    cv.create_polygon(qx, qy - 6, qx - 5, qy + 4, qx + 5, qy + 4, fill=BLUE, outline="")
                    if s > 0.0025:
                        cv.create_text(qx, qy + 11, text=name, fill=BLUE, font=("Segoe UI", 7))
                elif kind == "flight":
                    cv.create_oval(qx - 4, qy - 4, qx + 4, qy + 4, outline=GOLD)
                else:
                    cv.create_rectangle(qx - 3, qy - 3, qx + 3, qy + 3, outline="#c9c9c9")
                    if s > 0.006:
                        cv.create_text(qx, qy + 10, text=name, fill="#c9c9c9", font=("Segoe UI", 7))
        # needed monster spawns
        r = 2 if s < 0.002 else 3 if s < 0.006 else 4
        for m, pts in self.md["spawns"].items():
            if m not in self.sync.needed_all:
                continue
            st = self.status_of(m)
            if st == "skipped" and not self.show_skipped.get():
                continue
            color = SOUL if st == "hunting" else PINK if st == "look-alike" else DIM
            outline = INK if m == self.selected else ""
            for wp, x, y in pts:
                if wp != w or not (x0 <= x <= x1 and y0 <= y <= y1):
                    continue
                px, py = self.to_screen(x, y)
                cv.create_oval(px - r, py - r, px + r, py + r, fill=color, outline=outline)
                self.hits.append((px, py, m))
        # where the bot really goes, and the loop it walks there
        area = self.bot_area()
        if area and area[0] == w:
            hx, hy = self.to_screen(area[1], area[2])
            rr = max(10, ps.FARM_RADIUS * s)
            cv.create_oval(hx - rr, hy - rr, hx + rr, hy + rr, outline=ACC, width=2)
            cv.create_line(hx - 8, hy, hx + 8, hy, fill=ACC, width=2)
            cv.create_line(hx, hy - 8, hx, hy + 8, fill=ACC, width=2)
            cv.create_text(hx, hy - rr - 8, text="BOT HUNTS HERE", fill=ACC, font=("Segoe UI", 9, "bold"))
        side = "your side" if world["f"] == self.sync.race else "Abyss" if world["f"] == "abyss" else "enemy side"
        cv.create_text(10, H - 10, anchor="sw", fill=MUTED, font=FONT, text="%s · %s" % (world["name"], side))
        # scale bar
        metres = 10
        for metres in (10, 25, 50, 100, 250, 500, 1000, 2500, 5000):
            if metres * 100 * s > 80:
                break
        bar = metres * 100 * s
        cv.create_line(W - 20 - bar, H - 14, W - 20, H - 14, fill=MUTED, width=2)
        cv.create_text(W - 20 - bar / 2, H - 24, text="%d m" % metres if metres < 1000 else "%g km" % (metres / 1000.0),
                       fill=MUTED, font=("Segoe UI", 8))

    def nearest_hit(self, x, y, within=9):
        best, bd = None, within * within
        for px, py, m in self.hits:
            d = (px - x) ** 2 + (py - y) ** 2
            if d < bd:
                best, bd = m, d
        return best

    def on_press(self, e):
        self.drag = [e.x, e.y, self.view["cx"], self.view["cy"], False]

    def on_drag(self, e):
        if not self.drag:
            return
        sx, sy, cx, cy, _ = self.drag
        if abs(e.x - sx) + abs(e.y - sy) > 3:
            self.drag[4] = True
            self.view["cx"] = cx - (e.x - sx) / self.view["s"]
            self.view["cy"] = cy + (e.y - sy) / self.view["s"]
            self.redraw_map()

    def on_release(self, e):
        moved = self.drag and self.drag[4]
        self.drag = None
        if not moved:
            m = self.nearest_hit(e.x, e.y)
            if m:
                self.selected = m
                self.show_info(m)
                self.redraw_map()

    def on_wheel(self, e):
        f = 1.25 if e.delta > 0 else 0.8
        wx, wy = self.to_world(e.x, e.y)
        self.view["s"] *= f
        nx, ny = self.to_world(e.x, e.y)
        self.view["cx"] += wx - nx
        self.view["cy"] += wy - ny
        self.redraw_map()

    def on_motion(self, e):
        self.cv.configure(cursor="hand2" if self.nearest_hit(e.x, e.y) else "fleur")

    def show_info(self, m):
        st = self.status_of(m)
        w = self.view["world"]
        here = sum(1 for p in self.md["spawns"].get(m, []) if p[0] == w)
        lines = ["Status: %s" % {"hunting": "Hunting - Numpad 8 attacks it", "skipped": "Skipped by you",
                                  "look-alike": "Look-alike name on this map - not hunted",
                                  "not needed": "Not needed any more"}[st],
                 "Spawns on this map: %d" % here, "", "Drops souls for:"]
        for name, lv, have, need in self.pets_for(m):
            lines.append("  %s - %s" % (name, "done (Lv 3)" if lv >= 3 else "Lv %d, %d/%d souls" % (lv, have, need)))
        self.info_title.config(text=m)
        self.info_body.config(text="\n".join(lines))
        self.info_btn.pack_forget()
        self.focus_btn.pack_forget()
        if st == "hunting":
            self.focus_btn.config(text="Stop farming only this" if m in ps.FOCUS else "Farm only this",
                                  command=lambda: self.toggle_focus(m))
            self.focus_btn.pack(anchor="w", padx=10, pady=(10, 0))
        if st in ("hunting", "skipped"):
            self.info_btn.config(text="Skip for now" if st == "hunting" else "Hunt it again",
                                 command=lambda: self.toggle_monster(m))
            self.info_btn.pack(anchor="w", padx=10, pady=10)

    # ------------------------------------------------------------------ Pets tab
    def _pets_tab(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="Pets")
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x", pady=8)
        tk.Label(bar, text="Show", bg=BG, fg=MUTED).pack(side="left")
        self.pet_filter = ttk.Combobox(bar, state="readonly", width=18,
                                       values=["Still needed", "Missing", "Done (Lv 3)", "All"])
        self.pet_filter.current(0)
        self.pet_filter.pack(side="left", padx=6)
        self.pet_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh_pets())
        tk.Label(bar, text="Search", bg=BG, fg=MUTED).pack(side="left", padx=(12, 0))
        self.pet_search = tk.StringVar()
        ttk.Entry(bar, textvariable=self.pet_search, width=24).pack(side="left", padx=6)
        self.pet_search.trace_add("write", lambda *a: self.refresh_pets())
        tk.Label(bar, text="double-click a pet to show it on the map", bg=BG, fg=MUTED).pack(side="right")
        cols = ("pet", "lv", "souls", "monsters", "zones")
        tv = ttk.Treeview(tab, columns=cols, show="headings")
        for c, t, wdt in (("pet", "Pet", 200), ("lv", "Level", 70), ("souls", "Souls", 110),
                          ("monsters", "Drops from", 380), ("zones", "Where", 260)):
            tv.heading(c, text=t)
            tv.column(c, width=wdt, anchor="w")
        tv.pack(fill="both", expand=True)
        tv.bind("<Double-1>", self.pet_on_map)
        self.pet_tv = tv

    def refresh_pets(self):
        levels, souls = self.levels_souls()
        flt = self.pet_filter.get()
        q = self.pet_search.get().strip().lower()
        tv = self.pet_tv
        tv.delete(*tv.get_children())
        rows = []
        for pid, src in self.sync.sources.items():
            lv = levels.get(pid, 0)
            if (flt == "Still needed" and lv >= 3) or (flt == "Missing" and lv > 0) or (flt == "Done (Lv 3)" and lv < 3):
                continue
            if q and q not in src["name"].lower() and not any(q in m.lower() for m in src["monsters"]):
                continue
            have, need = ps.progress(src, lv, souls.get(pid, 0))
            zones = {}
            for m in src["monsters"]:
                for p in self.md["spawns"].get(m, []):
                    zones[p[0]] = zones.get(p[0], 0) + 1
            zn = ", ".join(self.md["worlds"][z]["name"] for z in sorted(zones, key=lambda z: -zones[z]) if z in self.md["worlds"])
            soul_txt = "done" if lv >= 3 else "%d / %d" % (have, need)
            mons = ", ".join("%s%s" % (m, " (skipped)" if m in ps.SKIPPED else "") for m in src["monsters"])
            rows.append((lv >= 3, -(have / float(max(need, 1))) if lv < 3 else 0, src["name"], pid, lv, soul_txt, mons, zn))
        for done, _, name, pid, lv, soul_txt, mons, zn in sorted(rows):
            tv.insert("", "end", iid=pid, values=(name, "Missing" if lv == 0 else "Lv %d" % lv, soul_txt, mons, zn))

    def pet_on_map(self, e):
        sel = self.pet_tv.selection()
        if not sel:
            return
        src = self.sync.sources.get(sel[0])
        if not src:
            return
        best = {}
        for m in src["monsters"]:
            for p in self.md["spawns"].get(m, []):
                best.setdefault(p[0], []).append((p[1], p[2]))
        if not best:
            return
        here = getattr(self.sync, "here", None)
        w = max(best, key=lambda k: len(best[k]) + (100000 if k == here else 0))
        pts = best[w]
        self.nb.select(0)
        self.set_world(w, fit=False)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.selected = src["monsters"][0]
        self.fit_box(min(xs) - 8000, min(ys) - 8000, max(xs) + 8000, max(ys) + 8000)
        self.show_info(self.selected)

    # ------------------------------------------------------------------ Nearby tab
    def _nearby_tab(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="Nearby")
        self.near_target = tk.Label(tab, text="", bg=BG, fg=INK, font=("Segoe UI", 11, "bold"), anchor="w")
        self.near_target.pack(fill="x", pady=(10, 4))
        tk.Label(tab, text="Needed monsters loaded around you (from Questlog's live combat log). "
                           "Ctrl+Alt+1..8 switches the numbered rows while you're in the game.",
                 bg=BG, fg=MUTED, anchor="w").pack(fill="x")
        cols = ("n", "monster", "count", "status", "pets")
        tv = ttk.Treeview(tab, columns=cols, show="headings", height=12)
        for c, t, wdt in (("n", "#", 40), ("monster", "Monster", 240), ("count", "Around you", 90),
                          ("status", "Status", 110), ("pets", "For pets", 460)):
            tv.heading(c, text=t)
            tv.column(c, width=wdt, anchor="w")
        tv.pack(fill="both", expand=True, pady=8)
        self.near_tv = tv
        bar = tk.Frame(tab, bg=BG)
        bar.pack(fill="x")
        ttk.Button(bar, text="Hunt / Skip selected", style="Accent.TButton", command=self.near_toggle).pack(side="left")
        tk.Label(bar, text="After switching, press Numpad 8 again (the bot reloads its list).", bg=BG, fg=MUTED).pack(side="left", padx=12)
        self.near_rows = []
        self.near_key = None

    def refresh_nearby(self, force=False):
        names = self.md.get("npc_names", {})
        tgt = ps.LIVE["target"]
        tname = names.get(ps.LIVE["alive"].get(tgt)) if tgt else None
        if not tgt:
            txt = "Targeting: nothing"
        elif not tname:
            txt = "Targeting: a monster no pet needs"
        else:
            txt = "Targeting: %s (%s)" % (tname, self.status_of(tname))
        self.near_target.config(text=txt)
        self.target_lbl.config(text=txt)
        counts = {}
        for npc in list(ps.LIVE["alive"].values()):
            n = names.get(npc)
            if n in self.sync.needed_all:
                counts[n] = counts.get(n, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        key = tuple((n, c, self.status_of(n)) for n, c in rows)
        if key == self.near_key and not force:
            return
        self.near_key = key
        sel = self.near_tv.selection()
        self.near_tv.delete(*self.near_tv.get_children())
        self.near_rows = [n for n, _ in rows]
        for i, (n, c) in enumerate(rows, 1):
            pets = ", ".join(sorted(set(self.sync.needed_all.get(n, []))))
            self.near_tv.insert("", "end", iid=n, values=(i if i <= 8 else "", n, c, self.status_of(n), pets))
        for s in sel:
            if self.near_tv.exists(s):
                self.near_tv.selection_add(s)

    def near_toggle(self):
        for m in self.near_tv.selection():
            self.toggle_monster(m)

    # ------------------------------------------------------------------ Settings tab
    def _settings_tab(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="Settings")
        grid = tk.Frame(tab, bg=BG)
        grid.pack(anchor="nw", padx=6, pady=12)
        s = ps.current_settings()
        self.v_range = tk.StringVar(value=str(s["PET_RANGE"]))
        self.v_loot = tk.BooleanVar(value=bool(s["LOOT_AFTER_KILLS"]))
        self.v_presses = tk.StringVar(value=str(s["LOOT_PRESSES"]))
        self.v_delay = tk.StringVar(value=str(s["LOOT_DELAY"]))
        self.v_gap = tk.StringVar(value=str(s["LOOT_GAP"]))
        self.v_overlay = tk.BooleanVar(value=bool(s["SHOW_OVERLAY"]))
        self.v_rearm = tk.BooleanVar(value=bool(s["AUTO_REARM"]))
        self.v_rearm_delay = tk.StringVar(value=str(s["REARM_DELAY"]))
        self.v_move = tk.BooleanVar(value=bool(s["AUTO_MOVE"]))
        self.v_zone = tk.BooleanVar(value=bool(s["ZONE_ONLY"]))
        rows = [
            ("Search range", ttk.Entry(grid, textvariable=self.v_range, width=10),
             "How far Pet Farm looks for needed monsters. Too far = it attacks things it can't reach."),
            ("Walk to monsters", ttk.Checkbutton(grid, variable=self.v_move),
             "When nothing needed is in range, walk to the nearest one nearby. Not between zones."),
            ("Only this map", ttk.Checkbutton(grid, variable=self.v_zone),
             "List only monsters on your current map, so the bot reacts faster. Needs Questlog running."),
            ("Loot after kills", ttk.Checkbutton(grid, variable=self.v_loot),
             "Press F after each of your kills while AION2 is the active window."),
            ("F presses per kill", ttk.Entry(grid, textvariable=self.v_presses, width=6), ""),
            ("Delay before looting (s)", ttk.Entry(grid, textvariable=self.v_delay, width=6), ""),
            ("Gap between presses (s)", ttk.Entry(grid, textvariable=self.v_gap, width=6), ""),
            ("Open overlay on start", ttk.Checkbutton(grid, variable=self.v_overlay), ""),
            ("Auto re-press Numpad 8", ttk.Checkbutton(grid, variable=self.v_rearm),
             "When the bot list changes while you farm (and AION2 is active), restart Pet Farm by itself."),
            ("Wait before re-pressing (s)", ttk.Entry(grid, textvariable=self.v_rearm_delay, width=6),
             "Time for the bot to reload chant2.ini first."),
        ]
        for i, (label, widget, hint) in enumerate(rows):
            tk.Label(grid, text=label, bg=BG, fg=INK, anchor="w").grid(row=i, column=0, sticky="w", pady=5, padx=(0, 14))
            widget.grid(row=i, column=1, sticky="w")
            tk.Label(grid, text=hint, bg=BG, fg=MUTED, anchor="w").grid(row=i, column=2, sticky="w", padx=14)
        bar = tk.Frame(tab, bg=BG)
        bar.pack(anchor="nw", padx=6, pady=8)
        ttk.Button(bar, text="Save settings", style="Accent.TButton", command=self.save_settings).pack(side="left")
        self.settings_msg = tk.Label(bar, text="", bg=BG, fg=MUTED)
        self.settings_msg.pack(side="left", padx=12)

    def save_settings(self):
        try:
            rng = int(float(self.v_range.get()))
            vals = {"PET_RANGE": rng, "LOOT_AFTER_KILLS": self.v_loot.get(),
                    "LOOT_PRESSES": max(1, int(float(self.v_presses.get()))), "LOOT_DELAY": float(self.v_delay.get()),
                    "LOOT_GAP": float(self.v_gap.get()), "SHOW_OVERLAY": self.v_overlay.get(),
                    "AUTO_REARM": self.v_rearm.get(), "REARM_DELAY": max(0.5, float(self.v_rearm_delay.get())),
                    "AUTO_MOVE": self.v_move.get(), "ZONE_ONLY": self.v_zone.get()}
        except ValueError:
            self.settings_msg.config(text="Please enter numbers only.", fg=SOUL)
            return
        before = ps.current_settings()
        ps.save_settings(vals)
        changed_list = any(before[k] != vals[k] for k in ("PET_RANGE", "AUTO_MOVE", "ZONE_ONLY"))
        self.sync.force = True
        self.sync.step()
        ps.log("Settings saved: range %d, loot %s" % (vals["PET_RANGE"], vals["LOOT_AFTER_KILLS"]))
        self.settings_msg.config(fg=ACC, text="Saved." + (" The bot list changed: press Numpad 8 again." if changed_list else ""))

    # ------------------------------------------------------------------ Log tab
    def _log_tab(self, nb):
        tab = tk.Frame(nb, bg=BG)
        nb.add(tab, text="Log")
        self.log_text = tk.Text(tab, bg=PANEL, fg=INK, font=("Consolas", 9), relief="flat", wrap="none")
        self.log_text.pack(fill="both", expand=True, pady=8)

    def refresh_log(self):
        try:
            mt = os.path.getmtime(ps.LOG)
        except OSError:
            return
        if mt == self.log_mtime:
            return
        self.log_mtime = mt
        with open(ps.LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-400:]
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", "".join(lines))
        self.log_text.see("end")

    # ------------------------------------------------------------------ overlay, hotkeys, loop
    def toggle_overlay(self):
        if self.overlay is not None and self.overlay.winfo_exists():
            self.overlay.destroy()
            self.overlay = None
        else:
            self.overlay = ps.run_overlay(self.sync, master=self.root)
        self.overlay_btn.config(text="Hide overlay" if self.overlay else "Show overlay")

    def hotkeys(self):
        # The overlay handles Ctrl+Alt+number while it is open; otherwise the app does
        if not (self.overlay is not None and self.overlay.winfo_exists()):
            q = ps.hotkey_queue()
            while not q.empty():
                k = q.get()
                if 1 <= k <= min(8, len(self.near_rows)):
                    self.toggle_monster(self.near_rows[k - 1])
        self.root.after(150, self.hotkeys)

    def questlog_running(self):
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Questlog.exe", "/NH"], capture_output=True,
                                 text=True, creationflags=0x08000000).stdout
            return "Questlog.exe" in out
        except Exception:
            return True

    def code_times(self):
        return [os.path.getmtime(os.path.join(ps.HERE, f)) for f in ("pet_app.py", "pet_sync.py")]

    def apply_update(self):
        # The new copy claims pet_sync.pid and this one then closes itself in tick(). If the new
        # copy can't start, this one simply keeps running.
        self.update_btn.config(text="Updating…", state="disabled")
        subprocess.Popen([sys.executable, os.path.join(ps.HERE, "pet_app.py")], cwd=ps.HERE, creationflags=0x00000010)
        ps.log("Update applied: starting the new version")
        self.root.after(8000, lambda: self.update_btn.config(text="Update ready - click to apply", state="normal"))

    def tick(self):
        if ps.replaced():
            ps.log("A newer pet sync started; closing this one")
            self.root.destroy()
            return
        if self.code_times() != self.code_stamp and not self.update_btn.winfo_ismapped():
            self.update_btn.pack(side="right", padx=(0, 8), before=self.ql_lbl)
        try:
            self.sync.step()
        except (OSError, ValueError, RuntimeError) as e:
            ps.log("Waiting: %s" % e)
        self.who.config(text=self.sync.who or "Waiting for Questlog…")
        if not self.questlog_running():
            warn = "Questlog is closed - souls, zone, nearby and looting won't update"
        else:
            # Questlog writes its combat log several times a second while you're in game; if it has
            # gone quiet, looting after kills and Nearby have nothing to go on.
            newest = ps.newest_combat_log()
            age = (time.time() - os.path.getmtime(newest)) if newest else 1e9
            warn = "" if age < 60 else "Questlog isn't recording combat - looting and Nearby are paused"
        self.ql_lbl.config(text=warn)
        p = self.sync.plan
        if p:
            mk = p.get("mark")
            route = ""
            if mk:
                dist = ("%.1f km" % (mk["m"] / 1000.0)) if mk["m"] >= 1000 else "%d m" % mk["m"]
                route = "   ·   from teleport \"%s\": ~%s %s" % (mk["name"], dist, mk["dir"])
            spot = ", ".join("%s ×%d" % kv for kv in sorted(p.get("hot", {}).items(), key=lambda kv: -kv[1]))
            self.next_lbl.config(text="Most souls overall (just a tip, the bot does not go here): %s · %s of %s%s\nAt that spot: %s" % (
                p["zone"], p.get("part") or "in", p["area"], route, spot))
        else:
            self.next_lbl.config(text="Nothing left to farm")
        key = (str(self.sync.last), str(sorted(ps.SKIPPED)), p and (p["world"], round(p.get("hx", 0)), round(p.get("hy", 0))), str(ps.ROUTE), str(ps.FARM_SPOT))
        if key != self.state_key:
            first = self.state_key is None
            self.state_key = key
            self.refresh_pets()
            self.show_area()
            if first and self.bot_area():
                self.focus_spot()
            else:
                self.redraw_map()
            if self.selected:
                self.show_info(self.selected)
        self.refresh_nearby()
        self.refresh_log()
        try:
            ps.collect_routes()
        except OSError as e:
            ps.log("Could not collect routes: %s" % e)
        self.root.after(ps.CHECK_EVERY_SECONDS * 1000, self.tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
