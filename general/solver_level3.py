"""
Age of Enteland - Level 3 solver.

Level 3 unlocks (in addition to Level 1 & 2): fast_routes, mine_nodes, ore,
iron-fittings, tools, police-station.

APPROACH
--------
This builds directly on the Level 2 machinery in common_solver.py:
  - the same feasibility-trimmed (town, upgrade) prefix search now also
    considers `police-station` (added automatically by
    candidate_upgrade_order once its min_level <= level_number), which
    needs iron-fittings and therefore a trip to a Mine node,
  - ore is a normal raw resource in constants["resources"] (buy_price is
    null) so the existing gather-node planner already routes its need to
    Mine nodes without any special-casing - it just can never be bought,
    which our planner never attempts anyway (we only gather/craft/sell).

WHAT'S ADDED FOR LEVEL 3 SPECIFICALLY
--------------------------------------
1. Tools (boots, pickaxe): crafted once, up front, via `extra_target_items`
   if a Mine node is reachable from the hub. They are permanent buffs, so
   crafting them before the main gather/build campaign lets every
   subsequent travel/gather action in the plan benefit - though see the
   note below on why we don't try to *plan* around their time savings.
2. Fast routes: intentionally NOT auto-selected by the pathfinder here.
   Taking a fast route only pays off when the Enteloot toll is worth more
   than the ticks it saves relative to what those ticks would otherwise
   earn - a real trade-off that depends on the rest of the plan. Modelling
   that properly is a well-scoped follow-on (mirroring how solver_level1's
   docstring flags multi-node splitting as a documented extension rather
   than guessed at): for now, standard routes are used everywhere, which
   is always valid, just potentially slower than optimal. The engine
   already supports `fast: true` travel actions; a future pass could
   compare, per edge on a candidate path, standard-time vs (fast-time +
   toll/value-of-time) and swap in the fast edge when it wins.

NOTE ON TOOL TIMING VS PLANNED TICKS
-------------------------------------
`build_actions_for_pairs` is called with travel_delta=0 / gather_delta=0
(i.e. it plans as if tools were never acquired) even though tools ARE
crafted first in the actual action list. This is a deliberate, safe
simplification: it means the *planned* tick totals are a conservative
overestimate of what the engine will actually charge (since boots/pickaxe
reduce real travel/gather cost after they're crafted), so actual replay
finishes with ticks to spare rather than risking underestimating and
overrunning the budget.
"""

import json

from engine import load_json
from common_solver import (
    build_adjacency,
    candidate_upgrade_order,
    dijkstra,
    plan_feasible_prefix,
    replay,
    plan_income_tail,
)


def find_crafting_hub(level):
    start = level["run"]["starting_town"]
    if "crafting" in level["towns"][start].get("affinities", []):
        return start
    candidates = [n for n, t in level["towns"].items() if "crafting" in t.get("affinities", [])]
    return sorted(candidates)[0] if candidates else start


def mine_reachable(level, hub):
    adj = build_adjacency(level["routes"])
    dist, _ = dijkstra(adj, hub)
    for name, node in level["nodes"].items():
        if node["type"] == "mine" and name in dist:
            return True
    return False


def print_result(result, meta, kept_pairs, tools_planned):
    print()
    print("=" * 72)
    print("LEVEL 3 RESULT")
    print("=" * 72)
    print("Tools crafted:", tools_planned or "none (no reachable Mine node)")
    print("Upgrades attempted (feasible prefix):", len(kept_pairs))
    by_town = {}
    for town, upgrade in kept_pairs:
        by_town.setdefault(town, []).append(upgrade)
    for town, ups in by_town.items():
        print(f"  {town}: {', '.join(ups)}")

    print()
    print("Planned infrastructure score:", meta["score_value"])
    print("Planned Enteloot cost:", meta["enteloot_cost"])

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


def main():
    constants = load_json("resources.json")
    level = load_json("3.txt")
    level_number = 3

    start = level["run"]["starting_town"]
    hub = find_crafting_hub(level)

    print("Level 3 solver")
    print("==============")
    print("Starting town:", start)
    print("Crafting hub:", hub)
    print("Tick budget:", level["run"]["total_ticks"])
    print("Starting Enteloot:", level["run"]["starting_enteloot"])

    have_mine = mine_reachable(level, hub)
    tools_planned = []
    extra_target_items = None
    if have_mine:
        tools_planned = ["boots", "pickaxe"]
        extra_target_items = [("boots", 1), ("pickaxe", 1)]
    print("Mine node reachable from hub:", have_mine)

    ordered_pairs = candidate_upgrade_order(constants, list(level["towns"].keys()), level_number)
    print("Candidate (town, upgrade) pairs (priority order):", len(ordered_pairs))

    actions, meta, kept_pairs = plan_feasible_prefix(
        constants, level, level_number, ordered_pairs, hub,
        extra_target_items=extra_target_items,
    )

    result, invalid = replay(constants, level, level_number, actions)
    result["_total_ticks"] = level["run"]["total_ticks"]

    # Tail income: capped rather than on/off. Uncapped, the batch search
    # fills ~85% of every idle tick, which on this map meant 14k+ extra
    # gather actions for Enteloot that (per the spec) scores "far less"
    # once hoarded - but empirically it still scores *something* on this
    # level, so cutting it entirely (as we now do for Level 2) measured
    # worse here. MAX_TAIL_TICKS bounds the batch to a small, cheap slice
    # of the idle budget so most of that residual value is captured without
    # the 10x+ blowup in action count / solve time. Tune this constant
    # against your real grader if the trade-off point moves.
    MAX_TAIL_TICKS = 3000
    if not invalid and result["final_tick"] < level["run"]["total_ticks"]:
        idle = level["run"]["total_ticks"] - result["final_tick"]
        print(f"\nAdding capped tail income phase (idle ticks: {idle}, "
              f"using up to {min(idle, MAX_TAIL_TICKS)})...")
        actions, result = plan_income_tail(
            constants, level, level_number, hub, actions,
            use_upkeep=False,  # Level 3 doesn't have upkeep
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

    with open("level3_actions.txt", "w") as f:
        json.dump({"actions": actions}, f, indent=2)
    print()
    print("Wrote level3_actions.txt")

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