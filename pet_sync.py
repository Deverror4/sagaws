"""Live pet sync: keeps the bot's [fPetFarm] list in chant2.ini matched to the pets you still need.

Watches the Questlog app's live tracker (map-tracker.json: current levels and soul counts) and its
character save (characters.json). Whenever a soul drops or a pet levels up, it rewrites the block
between the PET SYNC markers in chant2.ini with every monster that drops a soul for a pet that is
missing or below level 3, ordered so the pets closest to their next level come first. Pets that
reach level 3 drop out of the list automatically. Soul changes alone only reorder the list, so the
file is left alone then: the bot stops farming whenever chant2.ini changes.

Run it with start_pet_sync.bat and leave the window open while you play.
"""
import collections
import json
import math
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CHANT = os.path.join(HERE, "chant2.ini")
SOURCES = os.path.join(HERE, "pet_sources.json")
QL_DIR = os.path.join(os.environ.get("APPDATA", ""), "Questlog", "aion-2")
CHARACTERS = os.path.join(QL_DIR, "characters.json")
TRACKER = os.path.join(QL_DIR, "map-tracker.json")
LOG = os.path.join(HERE, "pet_sync.log")
ROUTES = os.path.join(HERE, "routes")

# Leave empty to follow whichever character Questlog saw last (pets are shared per server).
# Or pin one, e.g. "2301:YourName".
CHARACTER = ""
CHECK_EVERY_SECONDS = 2
# How far the bot looks for pet monsters (same units as the Kisk script's tcg % Kisk,5000).
# Set to 0 to use the bot's default range.
PET_RANGE = 6000
# When nothing needed is in attack range, walk to the nearest needed monster (tcm picks the closest of
# all the names, mtt walks to it). It can only find monsters the game has loaded around you, so this
# works inside an area, not between zones. MOVE_CLOSE is where to stop (negative = in front).
AUTO_MOVE = True
MOVE_RANGE = 20000
MOVE_CLOSE = -300
# Only list monsters that spawn on the map you are on. Needs Questlog running to notice you changed map.
ZONE_ONLY = True
# Monsters you picked in the app with "Farm only this". While any are set, Pet Farm hunts just those.
FOCUS = []
# The area you chose on the app's map, as [map, x, y]; empty = anywhere around you.
FARM_SPOT = []
FARM_RADIUS = 12000  # how far around the chosen point it hunts
ROUTE = None         # the chosen area in use, for the app: map, its point, needed spawns inside
LEARN_ROUTE = False  # saved-place route: off while checking whether it causes the sliding
SAVE_EVERY = 30000   # ms between saved places
ROUTE_POINTS = 6     # how many places are remembered
ROUTE_STEPS = 3      # short walks toward a place before trying the next
ROUTE_WALK = 5000    # ms for one short walk; Numpad 8 can only stop it between walks
NO_SOUL_KILLS = 15  # this many kills with no soul for its pets = the monster does not give that soul
FIGHT_BACK_RANGE = 2000  # only monsters targeting you this close: right on you, not ones that just noticed you
# Monsters to leave alone for now (exact names), e.g. ones that fly out of reach. The bot will not
# target them and FARM NEXT will not send you to them. Remove a name to farm it again.
IGNORE = ["Modified Lupyllini"]

START = "; >>> PET SYNC (written by pet_sync.py - edits here get overwritten) >>>"
END = "; <<< PET SYNC <<<"


def log(msg):
    line = datetime.now().strftime("%H:%M:%S") + "  " + msg
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    """Returns (who, levels, souls). Live tracker first, character save as fallback."""
    levels, souls, who = {}, {}, None
    if os.path.exists(TRACKER):
        c = read_json(TRACKER).get("character") or {}
        key = "%s:%s" % (c.get("server"), c.get("name"))
        if c.get("levels") and (not CHARACTER or CHARACTER == key):
            who = "%s (server %s)" % (c.get("name"), c.get("server"))
            levels = {str(k): int(v or 0) for k, v in c["levels"].items()}
            souls = {str(k): int(v or 0) for k, v in (c.get("souls") or {}).items()}
    if not levels and os.path.exists(CHARACTERS):
        chars = read_json(CHARACTERS).get("characters", {})
        if CHARACTER:
            c = chars.get(CHARACTER)
        else:
            c = max((c for c in chars.values() if c.get("pets")), key=lambda c: c.get("updatedAt", 0), default=None)
        if c:
            who = "%s (server %s)" % (c.get("name"), c.get("server"))
            levels = {str(p["pet"]): int(p.get("level") or 0) for p in c.get("pets", [])}
    return who, levels, souls


def progress(src, level, souls):
    """(souls now, souls needed for the next level)"""
    need = src.get("need") or [5, 25, 75]
    return souls, need[min(level, len(need) - 1)]


_warned = set()
SKIPPED = set()  # needed monster names left out of the bot list (your skips + look-alike names)
CLASHED = set()  # the look-alike part of SKIPPED: these can't be switched back on from the overlay
IGNORE_FILE = os.path.join(HERE, "pet_ignore.json")


def load_ignored():
    """Monsters you chose to skip (overlay buttons); starts from IGNORE until you click one."""
    try:
        with open(IGNORE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set(IGNORE)


def save_ignored(names):
    with open(IGNORE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(names), f, ensure_ascii=False, indent=1)

try:  # every monster name questlog knows on the maps, and which maps it spawns on (name-clash check)
    with open(os.path.join(HERE, "pet_map.json"), encoding="utf-8") as _f:
        SPAWN_POINTS = json.load(_f)["spawns"]
    SPAWN_WORLDS = {m: {p[0] for p in pts} for m, pts in SPAWN_POINTS.items()}
except (OSError, ValueError, KeyError):
    SPAWN_POINTS, SPAWN_WORLDS = {}, {}
KNOWN_MONSTERS = list(SPAWN_WORLDS)


def build_block(sources, levels, souls, who, here=None):
    needed = [pid for pid in sources if levels.get(pid, 0) < 3]

    def left(pid):
        have, need = progress(sources[pid], levels.get(pid, 0), souls.get(pid, 0))
        return max(need - have, 0)

    # Missing pets first (5 souls unlocks them), then whatever is closest to its next level.
    needed.sort(key=lambda pid: (levels.get(pid, 0) != 0, left(pid), sources[pid]["name"]))
    monsters = {}
    for pid in needed:
        lv = levels.get(pid, 0)
        have, need = progress(sources[pid], lv, souls.get(pid, 0))
        label = "%s %d/%d to %s" % (sources[pid]["name"], have, need, "unlock" if lv == 0 else "Lv %d" % (lv + 1))
        for m in sources[pid]["monsters"]:
            monsters.setdefault(m, []).append(label)

    # tcm matches any monster whose name CONTAINS the given name, so "Water Spirit" would also pick
    # Immortal / Intermediate Water Spirits (another tier). Leave such names out while the pet still
    # has another monster that drops its soul. Only a word IN FRONT counts as another tier: a name
    # that just gets a word added after it ("Immortal Earth Spirit Guardian") is kept, since the
    # monster itself is still needed and the odd extra Guardian kill is harmless.
    SKIPPED.clear()
    CLASHED.clear()
    for m in load_ignored():
        if monsters.pop(m, None) is not None:
            SKIPPED.add(m)
    known = set(KNOWN_MONSTERS) | {m for s in sources.values() for m in s["monsters"]}
    if here:
        # Only monsters that actually spawn on the map you are on can be mixed up there:
        # "Drana Mutant" is fine in Altgard because "Abyss Drana Mutant" only lives in the Abyss.
        known = {m for m in known if here in SPAWN_WORLDS.get(m, ())}
    clashes = {m: sorted(o for o in known if o != m and o not in monsters
                         and m.lower() in o.lower() and not o.lower().startswith(m.lower())) for m in monsters}
    for m, others in clashes.items():
        if not others:
            continue
        pets = [pid for pid in needed if m in sources[pid]["monsters"]]
        if all(any(o != m and not clashes.get(o) for o in sources[pid]["monsters"]) for pid in pets):
            del monsters[m]
            SKIPPED.add(m)
            CLASHED.add(m)
            if m not in _warned:
                _warned.add(m)
                log("Skipping '%s': the bot would also attack %s" % (m, ", ".join(others[:3]) + (" ..." if len(others) > 3 else "")))
    global ROUTE
    ROUTE = None
    here = here or (FARM_SPOT[0] if FARM_SPOT else None)  # Questlog not running: trust the chosen area
    spot = FARM_SPOT if FARM_SPOT and FARM_SPOT[0] == here else None
    for m in no_soul_monsters(monsters, sources, needed):
        del monsters[m]
        SKIPPED.add(m)
        if m not in _warned:
            _warned.add(m)
            log("Skipping '%s': %d+ kills and its pets never gained a soul" % (m, NO_SOUL_KILLS))
    picked = {m: v for m, v in monsters.items() if m in FOCUS}
    if picked:
        monsters = picked
    elif ZONE_ONLY and here:
        # Monsters with no known spawn stay in; a map with none of the list keeps the full list.
        local = {m: v for m, v in monsters.items() if here in SPAWN_WORLDS.get(m, (here,))}
        monsters = local or monsters
    lines = [
        START,
        "; --- FUNCTION: PET SOUL FARM (Numpad 8) ---",
        "; %s: %d pets still needed, %d monsters. Always the closest needed monster. Updated %s." % (
            who, len(needed), len(monsters), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "[fPetFarm]",
        "keys=esp % delete,30,1",
    ]
    # Anything attacking you comes first, needed or not, or the bot would stand there and take the hits.
    lines.append("keys2=ibtM%d|tcmT%d|call %% fRotation|dbg %% [PET] Fighting back|gt*" % (FIGHT_BACK_RANGE, FIGHT_BACK_RANGE))
    n = 2
    # tcm with several names (a;b;c) targets the closest of all of them
    names = ";".join(sorted(monsters))
    if spot:
        # Chosen area: only monsters inside it count, and the bot never walks to a point, only to a target.
        # Target as normal, then check where the target stands (get % target gives TX, TY, TZ).
        inside = "get %% target|cmp(VAR %% TX),%d.0|!cmp(VAR %% TX),%d.0|cmp(VAR %% TZ),%d.0|!cmp(VAR %% TZ),%d.0" % (
            spot[1] + FARM_RADIUS, spot[1] - FARM_RADIUS, spot[2] + FARM_RADIUS, spot[2] - FARM_RADIUS)
        ROUTE = {"world": here, "way": [(spot[1], spot[2])], "chosen": True,
                 "spawns": sum(1 for m in monsters if m not in IGNORE for w, x, y in SPAWN_POINTS.get(m, ())
                               if w == here and math.hypot(x - spot[1], y - spot[2]) <= FARM_RADIUS)}
    if monsters:
        n += 1
        if spot:
            lines.append("keys%d=tcm %% %s,%d|%s|call %% fRotation|dbg %% [PET] Fighting a needed monster in your area|gt*" % (
                n, names, PET_RANGE or 6000, inside))
        else:
            lines.append("keys%d=tcm %% %s|%scall %% fRotation|dbg %% [PET] Fighting the closest needed monster|gt*" % (
                n, "%s,%d" % (names, PET_RANGE) if PET_RANGE else names, "call %% fPetSave|" if LEARN_ROUTE else ""))
    if AUTO_MOVE and monsters:
        # Nothing in attack range: give the looter a moment, then walk to the closest needed monster.
        # The next pass of Pet Farm attacks it once it is in range.
        # The on-screen text says which it is: still walking/looking, or nothing needed loaded here.
        n += 1
        # Let go of the keys the rotation holds down, or the character keeps attacking with no target.
        lines.append("keys%d=store %% hold_state,off|(VAR %% Key_Onslaught_Hold_VK)u|(VAR %% Key_Macro_Hold_VK)u|esp %% add,30,1,PET FARM: LOOKING FOR NEEDED MOBS,900,690,(VAR %% ESP_FontSize),16753920|s500" % n)
        # Nothing left to fight here: pick up what is nearby before walking on (Numpad 5 turns this off).
        n += 1
        lines.append("keys%d=eq %% autoloot_phase,active|itl|(VAR %% Key_Loot_VK)d|rs120,150|(VAR %% Key_Loot_VK)u|s200|(VAR %% Key_Loot_VK)d|rs120,150|(VAR %% Key_Loot_VK)u|s200|(VAR %% Key_Loot_VK)d|rs120,150|(VAR %% Key_Loot_VK)u|s200|(VAR %% Key_Loot_VK)d|rs120,150|(VAR %% Key_Loot_VK)u|dbg %% [LOOT] Picking up nearby loot" % n)
        n += 1
        walk = "esp %% edit,30,1,PET FARM: WALKING TO A NEEDED MONSTER|mtt%d|dbg %% [MOVE] Walking to the closest needed monster|gt*" % MOVE_CLOSE
        if spot:
            lines.append("keys%d=tcm %% %s,%d|%s|%s" % (n, names, MOVE_RANGE, inside, walk))
            lines.append("keys%d=esp %% edit,30,1,PET FARM: NO NEEDED MONSTER IN YOUR AREA|s500" % (n + 1))
        else:
            lines.append("keys%d=tcm %% %s,%d|%s" % (n, names, MOVE_RANGE, walk))
            # Nothing around: go back through the places monsters were fought. No saved places yet = just wait.
            if LEARN_ROUTE:
                n += 1
                lines.append("keys%d=call %% fPetRoute" % n)
            lines.append("keys%d=esp %% edit,30,1,PET FARM: NO NEEDED MONSTER NEARBY|s500" % (n + 1))
    else:
        lines.append("keys%d=nop" % (n + 1))
    if LEARN_ROUTE and AUTO_MOVE and monsters and not spot:
        lines += route_lines()
    lines.append(END)
    return "\n".join(lines), needed


def route_lines():
    """[fPetSave] remembers where you stand during fights, at most once per SAVE_EVERY ms, in ROUTE_POINTS
    slots that are reused in turn. [fPetRoute] walks through them, ROUTE_STEPS short walks per place.
    [104] clears them when Pet Farm starts. Your own position is used because you really stood there."""
    out = ["", "[fPetSave]", "keys=!ct % pet_save_t|gt*",
           "keys2=st %% pet_save_t,%d,%d|add %% pet_n,1" % (SAVE_EVERY, SAVE_EVERY),
           "keys3=!cmp(VAR %% pet_n),%d.0|store %% pet_n,1" % ROUTE_POINTS,
           "keys4=get % pos"]
    for i in range(1, ROUTE_POINTS + 1):
        out.append("keys%d=eq %% pet_n,%d|store %% pet_x%d,(VAR %% PX)|store %% pet_z%d,(VAR %% PZ)|store %% pet_h%d,1|dbg %% [ROUTE] Saved place %d|gt*" % (
            i + 4, i, i, i, i, i))
    total = ROUTE_POINTS * ROUTE_STEPS
    out += ["", "[fPetRoute]", "keys=add % pet_w,1", "keys2=!cmp(VAR %% pet_w),%d.0|store %% pet_w,1" % total, "keys3=get % pos"]
    for s in range(total):
        i = s // ROUTE_STEPS + 1
        out.append("keys%d=eq %% pet_w,%d|eq %% pet_h%d,1|esp %% edit,30,1,PET FARM: GOING BACK TO SAVED PLACE %d|mt(VAR %% pet_x%d),(VAR %% PY),(VAR %% pet_z%d),%d|dbg %% [ROUTE] Walking to place %d|gt*" % (
            s + 4, s + 1, i, i, i, i, ROUTE_WALK, i))
    return out


def no_soul_monsters(monsters, sources, needed):
    """Monsters killed NO_SOUL_KILLS times or more while none of the pets they are listed under gained a
    soul. pet_sources.json can be wrong (Unstable Ashen Spider paid nothing in 30 kills), so go by results."""
    try:
        tracker = read_json(TRACKER)
        with open(os.path.join(HERE, "pet_map.json"), encoding="utf-8") as f:
            names = json.load(f)["npc_names"]
    except (OSError, ValueError, KeyError):
        return set()
    kills, gained = collections.Counter(), tracker.get("soulsGained") or {}
    for npc, count in (tracker.get("kills") or {}).items():
        kills[names.get(npc)] += count
    return {m for m in monsters if kills[m] >= NO_SOUL_KILLS and
            not any(gained.get(pid) for pid in needed if m in sources[pid]["monsters"])}


def collect_routes():
    """Speeder saves each recorded route under a random name next to the config. Move those into routes/.
    Never touches chant1/chant2, empty files, or a file still being written."""
    os.makedirs(ROUTES, exist_ok=True)
    moved = []
    for f in os.listdir(HERE):
        path = os.path.join(HERE, f)
        if (not re.fullmatch(r"[A-Za-z0-9]{6,16}\.ini", f) or f.lower() in ("chant1.ini", "chant2.ini")
                or not os.path.isfile(path) or os.path.getsize(path) == 0
                or time.time() - os.path.getmtime(path) < 5):
            continue
        to = os.path.join(ROUTES, "route_%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"), f))
        try:
            os.replace(path, to)
        except OSError:
            continue
        moved.append(to)
        log("Route recorded: %s" % os.path.basename(to))
    return moved


def write_block(block):
    with open(CHANT, encoding="utf-8", newline="") as f:
        text = f.read()
    a, b = text.find(START), text.find(END)
    if a < 0 or b < 0:
        raise RuntimeError("PET SYNC markers not found in chant2.ini")
    # The bot reloads chant2.ini (and stops farming) every time the file changes, so only write
    # when the set of monsters to hunt changes - not when souls just reorder the list.
    targets = lambda s: {l.split("=", 1)[1] if "[LOOT]" in l or "call % fRotation" in l else l.split("|", 1)[0].split("=", 1)[1] for l in s.splitlines()
                         if l.startswith("keys") and any(c in l for c in ("tcm %", "esp %", "[LOOT]", "call % fRotation"))}
    if targets(text[a:b + len(END)]) == targets(block):
        return False
    tmp = CHANT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text[:a] + block + text[b + len(END):])
    os.replace(tmp, CHANT)
    return True


def report(sources, old_lv, old_souls, levels, souls):
    msgs = []
    for pid, src in sources.items():
        lv, ol = levels.get(pid, 0), old_lv.get(pid, 0)
        if lv != ol:
            msgs.append("%s: Lv %d -> Lv %d%s" % (src["name"], ol, lv, "  (done, removed from bot)" if lv >= 3 else ""))
        elif lv < 3 and souls.get(pid, 0) != old_souls.get(pid, 0):
            have, need = progress(src, lv, souls.get(pid, 0))
            msgs.append("%s: %d/%d souls to %s" % (src["name"], have, need, "unlock" if lv == 0 else "Lv %d" % (lv + 1)))
    for m in msgs:
        log(m)
    return msgs


MAP = os.path.join(HERE, "pet_map.json")
LOCATOR = os.path.join(QL_DIR, "self-locator.json")
CELL = 12000  # size of one farming area on the map, in game units


def current_map():
    """The map Questlog last saw you on (self-locator 'everywhere'), or None."""
    try:
        ks = list((read_json(LOCATOR).get("everywhere") or {}).keys())
        return ks[0] if ks else None
    except (OSError, ValueError):
        return None


def in_poly(x, y, poly):
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def plan_next(mapdata, sources, levels, souls, race, here):
    """Best spot to farm: the map area with the most spawns of needed monsters, all pets counted
    equally. Prefers the map you are on, then your own faction's maps, then Reshanta, then the
    enemy side."""
    if not mapdata:
        return None
    weight, for_pets = {}, {}
    for pid, src in sources.items():
        lv = levels.get(pid, 0)
        if lv >= 3:
            continue
        have, need = progress(src, lv, souls.get(pid, 0))
        w = 1.0  # every needed monster counts the same: the best spot is simply where most of them are
        for m in src["monsters"]:
            if m in SKIPPED:
                continue
            if w > weight.get(m, 0):
                weight[m] = w
            for_pets.setdefault(m, set()).add((src["name"], have, need))
    worlds = mapdata["worlds"]

    def factor(w):
        f = worlds[w]["f"]
        side = 1.0 if w == here else 0.8 if f == race else 0.5 if f == "abyss" else 0.25
        return side

    cells = {}
    for m, pts in mapdata["spawns"].items():
        if m not in weight:
            continue
        for w, x, y in pts:
            if w not in worlds:
                continue
            k = (w, int(x // CELL), int(y // CELL))
            c = cells.setdefault(k, [0.0, []])
            c[0] += weight[m]
            c[1].append((m, x, y))
    best, best_score = None, 0
    for (w, cx, cy), c in cells.items():
        score = sum(cells.get((w, cx + dx, cy + dy), [0])[0] for dx in (-1, 0, 1) for dy in (-1, 0, 1)) * factor(w)
        if score > best_score:
            best, best_score = (w, cx, cy), score
    if not best:
        return None
    w, cx, cy = best
    pts = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1) for p in cells.get((w, cx + dx, cy + dy), [0, []])[1]]
    x = sum(p[1] for p in pts) / len(pts)
    y = sum(p[2] for p in pts) / len(pts)
    area = None
    for r in worlds[w]["regions"]:
        if any(in_poly(x, y, poly) for poly in r["p"]):
            area = r["n"]
            if r["k"] == "battlefield":
                break
    if not area:
        labeled = [r for r in worlds[w]["regions"] if r.get("l")]
        area = min(labeled, key=lambda r: (r["l"][0] - x) ** 2 + (r["l"][1] - y) ** 2)["n"] if labeled else ""
    pets = {}
    for m, _, _ in pts:
        for p in for_pets[m]:
            pets[p] = pets.get(p, 0) + 1
    top = sorted(pets, key=lambda p: (p[2] - p[1], -pets[p]))[:3]
    side = "your side" if worlds[w]["f"] == race else "Abyss" if worlds[w]["f"] == "abyss" else "enemy side"

    # The exact spot to stand: the densest small patch of needed spawns inside the area
    hot = {}
    for m, px_, py_ in pts:
        k = (int(px_ // HOT_CELL), int(py_ // HOT_CELL))
        hot.setdefault(k, []).append((m, px_, py_))
    hk = max(hot, key=lambda k: sum(len(hot.get((k[0] + dx, k[1] + dy), [])) for dx in (-1, 0, 1) for dy in (-1, 0, 1)))
    hpts = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1) for p in hot.get((hk[0] + dx, hk[1] + dy), [])]
    hx = sum(p[1] for p in hpts) / len(hpts)
    hy = sum(p[2] for p in hpts) / len(hpts)
    hot_counts = {}
    for m, _, _ in hpts:
        hot_counts[m] = hot_counts.get(m, 0) + 1

    # Nearest named teleport point (or other landmark) to that spot, with distance and direction
    marks = [l for l in mapdata.get("landmarks", []) if l[0] == w]
    tele = [l for l in marks if l[3] == "teleport" and l[4] not in ("Abyss Portal",)] or marks
    mark = None
    if tele:
        l = min(tele, key=lambda l: (l[1] - hx) ** 2 + (l[2] - hy) ** 2)
        dx, dy = hx - l[1], hy - l[2]
        mark = {"name": l[4], "type": l[3], "x": l[1], "y": l[2],
                "m": int(round((dx * dx + dy * dy) ** 0.5 / 100.0)), "dir": compass(dx, dy)}
    # Where the spot sits inside its named area ("south-east part of Immortal Isle")
    part = ""
    for r in worlds[w]["regions"]:
        if r["n"] == area and r.get("l"):
            ddx, ddy = hx - r["l"][0], hy - r["l"][1]
            part = "middle" if (ddx * ddx + ddy * ddy) ** 0.5 < 4000 else compass(ddx, ddy) + " part"
            break
    return {"world": w, "zone": worlds[w]["name"], "side": side, "area": area, "x": x, "y": y,
            "pets": top, "monsters": sorted({m for m, _, _ in pts}), "spawns": len(pts),
            "hx": hx, "hy": hy, "hot": hot_counts, "mark": mark, "part": part}


HOT_CELL = 2500  # the exact farm spot is the densest ~75 m patch of needed spawns


def compass(dx, dy):
    """Direction of (dx, dy) on the map, north up."""
    import math
    if abs(dx) < 1 and abs(dy) < 1:
        return "here"
    names = ["east", "north-east", "north", "north-west", "west", "south-west", "south", "south-east"]
    return names[int(round(math.degrees(math.atan2(dy, dx)) / 45.0)) % 8]


class Sync:
    """One check of the Questlog files; keeps what the overlay shows."""

    def __init__(self):
        self.sources = read_json(SOURCES)
        self.mapdata = read_json(MAP) if os.path.exists(MAP) else None
        self.last_stamp, self.last = None, None
        self.recent = []      # latest soul / level messages, newest first
        self.closest = []     # (name, have, need, goal) of the pets nearest their next level
        self.who = ""
        self.plan = None      # where to farm next (see plan_next)
        self.race = "dark"
        self.needed_monsters = set()
        self.needed_all = {}  # every monster a still-needed pet drops from -> those pets, skipped or not
        self.force = False    # set by the overlay buttons: rebuild the bot list on the next step

    def toggle(self, monster):
        """Overlay button: skip this monster for now, or hunt it again."""
        ignored = load_ignored()
        ignored.symmetric_difference_update({monster})
        save_ignored(ignored)
        log("%s: %s" % (monster, "skipped for now" if monster in ignored else "hunting again"))
        self.force = True

    def step(self):
        stamp = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (TRACKER, CHARACTERS, LOCATOR))
        if stamp == self.last_stamp and not self.force:
            return
        who, levels, souls = load_state()
        here = current_map()
        try:
            self.race = (read_json(TRACKER).get("character") or {}).get("race") or self.race
        except (OSError, ValueError):
            pass
        if who and (self.force or (levels, souls) != self.last or here != getattr(self, "here", None)):
            self.force = False
            self.here = here
            if self.last is not None:
                stamp_txt = datetime.now().strftime("%H:%M")
                self.recent = [stamp_txt + "  " + m for m in report(self.sources, self.last[0], self.last[1], levels, souls)] + self.recent
                self.recent = self.recent[:6]
            block, needed = build_block(self.sources, levels, souls, who, here)  # also fills SKIPPED
            if write_block(block):
                log("chant2.ini updated: %d pets still needed (%s)" % (len(needed), who))
                schedule_rearm()
            self.plan = plan_next(self.mapdata, self.sources, levels, souls, self.race, here)
            self.needed_monsters = {m for pid, s in self.sources.items() if levels.get(pid, 0) < 3
                                    for m in s["monsters"] if m not in SKIPPED}
            self.needed_all = {}
            for pid, s in self.sources.items():
                if levels.get(pid, 0) < 3:
                    for m in s["monsters"]:
                        self.needed_all.setdefault(m, []).append(s["name"])
            # Overlay list: pets you have souls for, fewest souls to go first; untouched ones after
            rows = []
            for pid in needed:
                lv = levels.get(pid, 0)
                have, need = progress(self.sources[pid], lv, souls.get(pid, 0))
                rows.append((have == 0, need - have, self.sources[pid]["name"], have, need, "unlock" if lv == 0 else "Lv %d" % (lv + 1)))
            self.closest = [r[2:] for r in sorted(rows)[:8]]
            self.who = "%s · %d pets left" % (who, len(needed))
            self.last = (levels, souls)
        self.last_stamp = stamp


PIDFILE = os.path.join(HERE, "pet_sync.pid")


def claim_instance():
    """Mark this pet sync as the current one; any older one sees this and closes itself."""
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def replaced():
    try:
        with open(PIDFILE) as f:
            return f.read().strip() != str(os.getpid())
    except OSError:
        return False


def run_console(sync):
    while True:
        if replaced():
            log("A newer pet sync started; closing this one")
            return
        try:
            sync.step()
        except (OSError, ValueError, RuntimeError) as e:
            # Questlog may be mid-write or the bot may hold the file; try again next round.
            log("Waiting: %s" % e)
            time.sleep(CHECK_EVERY_SECONDS * 2)
        time.sleep(CHECK_EVERY_SECONDS)


_HOTKEYS = []


def hotkey_queue():
    """Global hotkeys Ctrl+Alt+1..9 (work while the game has focus), registered once per process."""
    if _HOTKEYS:
        return _HOTKEYS[0]
    import queue
    import threading
    q = queue.Queue()
    _HOTKEYS.append(q)

    def run():
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        # An older pet sync may still be closing and holding the keys for a moment: retry a few times
        missing = list(range(1, 10))
        for attempt in range(8):
            missing = [i for i in missing if not u32.RegisterHotKey(None, i, 0x0001 | 0x0002 | 0x4000, 0x30 + i)]  # Alt|Ctrl|NoRepeat
            if not missing:
                break
            time.sleep(1)
        for i in missing:
            log("Hotkey Ctrl+Alt+%d is taken by another program" % i)
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                q.put(msg.wParam)

    if os.name == "nt":
        threading.Thread(target=run, daemon=True).start()
    return q


def run_overlay(sync, master=None):
    """Small always-on-top window with live souls. Drag it anywhere.
    Standalone (no master): it also drives pet sync and closing it stops pet sync.
    Inside the app (master given): the app drives pet sync; this is just a window you can close."""
    import tkinter as tk
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.title("Pet Souls")
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.88)
    except tk.TclError:
        pass
    root.configure(bg="#121715")
    root.geometry("+40+120")
    if os.name == "nt":
        # Never take keyboard focus from the game (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW), so the
        # game stays the active window and its keys keep working while the overlay is up.
        try:
            import ctypes
            root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
        except Exception:
            pass
    head = tk.Label(root, font=("Segoe UI", 9, "bold"), fg="#4cc39a", bg="#121715", anchor="w", justify="left")
    head.pack(fill="x", padx=10, pady=(8, 2))

    # --- NEARBY: what you are targeting + needed monsters loaded around you, with Hunt/Skip ---
    now_lbl = tk.Label(root, font=("Segoe UI", 9, "bold"), fg="#e3e9e4", bg="#121715", anchor="w", justify="left")
    now_lbl.pack(fill="x", padx=10, pady=(4, 0))
    near = tk.Frame(root, bg="#121715")
    near.pack(fill="x", padx=10)
    near_note = tk.Label(root, font=("Segoe UI", 8), fg="#93a19a", bg="#121715", anchor="w", justify="left")
    near_note.pack(fill="x", padx=10, pady=(0, 4))
    near_key = {"k": None}
    near_rows = []  # monster names in the order shown, for the Ctrl+Alt+number hotkeys
    npc_names = (sync.mapdata or {}).get("npc_names", {})

    # Global hotkeys, so the overlay works while the game has the mouse: Ctrl+Alt+1..8 = switch
    # Hunting/Skipped for that NEARBY row, Ctrl+Alt+9 = zoom the map in/out.
    hotkeys = hotkey_queue()

    def handle_hotkeys():
        if not root.winfo_exists():
            return
        while not hotkeys.empty():
            k = hotkeys.get()
            if k == 9:
                switch_view()
            elif 1 <= k <= len(near_rows):
                name = near_rows[k - 1]
                if name not in CLASHED:
                    sync.toggle(name)
                    near_key["k"] = None
        root.after(150, handle_hotkeys)

    def draw_nearby():
        tgt = LIVE["target"]
        tname = npc_names.get(LIVE["alive"].get(tgt)) if tgt else None
        if not tgt:
            now_lbl.config(text="TARGETING: nothing")
        elif not tname:
            now_lbl.config(text="TARGETING: a monster no pet needs")
        else:
            state = "skipped" if tname in SKIPPED else "needed" if tname in sync.needed_all else "not needed"
            now_lbl.config(text="TARGETING: %s (%s)" % (tname, state))
        counts = {}
        for npc in list(LIVE["alive"].values()):
            n = npc_names.get(npc)
            if n in sync.needed_all:
                counts[n] = counts.get(n, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
        key = tuple((n, c, n in SKIPPED, n in CLASHED) for n, c in rows)
        if key == near_key["k"]:
            return
        near_key["k"] = key
        for w in near.winfo_children():
            w.destroy()
        if not rows:
            tk.Label(near, text="No needed monsters loaded around you", font=("Segoe UI", 9), fg="#93a19a",
                     bg="#121715", anchor="w").pack(fill="x")
        near_rows[:] = [n for n, c, skipped, clashed in key]
        for i, (n, c, skipped, clashed) in enumerate(key, 1):
            row = tk.Frame(near, bg="#121715")
            row.pack(fill="x", pady=1)
            tk.Label(row, text="%d  %s ×%d" % (i, n[:26], c), font=("Consolas", 9), fg="#e3e9e4", bg="#121715",
                     anchor="w").pack(side="left")
            if clashed:
                tk.Label(row, text="look-alike", font=("Segoe UI", 8), fg="#93a19a", bg="#121715").pack(side="right")
            else:
                tk.Button(row, text="Skipped" if skipped else "Hunting", width=8, relief="flat",
                          font=("Segoe UI", 8, "bold"), fg="#0d1411" if not skipped else "#e3e9e4",
                          bg="#4cc39a" if not skipped else "#3c4a43", activebackground="#2a352f",
                          command=lambda n=n: (sync.toggle(n), near_key.update(k=None))).pack(side="right")
        near_note.config(text="Ctrl+Alt+row number (or click) switches Hunting/Skipped,\nthen press Numpad 8 again. Ctrl+Alt+9: zoom map." if rows else "Ctrl+Alt+9: zoom map")

    body = tk.Label(root, font=("Consolas", 10), fg="#e3e9e4", bg="#121715", anchor="w", justify="left")
    body.pack(fill="x", padx=10)
    feed = tk.Label(root, font=("Consolas", 9), fg="#f08a4b", bg="#121715", anchor="w", justify="left")
    feed.pack(fill="x", padx=10, pady=(4, 4))
    nxt = tk.Label(root, font=("Segoe UI", 9), fg="#e3e9e4", bg="#121715", anchor="w", justify="left", wraplength=300)
    nxt.pack(fill="x", padx=10, pady=(4, 2))
    W, H = 300, 240
    view = {"mode": "spot"}  # "spot" = close-up of the FARM NEXT area, "zone" = the whole map
    viewbtn = tk.Button(root, text="Show whole zone", relief="flat", font=("Segoe UI", 8), fg="#e3e9e4",
                        bg="#2a352f", activebackground="#3c4a43", command=lambda: switch_view())
    viewbtn.pack(anchor="e", padx=10)
    cv = tk.Canvas(root, width=W, height=H, bg="#0e1311", highlightthickness=0)
    cv.pack(padx=10, pady=(2, 2))
    legend = tk.Frame(root, bg="#121715")
    legend.pack(fill="x", padx=10, pady=(0, 10))
    drawn = {"key": None}
    COLORS = ["#f08a4b", "#4cc39a", "#6aa9ff", "#e5c25b", "#d778c7", "#ff6b6b", "#9be38f", "#c9c9c9"]

    def switch_view():
        view["mode"] = "zone" if view["mode"] == "spot" else "spot"
        viewbtn.config(text="Show whole zone" if view["mode"] == "spot" else "Zoom to farm spot")
        drawn["key"] = None
        draw_map(sync.plan)

    def draw_map(plan):
        key = plan and (view["mode"], plan["world"], round(plan["x"]), round(plan["y"]), tuple(plan["monsters"]))
        if key == drawn["key"]:
            return
        drawn["key"] = key
        cv.delete("all")
        for w in legend.winfo_children():
            w.destroy()
        if not plan or not sync.mapdata:
            cv.create_text(W / 2, H / 2, text="No map data", fill="#93a19a")
            return
        world = sync.mapdata["worlds"][plan["world"]]
        spot = set(plan["monsters"])
        colors = {m: COLORS[i % len(COLORS)] for i, m in enumerate(sorted(spot))}
        if view["mode"] == "spot":
            # Frame the needed spawns around the farm spot (the 3x3 cells plan_next scored)
            near = [(x, y) for m in spot for w, x, y in sync.mapdata["spawns"].get(m, [])
                    if w == plan["world"] and abs(x - plan["x"]) <= CELL * 1.6 and abs(y - plan["y"]) <= CELL * 1.6]
            half = max(max([abs(x - plan["x"]) for x, _ in near] + [abs(y - plan["y"]) for _, y in near] + [0]) * 1.8, 20000)
            x0, x1, y0, y1 = plan["x"] - half, plan["x"] + half, plan["y"] - half, plan["y"] + half
        else:
            x0, y0, x1, y1 = world["b"]
        s = min((W - 12) / (x1 - x0), (H - 12) / (y1 - y0))
        ox, oy = (W - (x1 - x0) * s) / 2, (H - (y1 - y0) * s) / 2
        to = lambda x, y: (ox + (x - x0) * s, oy + (y1 - y) * s)   # map north is up
        # Areas: battlefields filled, villages outlined; the farm area highlighted
        for r in world["regions"]:
            fill = "#1d3a2f" if r["n"] == plan["area"] else "#16211c" if r["k"] == "battlefield" else ""
            for poly in r["p"]:
                if len(poly) > 2:
                    cv.create_polygon(*[c for p in poly for c in to(*p)], outline="#3c4a43", fill=fill)
        for r in world["regions"]:
            if r.get("l") and x0 < r["l"][0] < x1 and y0 < r["l"][1] < y1 and (view["mode"] == "spot" or r["k"] == "battlefield"):
                lx, ly = to(*r["l"])
                cv.create_text(lx, ly, text=r["n"], fill="#6f8178" if view["mode"] == "zone" else "#93a19a",
                               font=("Segoe UI", 6 if view["mode"] == "zone" else 7))
        # Landmarks you can see in game: teleport points (▲), flight circles (○), portals (□)
        for wl, lx_, ly_, kind, name in sync.mapdata.get("landmarks", []):
            if wl != plan["world"] or not (x0 <= lx_ <= x1 and y0 <= ly_ <= y1):
                continue
            qx, qy = to(lx_, ly_)
            if kind == "teleport":
                cv.create_polygon(qx, qy - 5, qx - 4, qy + 3, qx + 4, qy + 3, fill="#6aa9ff", outline="")
            elif kind == "flight":
                cv.create_oval(qx - 3, qy - 3, qx + 3, qy + 3, outline="#e5c25b")
            else:
                cv.create_rectangle(qx - 2, qy - 2, qx + 2, qy + 2, outline="#c9c9c9")
            if view["mode"] == "spot" and kind == "teleport":
                cv.create_text(qx, qy + 9, text=name, fill="#6aa9ff", font=("Segoe UI", 7))
        counts = {}
        for m, pts in sync.mapdata["spawns"].items():
            hot = m in spot
            if not hot and m not in sync.needed_monsters:
                continue
            for w, x, y in pts:
                if w != plan["world"] or not (x0 <= x <= x1 and y0 <= y <= y1):
                    continue
                px, py = to(x, y)
                if hot:
                    counts[m] = counts.get(m, 0) + 1
                    r = 3 if view["mode"] == "spot" else 2
                    cv.create_oval(px - r, py - r, px + r, py + r, fill=colors[m], outline="")
                else:
                    cv.create_oval(px - 1, py - 1, px + 1, py + 1, fill="#5b6a62", outline="")
        px, py = to(plan["x"], plan["y"])
        if view["mode"] == "zone":
            rr = max(10, CELL * 1.5 * s)
            cv.create_oval(px - rr, py - rr, px + rr, py + rr, outline="#4cc39a", width=2)
            cv.create_text(px, py - rr - 7, text=plan["area"], fill="#4cc39a", font=("Segoe UI", 8, "bold"))
        else:
            # The exact spot to stand, plus an arrow to the nearest teleport point
            hx, hy = to(plan.get("hx", plan["x"]), plan.get("hy", plan["y"]))
            rr = max(8, HOT_CELL * 1.5 * s)
            cv.create_oval(hx - rr, hy - rr, hx + rr, hy + rr, outline="#4cc39a", width=2)
            cv.create_line(hx - 7, hy, hx + 7, hy, fill="#4cc39a", width=2)
            cv.create_line(hx, hy - 7, hx, hy + 7, fill="#4cc39a", width=2)
            cv.create_text(hx, hy - rr - 7, text="FARM HERE", fill="#4cc39a", font=("Segoe UI", 8, "bold"))
            mk = plan.get("mark")
            if mk:
                tx, ty = to(mk["x"], mk["y"])
                inside = 0 <= tx <= W and 0 <= ty <= H
                if not inside:
                    # clip the line from the spot toward the teleport at the map edge
                    t = min([v for v in (
                        (W - 4 - hx) / (tx - hx) if tx > hx else (4 - hx) / (tx - hx) if tx < hx else 9,
                        (H - 4 - hy) / (ty - hy) if ty > hy else (4 - hy) / (ty - hy) if ty < hy else 9) if v > 0] + [1])
                    tx, ty = hx + (tx - hx) * t, hy + (ty - hy) * t
                cv.create_line(hx, hy, tx, ty, fill="#6aa9ff", dash=(4, 3), arrow="first" if inside else "last")
                if not inside:
                    cv.create_text(min(max(tx, 60), W - 60), min(max(ty + (-10 if ty > H / 2 else 10), 8), H - 8),
                                   text="%s ▲ %s" % (mk["name"], ("%.1f km" % (mk["m"] / 1000.0)) if mk["m"] >= 1000 else "%d m" % mk["m"]),
                                   fill="#6aa9ff", font=("Segoe UI", 7, "bold"))
        cv.create_text(6, H - 6, text=world["name"] + (" · " + plan["area"] if view["mode"] == "spot" else ""),
                       fill="#93a19a", anchor="sw", font=("Segoe UI", 8))
        for m in sorted(counts, key=lambda m: -counts[m]):
            row = tk.Frame(legend, bg="#121715")
            row.pack(fill="x")
            tk.Label(row, text="●", fg=colors[m], bg="#121715", font=("Segoe UI", 9)).pack(side="left")
            pets = ", ".join(sorted(set(sync.needed_all.get(m, []))))[:34]
            tk.Label(row, text="%s ×%d  (%s)" % (m, counts[m], pets), fg="#e3e9e4", bg="#121715",
                     font=("Segoe UI", 8), anchor="w").pack(side="left")

    def tick():
        if not root.winfo_exists():
            return
        if master is None:
            if replaced():
                log("A newer pet sync started; closing this one")
                root.destroy()
                return
            try:
                sync.step()
            except (OSError, ValueError, RuntimeError) as e:
                log("Waiting: %s" % e)
        head.config(text=sync.who or "Waiting for Questlog…")
        body.config(text="\n".join("%-26s %3d/%-3d %s" % (n[:26], h, need, goal) for n, h, need, goal in sync.closest) or "No pets left to farm")
        feed.config(text="\n".join(sync.recent) or "Soul gains show here")
        p = sync.plan
        if p:
            here = " (you are here)" if p["world"] == getattr(sync, "here", None) else ""
            pets = ", ".join("%s %d/%d" % pt for pt in p["pets"])
            mk = p.get("mark")
            route = ""
            if mk:
                dist = ("%.1f km" % (mk["m"] / 1000.0)) if mk["m"] >= 1000 else "%d m" % mk["m"]
                route = "\nFrom teleport point \"%s\": go ~%s %s" % (mk["name"], dist, mk["dir"])
            spot = ", ".join("%s ×%d" % kv for kv in sorted(p.get("hot", {}).items(), key=lambda kv: -kv[1]))
            nxt.config(text="FARM NEXT: %s%s · %s of %s (%s)%s\nAt the spot: %s" % (
                p["zone"], here, p.get("part") or "in", p["area"], p["side"], route, spot))
        else:
            nxt.config(text="FARM NEXT: nothing left to farm")
        draw_map(p)
        draw_nearby()
        root.after(CHECK_EVERY_SECONDS * 1000, tick)

    tick()
    handle_hotkeys()
    if master is None:
        root.mainloop()
    return root


SHOW_OVERLAY = True

# ---- Loot after kills -------------------------------------------------------------------------
# The bot's own F presses are ignored while AION2 is the active window. This presses F as real
# keyboard input instead, a moment after Questlog's combat log shows that you killed a monster.
LOOT_AFTER_KILLS = True
LOOT_PRESSES = 2           # F presses per kill
LOOT_DELAY = 0.2           # seconds to wait after the kill before the first press
LOOT_GAP = 0.15            # seconds between presses
GAME_EXE = "aion2.exe"
COMBAT_LOGS = os.path.join(QL_DIR, "combat-logs")


def _win32():
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    def game_focused():
        hwnd = u32.GetForegroundWindow()
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            buf, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return bool(ok) and os.path.basename(buf.value).lower() == GAME_EXE
        finally:
            k32.CloseHandle(h)

    def press_f():
        # Scan code input (F = 0x21): games that read the real keyboard accept this.
        down = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, 0x21, 0x0008, 0, 0)))
        up = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, 0x21, 0x0008 | 0x0002, 0, 0)))
        u32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.06)
        u32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))

    def press_numpad8():
        # Virtual-key input (VK_NUMPAD8 = 0x68, scan 0x48), the key the bot listens for to start Pet Farm
        down = INPUT(type=1, u=_U(ki=KEYBDINPUT(0x68, 0x48, 0, 0, 0)))
        up = INPUT(type=1, u=_U(ki=KEYBDINPUT(0x68, 0x48, 0x0002, 0, 0)))
        u32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.06)
        u32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))

    _win32.press_numpad8 = press_numpad8
    _win32.game_focused = game_focused
    return game_focused, press_f


# ---- Restart Pet Farm after the bot reloads its list --------------------------------------------
# The bot stops Pet Farm whenever chant2.ini changes. When the list changes while you are farming
# (you killed something in the last REARM_IF_KILLED_WITHIN seconds and AION2 is the active window),
# press Numpad 8 once after REARM_DELAY seconds so farming carries on by itself.
AUTO_REARM = True
REARM_DELAY = 2.0
REARM_IF_KILLED_WITHIN = 90


def schedule_rearm():
    if not AUTO_REARM or os.name != "nt":
        return
    import threading

    def rearm():
        if not getattr(_win32, "press_numpad8", None):
            _win32()
        if time.time() - LIVE.get("last_kill", 0) > REARM_IF_KILLED_WITHIN:
            log("Bot list changed - not farming right now, so press Numpad 8 yourself when you start")
            return
        if not _win32.game_focused():
            log("Bot list changed - AION2 isn't the active window, so press Numpad 8 yourself")
            return
        _win32.press_numpad8()
        log("Bot list changed - pressed Numpad 8 to restart Pet Farm")

    threading.Timer(REARM_DELAY, rearm).start()


def newest_combat_log():
    try:
        files = [os.path.join(COMBAT_LOGS, f) for f in os.listdir(COMBAT_LOGS) if f.endswith(".ndjson")]
        return max(files, key=os.path.getmtime) if files else None
    except OSError:
        return None


# What the combat log says is around you right now (read by the overlay)
LIVE = {"alive": {}, "target": None, "me": None}


def combat_watcher():
    """Follows Questlog's live combat log: keeps track of the monsters loaded around you and what
    you are targeting, and (LOOT_AFTER_KILLS) presses F after each monster you kill while AION2
    is the active window."""
    game_focused, press_f = _win32() if os.name == "nt" else (lambda: False, None)
    path, pos, buf = None, 0, ""
    kills = 0

    def handle(o, live):
        nonlocal kills
        t = o.get("type")
        alive = LIVE["alive"]
        if t == "player" and o.get("self"):
            LIVE["me"] = o.get("entity", LIVE["me"])
        elif t == "map":
            alive.clear()
            LIVE["target"] = None
        elif t == "spawn" and o.get("npc"):
            alive[o.get("entity")] = str(o["npc"])
        elif t == "removed":
            alive.pop(o.get("entity"), None)
        elif t == "target" and o.get("entity") == LIVE["me"]:
            LIVE["target"] = o.get("target") or None
        elif t == "death":
            dead = o.get("entity")
            was_npc = alive.pop(dead, None) is not None
            if LIVE["target"] == dead:
                LIVE["target"] = None
            if live and was_npc and o.get("killer") == LIVE["me"]:
                LIVE["last_kill"] = time.time()
            if live and LOOT_AFTER_KILLS and was_npc and o.get("killer") == LIVE["me"] and game_focused():
                time.sleep(LOOT_DELAY)
                for _ in range(LOOT_PRESSES):
                    press_f()
                    time.sleep(LOOT_GAP)
                kills += 1
                if kills % 25 == 1:
                    log("Looting after kills is working (%d kills so far)" % kills)

    def lines_of(text):
        for line in text:
            try:
                yield json.loads(line)
            except ValueError:
                continue

    while True:
        try:
            newest = newest_combat_log()
            if newest and newest != path:
                path, buf = newest, ""
                LIVE["alive"].clear()
                # Catch up on what is already in the file (who you are, what is around), no looting
                with open(path, encoding="utf-8", errors="replace") as f:
                    for o in lines_of(f):
                        handle(o, False)
                    pos = f.tell()
            if path:
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                buf += chunk
                parts = buf.split("\n")
                buf = parts.pop()
                for o in lines_of(parts):
                    handle(o, True)
        except Exception as e:  # never let the watcher take pet sync down
            log("Combat log watcher waiting: %s" % e)
            time.sleep(2)
        if replaced():
            return
        time.sleep(0.15)


def main():
    claim_instance()
    log("Pet sync started. Watching %s" % QL_DIR)
    import threading
    threading.Thread(target=combat_watcher, daemon=True).start()
    if LOOT_AFTER_KILLS and os.name == "nt":
        log("Loot after kills: on (presses F when AION2 is the active window)")
    sync = Sync()
    if SHOW_OVERLAY:
        try:
            run_overlay(sync)
            return
        except Exception as e:  # no display / tkinter missing: fall back to the console
            log("Overlay unavailable (%s); using the console only" % e)
    run_console(sync)


SETTINGS_FILE = os.path.join(HERE, "pet_settings.json")
SETTING_NAMES = ["PET_RANGE", "LOOT_AFTER_KILLS", "LOOT_PRESSES", "LOOT_DELAY", "LOOT_GAP", "SHOW_OVERLAY",
                 "AUTO_REARM", "REARM_DELAY", "AUTO_MOVE", "ZONE_ONLY", "FOCUS", "FARM_SPOT"]


def current_settings():
    g = globals()
    return {k: g[k] for k in SETTING_NAMES}


def apply_settings(values):
    g = globals()
    for k, v in values.items():
        if k in SETTING_NAMES:
            g[k] = v


def save_settings(values):
    apply_settings(values)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current_settings(), f, indent=1)


try:  # settings saved from the Pet Farm Helper app override the defaults above
    with open(SETTINGS_FILE, encoding="utf-8") as _f:
        apply_settings(json.load(_f))
except (OSError, ValueError):
    pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
