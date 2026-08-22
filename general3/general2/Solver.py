"""
solver.py — constructs a full actions.json for a level.

Approach
--------
True brute force over the action space is impossible here (branching factor
per tick is huge and horizons run to 100,000 ticks), so instead this module:

  1. Uses the planner to identify the most profitable repeatable *cycle*
     (gather/buy -> craft -> sell), since prices/recipes are fixed for the
     whole run (Assumption 11) the best cycle found early stays the best
     cycle throughout.
  2. Drives an actual `engine.Simulator` incrementally: every chunk of
     actions we're considering (a tool build, an upgrade, N cycle
     repetitions) is first trial-run on a `copy.deepcopy` of the live
     simulator to get its EXACT tick/Enteloot cost (no guesswork about
     affinity/tools/prices), then only committed to the real simulator if
     it is affordable and beneficial.
  3. For Level 2+, spends surplus Enteloot building upgrades, touring towns
     in a nearest-neighbour order and building as deep a chain as capital
     allows at each stop, so development is spread across towns (the
     spec's "distribution multiplier") rather than piled into one.
  4. Several complete strategies (different phase splits, tools on/off,
     upkeep on/off) are generated and each is scored with the exact
     engine; the best-scoring valid one is returned. This is the practical
     stand-in for "try every method": every *principled* strategy is
     checked against the real simulator, and the winner is kept.
"""
from __future__ import annotations
import copy
import itertools
import json
import sys

# Import from local modules
from engine import Simulator, features_for_level, ALL_UPGRADES, PRODUCTION_UPGRADES, CIVIC_UPGRADES
from planner import (
    GraphIndex, best_recipe_cycles, best_raw_sell_cycle,
    build_recipe_cycle_actions, build_raw_sell_cycle_actions,
    input_source_plan, plan_multi_chain,
)


# ---------------------------------------------------------------------
# Measurement helper: trial-run a chunk of actions on a scratch copy of
# the live simulator to learn its exact tick/enteloot cost before
# committing to it for real.
# ---------------------------------------------------------------------
def measure(sim: Simulator, actions: list):
    if not actions:
        return 0, 0, 0, sim.location
    s2 = copy.deepcopy(sim)
    t0, e0 = s2.tick, s2.enteloot
    n0 = len(s2.log)
    s2.run_actions(actions)
    ticks = s2.tick - t0
    profit = s2.enteloot - e0
    tail = s2.log[n0:]
    invalid = sum(1 for e in tail if e.get("result") == "invalid")
    return ticks, profit, invalid, s2.location


def commit(sim: Simulator, action_log: list, actions: list):
    sim.run_actions(actions)
    action_log.extend(actions)


# ---------------------------------------------------------------------
# Wealth cycle selection
# ---------------------------------------------------------------------
def pick_wealth_cycle(gi: GraphIndex, current_location: str, features: dict, has_pickaxe: bool):
    candidates = []
    if features["crafting"]:
        for r in best_recipe_cycles(gi, current_location, has_pickaxe, top_n=6):
            actions = build_recipe_cycle_actions(gi, r["hub"], r["sell_town"], r["item"], r["batch"], has_pickaxe)
            if actions is None:
                continue
            candidates.append({"kind": "recipe", "hub": r["hub"], "label": r["item"],
                                "actions": actions, "rate_estimate": r["rate"]})
    for r in best_raw_sell_cycle(gi, current_location, has_pickaxe, top_n=6):
        actions = build_raw_sell_cycle_actions(gi, r["node"], r["sell_town"], r["gathers"])
        if actions is None:
            continue
        candidates.append({"kind": "raw", "hub": r["node"], "label": r["resource"],
                            "actions": actions, "rate_estimate": r["rate"]})
    candidates.sort(key=lambda c: -c["rate_estimate"])
    return candidates


def run_wealth_cycles(sim: Simulator, gi: GraphIndex, action_log: list, features: dict,
                       has_pickaxe: bool, tick_budget: int, min_reserve_enteloot: int = 0):
    """Pick the best cycle that actually measures as valid & profitable from
    the current live state, travel to its hub, and repeat it to (roughly)
    fill `tick_budget`. Returns ticks actually spent."""
    start_tick = sim.tick
    candidates = pick_wealth_cycle(gi, sim.location, features, has_pickaxe)
    for cand in candidates:
        travel_in = gi.travel_sequence(sim.location, cand["hub"])
        if travel_in is None:
            continue
        t_travel, e_travel, inv_travel, loc = measure(sim, travel_in)
        if inv_travel > 0:
            continue
        s2 = copy.deepcopy(sim)
        s2.run_actions(travel_in)
        t_cyc, e_cyc, inv_cyc, _ = measure(s2, cand["actions"])
        if inv_cyc > 0 or t_cyc <= 0:
            continue
        # found a working cycle
        commit(sim, action_log, travel_in)
        remaining = tick_budget - (sim.tick - start_tick)
        reps = max(0, remaining // t_cyc)
        # cap reps to avoid pathological huge lists; batch is fine even in
        # the tens of thousands for level 4, but guard anyway
        reps = int(reps)
        for _ in range(reps):
            if sim.tick - start_tick >= tick_budget:
                break
            commit(sim, action_log, cand["actions"])
        return sim.tick - start_tick, cand
    return 0, None


# ---------------------------------------------------------------------
# Tools (level 3+)
# ---------------------------------------------------------------------
def plan_tools(gi: GraphIndex, current: str):
    mines = [(nid, n) for nid, n in gi.nodes.items() if n["type"] == "mine"]
    if not mines:
        return None
    best = None
    for nid, n in mines:
        d = gi.dist(current, nid)
        if d is not None and (best is None or d < best[1]):
            best = (nid, d)
    if best is None:
        return None
    mine_nid, d_to_mine = best
    d_from_mine, _, _ = gi.shortest_paths_from(mine_nid)
    town_candidates = [(t, d_from_mine[t]) for t in gi.towns if t in d_from_mine]
    if not town_candidates:
        return None
    town_candidates.sort(key=lambda tc: (0 if "crafting" in gi.towns[tc[0]].get("affinities", []) else 1, tc[1]))
    hub = town_candidates[0][0]

    actions = []
    to_mine = gi.travel_sequence(current, mine_nid)
    if to_mine is None:
        return None
    actions += to_mine
    node = gi.nodes[mine_nid]
    ORE_NEEDED = 8  # 2 tools x 2 iron-fittings x 2 ore
    n_gathers = -(-ORE_NEEDED // node["yield"])
    actions += [{"type": "gather"} for _ in range(n_gathers)]
    to_hub = gi.travel_sequence(mine_nid, hub)
    if to_hub is None:
        return None
    actions += to_hub

    # iron-fittings needs wood too (1 per iron-fitting, need 4 iron-fittings total)
    wood_opts = input_source_plan(gi, hub, "wood", 4)
    if not wood_opts:
        return None
    best_wood = min(wood_opts, key=lambda o: o["ticks"] + o["enteloot_cost"] / 20.0)
    actions += best_wood["actions"]
    actions.append({"type": "craft", "item": "iron-fittings", "quantity": 4})

    rope_chain = plan_multi_chain(gi, hub, {"rope": 2})
    planks_chain = plan_multi_chain(gi, hub, {"planks": 2})
    if rope_chain is None or planks_chain is None:
        return None
    actions += rope_chain
    actions += planks_chain
    actions.append({"type": "craft", "item": "boots", "quantity": 1})
    actions.append({"type": "craft", "item": "pickaxe", "quantity": 1})
    return {"hub": hub, "actions": actions}


# ---------------------------------------------------------------------
# Upgrade buildout (level 2+)
# ---------------------------------------------------------------------
def nn_tour(gi: GraphIndex, start: str, towns: list):
    remaining = set(towns)
    remaining.discard(start)
    order = []
    cur = start
    while remaining:
        best_t, best_d = None, None
        for t in remaining:
            d = gi.dist(cur, t)
            if d is not None and (best_d is None or d < best_d):
                best_t, best_d = t, d
        if best_t is None:
            break
        order.append(best_t)
        remaining.discard(best_t)
        cur = best_t
    return order


def try_build_one(sim: Simulator, gi: GraphIndex, action_log: list, town: str,
                   upgrade: str, has_pickaxe: bool, enteloot_reserve: int) -> bool:
    """Attempt to source components and build `upgrade` at `town` (sim must
    already be located at `town`). Commits to sim if it works."""
    if upgrade not in ALL_UPGRADES:
        return False
    ts = sim.towns[town]
    if upgrade in ts.upgrades:
        return False
    info = ALL_UPGRADES[upgrade]
    prereq = info.get("prerequisite")
    if prereq:
        kind = prereq.get("type")
        if kind == "any_production_upgrades":
            val = prereq.get("count", 1)
            if sum(1 for u in ts.upgrades if u in PRODUCTION_UPGRADES) < val:
                return False
        elif kind == "specific_upgrade":
            val = prereq.get("upgrade")
            if val not in ts.upgrades:
                return False
    if upgrade == "police-station" and not sim.features["mines"]:
        return False

    chain = plan_multi_chain(gi, town, info["components"], has_pickaxe)
    if chain is None:
        return False
    build_action = [{"type": "build", "upgrade": upgrade}]
    full = chain + build_action

    t_cost, e_cost, invalid, _ = measure(sim, full)
    if invalid > 0:
        return False
    if sim.enteloot + e_cost < enteloot_reserve:  # e_cost is negative (spend)
        return False
    if sim.tick + t_cost > sim.total_ticks:
        return False
    commit(sim, action_log, full)
    return True


def buildout_tour(sim: Simulator, gi: GraphIndex, action_log: list, has_pickaxe: bool,
                   enteloot_reserve: int, tick_budget: int, deep_first: bool = True):
    start_tick = sim.tick
    order = nn_tour(gi, sim.location, list(gi.towns.keys()))
    order = [sim.location] + order  # include current town first
    prod_names = list(PRODUCTION_UPGRADES.keys())

    for town in order:
        if sim.tick - start_tick >= tick_budget:
            break
        travel = gi.travel_sequence(sim.location, town)
        if travel is None:
            continue
        t_cost, e_cost, invalid, _ = measure(sim, travel)
        if invalid > 0:
            continue
        if sim.tick + t_cost > sim.total_ticks:
            break
        commit(sim, action_log, travel)

        # up to 2 different production upgrades (covers rec-center & fire-station prereqs)
        built_prod = 0
        for up in prod_names:
            if built_prod >= 2:
                break
            if up in sim.towns[town].upgrades:
                built_prod += 1
                continue
            if try_build_one(sim, gi, action_log, town, up, has_pickaxe, enteloot_reserve):
                built_prod += 1
            if sim.tick - start_tick >= tick_budget:
                break

        for up in ["rec-center", "fire-station", "school", "police-station", "library"]:
            if sim.tick - start_tick >= tick_budget:
                break
            try_build_one(sim, gi, action_log, town, up, has_pickaxe, enteloot_reserve)

    return sim.tick - start_tick


# ---------------------------------------------------------------------
# Full strategy builder
# ---------------------------------------------------------------------
def build_strategy(level_data: dict, level_number: int, *, use_tools=True,
                    phase1_fraction=0.35, enteloot_reserve=0, seed_label="") -> Simulator:
    features = features_for_level(level_number)
    gi = GraphIndex(level_data, allow_fast=features["fast_routes"])
    sim = Simulator(level_data, level_number)
    action_log = []
    has_pickaxe = False

    # Phase 0: tools
    if features["tools"] and use_tools and sim.total_ticks >= 2000:
        plan = plan_tools(gi, sim.location)
        if plan:
            travel = gi.travel_sequence(sim.location, plan["hub"])
            # plan['actions'] already starts with travel from `current`(=sim.location at plan time)
            t_cost, e_cost, invalid, _ = measure(sim, plan["actions"])
            if invalid == 0 and sim.enteloot + e_cost >= 0 and sim.tick + t_cost <= sim.total_ticks * 0.4:
                commit(sim, action_log, plan["actions"])
                has_pickaxe = True

    # Phase 1: wealth engine (build a war chest)
    total = sim.total_ticks
    phase1_budget = int((total - sim.tick) * phase1_fraction)
    run_wealth_cycles(sim, gi, action_log, features, has_pickaxe, phase1_budget)

    # Phase 2: build-out across towns
    if features["building"]:
        remaining_budget = int((total - sim.tick) * 0.55)
        buildout_tour(sim, gi, action_log, has_pickaxe, enteloot_reserve, remaining_budget)

    # Phase 3: spend whatever ticks remain back on the wealth cycle
    remaining = total - sim.tick
    if remaining > 0:
        run_wealth_cycles(sim, gi, action_log, features, has_pickaxe, remaining)

    # Phase 4: mop up — if a lot of ticks (or enteloot) remain, do another
    # buildout pass (upgrades may now be affordable) then a final wealth pass
    if features["building"]:
        remaining = total - sim.tick
        if remaining > 50:
            buildout_tour(sim, gi, action_log, has_pickaxe, enteloot_reserve, remaining)
            remaining = total - sim.tick
            if remaining > 0:
                run_wealth_cycles(sim, gi, action_log, features, has_pickaxe, remaining)

    sim._final_actions = action_log
    sim._strategy_label = seed_label
    return sim


def solve(level_data: dict, level_number: int, verbose=True):
    """Try a handful of complete strategies, score each with the exact
    engine, return the best (sim, actions)."""
    features = features_for_level(level_number)
    strategies = []
    phase1_options = [0.25, 0.4, 0.6] if level_number >= 2 else [1.0]
    tools_options = [True, False] if features["tools"] else [False]

    for p1 in phase1_options:
        for ut in tools_options:
            label = f"p1={p1},tools={ut}"
            strategies.append((p1, ut, label))

    best = None
    best_score = None
    reports = []
    for p1, ut, label in strategies:
        try:
            sim = build_strategy(level_data, level_number, use_tools=ut, phase1_fraction=p1, seed_label=label)
        except Exception as e:
            reports.append((label, f"EXCEPTION: {e}"))
            continue
        summ = sim.summarize()
        score = summ["proxy_score_level1"] if level_number == 1 else summ["proxy_score_level2plus"]
        reports.append((label, summ))
        if best_score is None or score > best_score:
            best_score = score
            best = sim

    if verbose:
        for label, r in reports:
            print(f"--- strategy {label} ---")
            print(r)
    return best, reports


# ---------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python Solver.py <level_number>")
        print("Example: python Solver.py 1")
        sys.exit(1)
    
    level_num = int(sys.argv[1])
    level_file = f"Level{level_num}.json"
    
    try:
        with open(level_file) as f:
            level_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: {level_file} not found")
        sys.exit(1)
    
    best_sim, reports = solve(level_data, level_num, verbose=True)
    
    if best_sim:
        print("\n" + "="*60)
        print("BEST STRATEGY SUMMARY")
        print("="*60)
        print(json.dumps(best_sim.summarize(), indent=2))
        
        # Optionally save the actions to a file
        if hasattr(best_sim, '_final_actions'):
            with open(f"actions_level{level_num}.json", "w") as f:
                json.dump({"actions": best_sim._final_actions}, f, indent=2)
            print(f"\nActions saved to actions_level{level_num}.json")
    else:
        print("No valid strategy found")