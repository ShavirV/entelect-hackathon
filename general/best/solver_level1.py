"""
Age of Enteland - Level 1 solver.

LEVEL 1 CONTEXT
---------------
Only travel / buy / sell / gather are unlocked. No crafting, no building.
Score (per the problem statement) comes from:
  (a) Enteloot generated over the run,
  (b) the value of items held at the end,
  (c) a multiplier tied to how many items you actually sold.
The exact scoring formula/weights for (c) aren't given in the provided
materials, so this solver optimizes the well-defined part of that objective
directly: maximize (Enteloot earned by selling gathered resources) + (value
of any resources still held at the end), inside the tick budget - and sells
everything it gathers, so it also maximizes items-sold as a side effect.

KEY OBSERVATION THAT SIMPLIFIES LEVEL 1
----------------------------------------
Resource sell prices are GLOBAL CONSTANTS (resources.json), not per-town.
Only crafted goods have per-town item-rates, and crafting is disabled at
Level 1. So *where* you sell a raw resource doesn't change what you get for
it - the only real levers are:
  1. Which node(s) to gather at (yield / gather_time = "harvest rate"),
  2. How much travel overhead it costs to reach and return from that node,
  3. Buying is a pure loss for resources you don't need to craft with
     (buy_price > sell_price always), so buying-then-selling is never
     profitable at Level 1 and the solver ignores it.
  4. Passive town trickle is running for free the whole time regardless of
     what the player does, so it doesn't need to be "solved for" - it's
     credited automatically. The player's job is to add value ON TOP of it.

STRATEGY: "camp the best node"
-------------------------------
Since nodes are never depleted and a single sell action can liquidate
any quantity for a flat 1 tick, the ticks-efficient pattern is:
    travel to node -> gather, gather, gather, ... -> travel to a town -> sell
i.e. one round trip, front-load all the gathering, cash out once. Travelling
back and forth between individual gathers only wastes ticks.

For every node reachable from the starting town, the solver computes the
total Enteloot obtainable if the WHOLE run's tick budget were dedicated to
that node (outbound travel + repeated gather + inbound travel to the best
nearby town + one sell action), and picks the best single node. This is a
strong, easily-verifiable baseline: with total_ticks typically in the
hundreds and round trips costing single-digit ticks, the top node's
steady-state harvest rate (yield/gather_time * sell_price) dominates the
fixed travel overhead, so "dedicate everything to the best node" is at or
very near optimal for a single-resource, single-node economy.

The code is written so a straightforward extension - splitting the budget
across the top-K nodes by marginal rate once diminishing overhead no longer
favors a single node - can be dropped in; see `plan_multi_node` stub at the
bottom for where that would go.
"""

import heapq
import json
from engine import Engine, load_json


def dijkstra(adj, source):
    """Shortest travel-time (standard routes only - fast routes aren't
    unlocked until Level 3) from source to every other vertex. Returns
    (dist, prev) so callers can reconstruct the actual hop-by-hop path -
    the engine's travel action only moves across a single direct edge, so
    a multi-hop route needs one travel action per edge, not one jump."""
    dist = {source: 0}
    prev = {}
    pq = [(0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, float("inf")):
            continue
        for v, edge in adj.get(u, []):
            if edge["fast"]:
                continue  # not usable at level 1
            nd = d + edge["weight"]
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def reconstruct_path(prev, source, target):
    """Returns the list of vertices [source, ..., target]."""
    if source == target:
        return [source]
    path = [target]
    while path[-1] != source:
        path.append(prev[path[-1]])
    return list(reversed(path))


def build_adjacency(routes):
    from collections import defaultdict
    adj = defaultdict(list)
    for r in routes:
        a, b = r["between"]
        fast = r.get("toll", 0) > 0
        edge = {"weight": r["weight"], "toll": r.get("toll", 0), "fast": fast}
        adj[a].append((b, edge))
        adj[b].append((a, edge))
    return adj


def plan_single_node_strategy(constants, level):
    towns = level["towns"]
    nodes = level["nodes"]
    total_ticks = level["run"]["total_ticks"]
    start = level["run"]["starting_town"]
    adj = build_adjacency(level["routes"])

    dist_from_start, prev_from_start = dijkstra(adj, start)
    town_names = list(towns.keys())

    best = None  # (enteloot, node_name, sell_town, num_gathers, out_dist, back_dist)

    for node_name, node in nodes.items():
        if node_name not in dist_from_start:
            continue  # unreachable
        out_dist = dist_from_start[node_name]

        # distance from this node back to every town (need node's own
        # shortest-path tree, so run dijkstra from the node)
        dist_from_node, prev_from_node = dijkstra(adj, node_name)
        reachable_towns = [(t, dist_from_node[t]) for t in town_names if t in dist_from_node]
        if not reachable_towns:
            continue
        sell_town, back_dist = min(reachable_towns, key=lambda x: x[1])

        overhead = out_dist + back_dist + 1  # +1 for the sell action
        remaining = total_ticks - overhead
        if remaining <= 0:
            continue

        gather_time = node["gather-time"]
        num_gathers = remaining // gather_time
        if num_gathers <= 0:
            continue

        yield_total = num_gathers * node["yield"]
        sell_price = constants["resources"][node["resource"]]["sell_price"]
        enteloot = yield_total * sell_price

        candidate = {
            "enteloot": enteloot,
            "node": node_name,
            "resource": node["resource"],
            "sell_town": sell_town,
            "num_gathers": num_gathers,
            "yield_total": yield_total,
            "out_dist": out_dist,
            "back_dist": back_dist,
            "gather_time": gather_time,
            "ticks_used": out_dist + num_gathers * gather_time + back_dist + 1,
            "path_out": reconstruct_path(prev_from_start, start, node_name),
            "path_back": reconstruct_path(prev_from_node, node_name, sell_town),
        }
        if best is None or candidate["enteloot"] > best["enteloot"]:
            best = candidate

    return best


def build_actions(start, plan):
    actions = []
    # one travel action per edge on the outbound path
    for a, b in zip(plan["path_out"], plan["path_out"][1:]):
        actions.append({"type": "travel", "destination": b})
    for _ in range(plan["num_gathers"]):
        actions.append({"type": "gather"})
    # one travel action per edge on the return path
    for a, b in zip(plan["path_back"], plan["path_back"][1:]):
        actions.append({"type": "travel", "destination": b})
    actions.append({"type": "sell", "item": plan["resource"], "quantity": plan["yield_total"]})
    return actions


def plan_multi_node(constants, level):
    """
    Extension point: once a single node's marginal rate no longer beats the
    2nd-best node's rate net of *its* travel overhead, splitting remaining
    ticks across multiple nodes (or interleaving with town trickle capture)
    can raise total Enteloot further. For Level 1's typical scale (a handful
    of nodes, travel cost << total_ticks) the single-node plan captures
    the overwhelming majority of achievable value, so this is left as a
    documented extension rather than implemented speculatively without a
    concrete larger map to validate it against.
    """
    raise NotImplementedError("see docstring - plug in the real level1.json to extend")


if __name__ == "__main__":
    constants = load_json("resources.json")
    level = load_json("1.txt")

    plan = plan_single_node_strategy(constants, level)
    print("Chosen plan:", json.dumps(plan, indent=2))

    actions = build_actions(level["run"]["starting_town"], plan)
    print("\nGenerated actions:")
    print(json.dumps({"actions": actions}, indent=2))

    with open("level1_actions.txt", "w") as f:
        json.dump({"actions": actions}, f, indent=2)

    # Verify by replaying through the engine
    eng = Engine(constants, level, level_number=1)
    result = eng.run(actions)
    print("\nEngine replay summary:")
    print("final_tick:", result["final_tick"])
    print("final_enteloot:", result["final_enteloot"],
          "(started with", level["run"]["starting_enteloot"], ")")
    print("final_inventory:", result["final_inventory"])
    net_gain = result["final_enteloot"] - level["run"]["starting_enteloot"]
    print("net enteloot gain from starting balance:", net_gain)
