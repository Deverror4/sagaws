# Aion 2 Chanter bot: Pet Farm

Scripts and tools for automating Chanter in Aion 2 with Speeder (a macro tool with a line-based script language).

**Goal:** press one key (Numpad 8) and the character walks, fights, loots and travels to the next area by itself, including by kisk or teleport, to collect the pet souls still needed.

## Files

| File | What it is |
|---|---|
| `chant2.example.ini` | The bot script: keybinds, modes, Pet Farm. The real `chant2.ini` is not in the repo. |
| `config.example.txt` | Speeder settings. Each line number has a fixed meaning. Lines 2 and 3 are removed. |
| `pet_sync.py` | Reads Questlog's local pet data, works out what is still needed, and writes the `PET SYNC` block of `chant2.ini`. |
| `pet_app.py` | Desktop app: map, needed pets, nearby monsters. Also moves recorded routes into `routes/`. |
| `pet_map.json` | Spawn points and landmarks per map. Landmarks are `[world, x, y, kind, name]`. |
| `pet_sources.json` | Which monsters give which pet souls. |
| `routes/` | Waymark files recorded by Pet Farm. |

Not in the repo: `config.txt`, `chant1.ini` (encrypted; holds the combat rotations `fRotation`, `fPvPRotation` and others), logs.

## How the pieces connect

Data only flows one way: Questlog files → app/`pet_sync.py` → `chant2.ini` → bot. The bot reloads `chant2.ini` when it changes, and that reload **stops Pet Farm**. The app cannot see the player position live. Bot text drawn with `esp` does not appear in screenshots.

Coordinates: Questlog map `(x, y)` = game `(X, Z)`. Y is height.

Worlds in use: 1110 Altgard, 1010 Verteron, 20 Lower Reshanta, 22 Middle Reshanta.

## Working

Fighting, looting, and fight-back with `ibtM2000|tcmT2000`. Walking to monsters (`mtt`) is switched off with `AUTO_MOVE = False`, because it has no time limit and Numpad 8 could not stop it. With it off, Numpad 8 turns off again.

## Not working

0. **Attacking:** after `mtt` was switched off, Pet Farm no longer attacks needed monsters, only things that attack first. `keys3=tcm % names,6000|call % fRotation` is unchanged.
1. **Walking:** `mt` goes in a straight line, hits terrain, and stops after a few metres.
2. **Sliding:** the character slides. It began when the `mt` saved-place route was added, so that route is off (`LEARN_ROUTE = False`). Not yet confirmed as the cause.
3. **Far monsters:** monsters that are not loaded cannot be seen by `tcm`.
4. **Area limit:** coordinates on `tcm %` and a `get % target` box check both failed, and the bot stopped recognising monsters.
5. **Game auto-path:** `ap x,y,z` mostly did not start, including with a manual map right-click.
6. **Teleport/kisk use:** not built. The test on F4 has not been run. `config.txt` line 32 lists Siege, Alliance, Group, Personal and Legion Kisk. The menu after interacting is unknown.

## Numpad 8 lock-ups

Numpad 8 stops responding when something in the Pet Farm loop never returns. Seen with `mtt` and with `tcm ...,20000|call % fRotation` on far targets (the 20,000 version did get kills). At 6,000 it turns off. Current far-target handling is one short attack-key tap with `gt*`, relying on the game's Pursue Targets setting. Untested.

## Plan

Use Speeder's waymark system instead of `mt`. Each stage has to pass in the game before the next starts.

**Stage 1, built, untested:** F12 `[123]` records a route (`cc % -record 500,30`, then `cc % -record`). The app moves the file to `routes/` and copies the newest to `route_active.ini`. Config line 24 is F9, the movement pause key.

**Stage 2, built, untested:** F11 `[122]` plays it with `cc % -twrepeat* route_active.ini,?`. F11 again pauses with `wmark` and waits up to about 4 seconds for `WMS` 0.

**Stage 3, not built:** Pet Farm pauses the route before a fight and restarts it with `,?` after looting. Must only pause when `WMS` is 2 or more, must wait with a limit, and must not rewrite `chant2.ini` during a route, because a reload kills the macro while waymarks may keep walking.

**Stage 4, not built:** the app writes route files itself once the format is known, then area switching and kisk travel.

Rules: Numpad 8 stays the master switch, one thing moves the character at a time, and no unbounded waits or recording in `[104]`.

- `wmark?` sets `WMS`: 3 moving, 2 active and not moving, 1 exiting, 0 stopped.
- `[unstick]` and `[death]` sections handle stuck and dead. `[death]` example: `keys=lwf % resurrect.ini`.
- `tcg` plus the interact key makes the game run to a gathering object with its own pathing. Whether this works on monsters is untested.

The waymark file format is not known yet because the guide is members-only. The first recorded file will show it. Questlog files have no live position.

## Speeder script notes

- `[VK]` sections hold `keys`, `keys2`...; `gt*` ends the macro; `store`, `eq`, `add`, `sub`.
- `cmp(VAR % v),n` is true when `v <= n`. `!cmp` is the inverse.
- `st % timer,min,max` starts a timer; `ct % timer` is true once it has expired.
- `tcm % a;b;c,dist,height` targets the closest match; names match partially.
- `get % pos` fills `PX`, `PY`, `PZ`.
- `mt[x],[y],[z],[timeout]`, `mtt[how close]`, `ap[x],[y],[z]`, `ap?`.
- `itl` means loot is nearby; `dlg` means a dialogue is open; `cc % -command` runs a console command.
- Line 23 of the config is arrival distance. Line 24 is the pause key, and it is empty for now.
- `config.txt` changes need a bot restart; `chant2.ini` reloads itself.

## Ground rules for changes

- Do not edit inside the `PET SYNC` markers by hand. `pet_sync.py` regenerates that block, so change the generator.
- `config.txt` uses CRLF and fixed line numbers. `chant2.ini` uses LF.
- Things that broke the bot before: a `get % mobs` scan loop, coordinates on `tcm %`, the `ua` check for fight-back, and an `itm` guard that slowed attacks, and `cc % -record` in `[104]`.
