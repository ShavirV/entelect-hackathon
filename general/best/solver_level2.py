"""
Age of Enteland - Level 2 solver.

Level 2 unlocks: travel, buy, sell, gather, craft, build.

WHAT WAS WRONG BEFORE
----------------------
The previous solver requested EVERY production + civic upgrade in EVERY
town unconditionally (10 towns x 10 upgrades here = 109,000 Enteloot and
360 build-ticks of demand against a run that starts with 500 Enteloot).
It never checked affordability before generating the action list, so the
engine replay came back with dozens of "cannot afford build" / "prerequisite
not met" invalid actions once Enteloot ran out around tick ~1,850 - and then
just left the remaining ~3,000 ticks of the run completely unused, because
the plan had nothing left to fall back on.

THE FIX
-------
This solver (via common_solver.py) builds a global, prerequisite-safe
priority order of (town, upgrade) candidates - cheapest score/enteloot_cost
production upgrades first, then the civic chain, round-robined across towns
so value spreads out rather than piling into one town - and then binary-
searches the longest PREFIX of that list that actually replays through the
real Engine with zero invalid actions and within the tick budget. That
prefix is what gets built. This guarantees a submittable, valid plan by
construction instead of hoping the numbers happen to work out.
"""

import json

from engine import load_json
from common_solver import (
    candidate_upgrade_order,
    plan_feasible_prefix,
    replay,
    plan_income_tail,
)


def find_crafting_hub(level):
    """Prefer the starting town if it has crafting affinity, else the
    alphabetically-first affinity town, else the starting town."""
    start = level["run"]["starting_town"]
    if "crafting" in level["towns"][start].get("affinities", []):
        return start
    candidates = [n for n, t in level["towns"].items() if "crafting" in t.get("affinities", [])]
    return sorted(candidates)[0] if candidates else start


def print_result(result, meta, kept_pairs):
    print()
    print("=" * 72)
    print("LEVEL 2 RESULT")
    print("=" * 72)
    print("Upgrades attempted (feasible prefix):", len(kept_pairs))
    by_town = {}
    for town, upgrade in kept_pairs:
        by_town.setdefault(town, []).append(upgrade)
    for town, ups in by_town.items():
        print(f"  {town}: {', '.join(ups)}")

    print()
    print("Planned infrastructure score:", meta["score_value"])
    print("Planned Enteloot cost:", meta["enteloot_cost"])
    print("Planned build ticks:", meta["build_ticks"])

    print()
    print("Engine result:")
    print("  final_tick:", result["final_tick"], "/", result.get("_total_ticks"))
    print("  final_enteloot:", result["final_enteloot"])
    print("  infrastructure_score:", result["total_infrastructure_score_value"])
    print("  final_location:", result["final_location"])

    print()
    print("Built:")
    for town, upgrades in result["built"].items():
        if upgrades:
            print(f"  {town}: {', '.join(upgrades)}")

    print()
    print("Final inventory:")
    for item, quantity in sorted(result["final_inventory"].items()):
        if quantity:
            print(f"  {item}: {quantity}")


def main():
    constants = load_json("resources.json")
    level = load_json("2.txt")
    level_number = 2

    start = level["run"]["starting_town"]
    hub = find_crafting_hub(level)

    print("Level 2 solver")
    print("==============")
    print("Starting town:", start)
    print("Crafting hub:", hub)
    print("Tick budget:", level["run"]["total_ticks"])
    print("Starting Enteloot:", level["run"]["starting_enteloot"])

    ordered_pairs = candidate_upgrade_order(constants, list(level["towns"].keys()), level_number)
    print()
    print("Candidate (town, upgrade) pairs (priority order):", len(ordered_pairs))

    actions, meta, kept_pairs = plan_feasible_prefix(
        constants, level, level_number, ordered_pairs, hub,
    )

    result, invalid = replay(constants, level, level_number, actions)
    result["_total_ticks"] = level["run"]["total_ticks"]
    
    if not invalid and result["final_tick"] < level["run"]["total_ticks"]  and len(kept_pairs) < len(ordered_pairs):
        print(f"\nAdding tail income phase (remaining ticks: {level['run']['total_ticks'] - result['final_tick']})...")
        actions, result = plan_income_tail(
            constants, level, level_number, hub, actions,
            use_upkeep=False  # Level 3 doesn't have upkeep
        )
        # Re-verify the combined plan
        result, invalid = replay(constants, level, level_number, actions)
        result["_total_ticks"] = level["run"]["total_ticks"]

    print_result(result, meta, kept_pairs)

    print()
    print("Generated actions:", len(actions))
    print("Invalid actions:", len(invalid))
    if invalid:
        print("First invalid actions:")
        for entry in invalid[:10]:
            print(" ", entry["tick"], entry["action"], "->", entry["detail"])

    with open("level2_actions.txt", "w") as f:
        json.dump({"actions": actions}, f, indent=2)
    print()
    print("Wrote level2_actions.txt")

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
