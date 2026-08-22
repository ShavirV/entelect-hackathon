"""
Age of Enteland - Level 4 solver.

Level 4 unlocks (in addition to Levels 1-3): upkeep.

Upkeep costs 5 ticks and doubles the current town's Enteloot production for
50 ticks (75 with that town's fire-station built), refreshing rather than
stacking on repeat triggers. It doesn't add a scoring term of its own - it
only helps indirectly, by generating extra Enteloot that can fund more
upgrades (the actual score driver). So the only sensible use of upkeep is
as a cheap income booster woven into the same build tour Level 2/3 already
do, not as a goal in itself.

WHAT THIS ADDS ON TOP OF LEVEL 3
---------------------------------
1. `use_upkeep=True` is passed into the shared planner: every time the tour
   is already at a town to build its upgrades, it also spends 5 ticks
   triggering that town's upkeep boost before moving on. Town trickle fires
   globally regardless of location (Assumption 5), so the boosted window
   keeps paying out after we've left - extra funding for upgrades built
   later in the same tour, for a fraction of a build's tick cost.
2. If the full candidate upgrade list (every upgrade in every town) is
   exhausted with ticks and Enteloot still unused, there is nothing further
   to invest in - hoarding Enteloot scores far less than investing it, so
   the plan stops there rather than padding the tail with upkeep actions
   that wouldn't fund anything.
"""

import json

from engine import load_json
from common_solver import candidate_upgrade_order, plan_feasible_prefix, replay, plan_income_tail
from solver_level3 import find_crafting_hub, mine_reachable, print_result


def main():
    constants = load_json("resources.json")
    level = load_json("4.txt")
    level_number = 4

    start = level["run"]["starting_town"]
    hub = find_crafting_hub(level)

    print("Level 4 solver")
    print("==============")
    print("Starting town:", start)
    print("Crafting hub:", hub)
    print("Tick budget:", level["run"]["total_ticks"])
    print("Starting Enteloot:", level["run"]["starting_enteloot"])

    have_mine = mine_reachable(level, hub)
    tools_planned = ["boots", "pickaxe"] if have_mine else []
    extra_target_items = [("boots", 1), ("pickaxe", 1)] if have_mine else None
    print("Mine node reachable from hub:", have_mine)

    ordered_pairs = candidate_upgrade_order(constants, list(level["towns"].keys()), level_number)
    print("Candidate (town, upgrade) pairs (priority order):", len(ordered_pairs))

    actions, meta, kept_pairs = plan_feasible_prefix(
        constants, level, level_number, ordered_pairs, hub,
        extra_target_items=extra_target_items,
        use_upkeep=True,
    )

    result, invalid = replay(constants, level, level_number, actions)
    result["_total_ticks"] = level["run"]["total_ticks"]

    # See solver_level3.py for why this is capped rather than on/off.
    MAX_TAIL_TICKS = 3000
    if not invalid and result["final_tick"] < level["run"]["total_ticks"]:
        idle = level["run"]["total_ticks"] - result["final_tick"]
        print(f"\nAdding capped tail income phase (idle ticks: {idle}, "
              f"using up to {min(idle, MAX_TAIL_TICKS)})...")
        actions, result = plan_income_tail(
            constants, level, level_number, hub, actions,
            use_upkeep=True,  # Level 4 has upkeep - trigger it at hub for bonus
            max_tail_ticks=MAX_TAIL_TICKS,
        )
        # Re-verify the combined plan
        result, invalid = replay(constants, level, level_number, actions)
        result["_total_ticks"] = level["run"]["total_ticks"]

    print_result(result, meta, kept_pairs, tools_planned)

    print()
    print("Generated actions:", len(actions))
    print("Invalid actions:", len(invalid))
    if invalid:
        print("First invalid actions:")
        for entry in invalid[:10]:
            print(" ", entry["tick"], entry["action"], "->", entry["detail"])

    remaining = level["run"]["total_ticks"] - result["final_tick"]
    print()
    print(f"Candidates built: {len(kept_pairs)} / {len(ordered_pairs)}")
    print(f"Unused ticks: {remaining}",
          "(candidate list exhausted - nothing left to invest in)"
          if len(kept_pairs) == len(ordered_pairs) else "")

    with open("level4_actions.txt", "w") as f:
        json.dump({"actions": actions}, f, indent=2)
    print()
    print("Wrote level4_actions.txt")

    if invalid:
        print("WARNING: plan contains invalid actions - this should not happen.")
        return 2
    if result["final_tick"] > level["run"]["total_ticks"]:
        print("ERROR: tick budget exceeded.")
        return 3

    print("Plan replayed successfully with zero invalid actions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())