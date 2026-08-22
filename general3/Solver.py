import json, heapq, sys
from Simulator import Simulator

SINGLE_RES_RECIPE = {"wheat": "bread", "wood": "wooden-crafts", "stone": "stone-works", "sheep": "wool-garments"}


def dijkstra(adj, src):
    dist = {src: 0}
    pq = [(0, src)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        for (v, w, toll, fast) in adj.get(u, []):
            if fast:
                continue  # ignore fast/toll routes for path planning simplicity
            nd = d + w
            if nd < dist.get(v, 1 << 30):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def build_adj(routes):
    from collections import defaultdict
    adj = defaultdict(list)
    for r in routes:
        a, b = r["between"]
        w, toll = r["weight"], r.get("toll", 0)
        adj[a].append((b, w, toll > 0))
        adj[b].append((a, w, toll > 0))
    return adj


def path(dist_from, prev, dst):
    pass  # not needed; we just need distances + we reconstruct via BFS parent map below


def shortest_path_edges(adj, src, dst):
    # returns ordered list of hop destinations from src to dst (standard routes only)
    dist = {src: 0}
    prev = {}
    pq = [(0, src)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == dst:
            break
        for (v, w, fast) in adj.get(u, []):
            if fast:
                continue
            nd = d + w
            if nd < dist.get(v, 1 << 30):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if dst not in dist:
        return None, None
    hops = []
    cur = dst
    while cur != src:
        hops.append(cur)
        cur = prev[cur]
    hops.reverse()
    return hops, dist[dst]


def find_best_raw_loop(level, const):
    """Level 1: no craft. Gather raw resource, sell at nearest town (sell price is global)."""
    towns = level["towns"]
    nodes = level["nodes"]
    adj = build_adj(level["routes"])
    best = None
    for node_name, node in nodes.items():
        resource = node["resource"]
        price = const["resources"].get(resource, {}).get("sell_price")
        if not price:
            continue
        gtime = node.get("gather-time", 2)
        yield_amt = node["yield"]
        best_town, best_dist = None, 1 << 30
        for t in towns:
            hops, d = shortest_path_edges(adj, node_name, t)
            if hops is not None and d < best_dist:
                best_town, best_dist = t, d
        if best_town is None:
            continue
        cycle_ticks = gtime + best_dist + 1 + best_dist  # gather, go sell, come back
        revenue_per_tick = (yield_amt * price) / cycle_ticks
        cand = dict(node=node_name, resource=resource, recipe=None, affinity_town=best_town,
                    sell_town=best_town, price=price, gtime=gtime, yield_amt=yield_amt,
                    need_per_item=1, aff_dist=0, sell_dist=best_dist, back_dist=best_dist,
                    craft_time=0, score_per_tick=revenue_per_tick, gathers_needed=1)
        if best is None or cand["score_per_tick"] > best["score_per_tick"]:
            best = cand
    return best, adj


def find_best_loop(level, const, level_num):
    if level_num < 2:
        return find_best_raw_loop(level, const)
    towns = level["towns"]
    nodes = level["nodes"]
    adj = build_adj(level["routes"])
    start = level["run"]["starting_town"]

    best = None
    for node_name, node in nodes.items():
        resource = node["resource"]
        recipe_name = SINGLE_RES_RECIPE.get(resource)
        if not recipe_name:
            continue
        recipe = const["recipes"][recipe_name]
        need_per_item = recipe["inputs"][resource]
        gtime = node.get("gather-time", 2)
        yield_amt = node["yield"]

        # nearest affinity town reachable from node
        best_affinity, best_aff_dist = None, 1 << 30
        for t, tdata in towns.items():
            if "crafting" in tdata.get("affinities", []):
                hops, d = shortest_path_edges(adj, node_name, t)
                if hops is not None and d < best_aff_dist:
                    best_affinity, best_aff_dist = t, d
        if best_affinity is None:
            continue

        # best-paying town for recipe, reachable from affinity town
        best_sell, best_price, best_sell_dist = None, -1, None
        for t, tdata in towns.items():
            price = tdata.get("item-rates", {}).get(recipe_name)
            if price is None:
                continue
            hops, d = shortest_path_edges(adj, best_affinity, t)
            if hops is None:
                continue
            if price > best_price or (price == best_price and d < (best_sell_dist or 1 << 30)):
                best_sell, best_price, best_sell_dist = t, price, d
        if best_sell is None:
            continue

        # distance back from sell town to node, to close the loop
        hops_back, d_back = shortest_path_edges(adj, best_sell, node_name)
        if hops_back is None:
            continue

        craft_time = const["constants"]["craft_time_affinity"] if "crafting" in towns[best_affinity].get("affinities", []) else const["constants"]["craft_time_base"]

        # one "unit cycle": gather enough for 1 crafted item, craft 1, sell 1
        gathers_needed = -(-need_per_item // yield_amt)  # ceil
        cycle_ticks = gathers_needed * gtime + best_aff_dist + craft_time + best_sell_dist + 1 + d_back
        cycle_revenue = best_price
        score_per_tick = cycle_revenue / cycle_ticks

        cand = dict(node=node_name, resource=resource, recipe=recipe_name, affinity_town=best_affinity,
                    sell_town=best_sell, price=best_price, gtime=gtime, yield_amt=yield_amt,
                    need_per_item=need_per_item, aff_dist=best_aff_dist, sell_dist=best_sell_dist,
                    back_dist=d_back, craft_time=craft_time, score_per_tick=score_per_tick,
                    gathers_needed=gathers_needed)
        if best is None or cand["score_per_tick"] > best["score_per_tick"]:
            best = cand
    return best, adj


def hops_to_actions(adj, src, dst):
    hops, _ = shortest_path_edges(adj, src, dst)
    return [{"type": "travel", "destination": h} for h in (hops or [])]


def generate_actions(level, const, level_num, start_at=None):
    towns = level["towns"]
    start = start_at or level["run"]["starting_town"]
    total_ticks = level["run"]["total_ticks"]
    best, adj = find_best_loop(level, const, level_num)
    if best is None:
        return []

    actions = []
    loc = start
    actions += hops_to_actions(adj, loc, best["node"])
    loc = best["node"]

    # reserve some ticks at the end as safety buffer (in case of rounding)
    buffer = max(50, int(total_ticks * 0.005))
    budget = total_ticks - buffer

    raw_mode = best["recipe"] is None

    # cost of a single big batch: gather G times, travel to affinity, craft G items, travel to sell town, sell, travel back to node
    def batch_cost(g):
        if raw_mode:
            return g * best["gtime"] + best["sell_dist"] + 1 + best["back_dist"]
        return g * best["gtime"] + best["aff_dist"] + g * best["craft_time"] + best["sell_dist"] + 1 + best["back_dist"]

    # choose a batch size that yields a reasonably large craft count per cycle to amortize travel
    # aim for ~ enough gathers to produce 200-2000 crafted items depending on level scale, capped so total loop reps>=1
    target_items = max(50, total_ticks // 20)
    g = max(best["gathers_needed"], target_items)

    used = 0
    first = True
    while True:
        cost = batch_cost(g)
        if used + cost > budget:
            # shrink g to fit remaining budget, else stop
            remaining = budget - used
            # minimal loop needs at least gathers_needed + travel to sell 1 item
            min_cost = batch_cost(best["gathers_needed"])
            if remaining < min_cost:
                break
            # binary search g that fits
            lo, hi = best["gathers_needed"], g
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if batch_cost(mid) <= remaining:
                    lo = mid
                else:
                    hi = mid - 1
            g = lo
            if g < best["gathers_needed"]:
                break
            cost = batch_cost(g)

        for _ in range(g):
            actions.append({"type": "gather"})

        if raw_mode:
            items = g * best["yield_amt"]
            if best["sell_dist"] > 0:
                actions += hops_to_actions(adj, best["node"], best["sell_town"])
            actions.append({"type": "sell", "item": best["resource"], "quantity": items})
        else:
            items = g * best["yield_amt"] // best["need_per_item"]
            if items <= 0:
                break
            if best["aff_dist"] > 0:
                actions += hops_to_actions(adj, best["node"], best["affinity_town"])
            actions.append({"type": "craft", "item": best["recipe"], "quantity": items})
            if best["affinity_town"] != best["sell_town"]:
                actions += hops_to_actions(adj, best["affinity_town"], best["sell_town"])
            actions.append({"type": "sell", "item": best["recipe"], "quantity": items})

        used += cost
        actions += hops_to_actions(adj, best["sell_town"], best["node"])

        if used + batch_cost(best["gathers_needed"]) > budget:
            break

    return actions, best


if __name__ == "__main__":
    lvl_num = int(sys.argv[1])
    with open(f"level{lvl_num}.json") as f:
        level = json.load(f)
    with open("constants.json") as f:
        const = json.load(f)

    actions, best = generate_actions(level, const, lvl_num)
    print("Best loop:", json.dumps(best, indent=2))
    print(f"Generated {len(actions)} actions")

    sim = Simulator(level, const, level_number=lvl_num)
    log = sim.run(actions)

    # trim trailing actions once the run has ended (avoids pointless invalid tail entries)
    cutoff = len(actions)
    for e in log:
        if e.detail in ("run ended",) or "exceeds total_ticks" in e.detail:
            cutoff = e.index
            break
    actions = actions[:cutoff]

    sim = Simulator(level, const, level_number=lvl_num)
    log = sim.run(actions)
    invalid = [e for e in log if not e.valid]
    print(f"Invalid actions: {len(invalid)} / {len(log)}")
    for e in invalid[:10]:
        print(" ", e.index, e.action, e.detail)
    print(sim.summary())

    with open(f"output_level{lvl_num}.txt", "w") as f:
        json.dump({"actions": actions}, f)
    print(f"Wrote output_level{lvl_num}.txt")