---
name: Puzzle proposal
about: Propose a new puzzle for the benchmark
title: "[puzzle] "
labels: puzzle, enhancement
assignees: ''
---

**Source**
Where the puzzle comes from (Duel Links, GX Spirit Caller, Nightmare
Troubadour, World Championship, community collection name, etc.).
Include a link or reference if public.

**Setup**
A `lua_setup` block that initialises the puzzle state in
`engine.replay`. Attach as a code block or as a file.

**Expected outcome**
- Win condition the model must achieve.
- Estimated complexity tier (1-10) and source kind.
- Whether you have a gold solution: yes / no.

**Gold solution (if any)**
A JSON action list (per the *Solution format* in the README) that the
engine replays to a `MSG_WIN` for the scoring player.

**Verification**
- [ ] I have run `python -m engine.replay --solutions <my-solution.json> --only <id>` and it passes
- [ ] The puzzle does not duplicate one already in `data/yugioh_bench.jsonl`
- [ ] No copyrighted card text is included verbatim (the harness fetches card descriptions from BabelCDB at install time)
