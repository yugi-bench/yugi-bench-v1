# Sample puzzle

A small, self-contained puzzle used as the worked example inside every
benchmark prompt. It is **not** part of the 217-puzzle benchmark — it exists
only so the prompt can show the action format without leaking from the
evaluation set.

- `puzzle.lua` — the Lua setup (hand, field, graveyard, LP)
- `solution.json` — a complete winning action sequence

The solution is verified to end the duel with `winner == 0` (player). For
a programmatic check from Python (run from the repo root after `setup.sh`,
which adds `src/` to the import path of every entry-point script):

```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from engine.core import (CardDB, OCGEngine, DB_DIR, DYLIB_PATH,
                         SCRIPT_DIR, CARD_SCRIPT_DIR)
from engine.replay import replay_solution

lua = open("sample/puzzle.lua").read()
actions = json.loads(open("sample/solution.json").read())
card_db = CardDB(Path(DB_DIR))
engine = OCGEngine(dylib_path=Path(DYLIB_PATH), card_db=card_db,
                   script_dir=Path(SCRIPT_DIR),
                   card_script_dir=Path(CARD_SCRIPT_DIR))
r = replay_solution(engine, lua, actions, lp0=1500, lp1=3100)
engine.destroy()
assert r.winner == 0, r.error
```

## What it demonstrates

| Mechanic              | Action type          | In the solution               |
|-----------------------|----------------------|-------------------------------|
| Activate a Spell      | `activate`           | Monster Reborn                |
| Change battle position| `repos`              | Mystical Elf DEF → ATK        |
| Normal Summon         | `summon` + `position`| La Jinn                       |
| Enter Battle Phase    | `to_battle_phase`    |                               |
| Attack (vs monster)   | `attack` + `target`  | Blue-Eyes vs Battle Ox, etc.  |
| Direct attack         | `attack` + `"DIRECT"`| Mystical Elf for the finisher |
| End turn              | `to_end_phase`       |                               |

The damage math:

- Blue-Eyes (3000) vs Battle Ox (1700): **1300 damage**
- La Jinn (1800) vs Mystical Elf (800 ATK): **1000 damage**
- Mystical Elf (800) direct attack: **800 damage**
- **Total: 3100 damage → opponent's 3100 LP → 0 = win**
