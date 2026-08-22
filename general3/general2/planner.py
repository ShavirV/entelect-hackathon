"""
Planning utilities: shortest paths over the map graph, and search for the
best repeatable "trade cycles" (gather/buy -> craft -> sell) and the
cheapest way to stock up on construction components for upgrades.

Design note: because buy/sell actions cost a FLAT 1 tick regardless of
quantity (per the activity table), bulk-buying inputs at a town that
produces them is almost always far cheaper in ticks than gathering at a
node, provided you can afford the Enteloot. Gathering only wins when
Enteloot is the binding constraint (e.g. very early game) or when no town
sells the resource you need. The planner accounts for both options and
picks whichever is better for the ticks-vs-enteloot trade-off requested.
"""
from __future__ import annotations
import json
import heapq
from collections import defaultdict

# Load constants from JSON
with open('constants.json') as f:
    _CONSTANTS_DATA = json.load(f)

RESOURCES = _CONSTANTS_DATA['resources']
RECIPES = _CONSTANTS_DATA['recipes']
COMPONENTS = _CONSTANTS_DATA['components']

def sell_price(resource):
    """Get sell price for a resource"""
    if resource in RESOURCES:
        return RESOURCES[resource]['sell_price']
    return None

def buy_price(resource):
    """Get buy price for a resource"""
    if resource in RESOURCES:
        return RESOURCES[resource]['buy_price']
    return None

CRAFTABLES = {**RECIPES, **COMPONENTS}

_DEPTH_MEMO = {}


def craft_depth(item):
    """Dependency depth of a craftable (0 = only raw resources as inputs)."""
    if item in _DEPTH_MEMO:
        return _DEPTH_MEMO[item]
    recipe = COMPONENTS.get(item) or RECIPES.get(item)
    if recipe is None:
        return 0
    d = 0
    for sub in recipe["inputs"]:
        if sub in COMPONENTS or sub in RECIPES:
            d = max(d, 1 + craft_depth(sub))
    _DEPTH_MEMO[item] = d
    return d


def plan_multi_chain(gi, hub: str, items_qty: dict, has_pickaxe=False):
    """
    Produce the given {item: qty} map (construction components, recipe
    goods, and/or tool inputs) sitting in inventory at `hub`, handling
    arbitrarily nested component chains (e.g. bricks -> mortar ->
    clay+stone) and batching raw-resource purchases/gathers ONE time across
    the WHOLE combined tree (important: an upgrade often needs several
    components that separately need the same base resource, e.g. wood).
    Craft actions are emitted in correct dependency order (deepest first).
    Returns an action list, or None if some input is unreachable.
    """
    craft_qty = defaultdict(int)
    resource_need = defaultdict(int)

    def accumulate(it, q):
        if it in RESOURCES:
            resource_need[it] += q
            return
        recipe = COMPONENTS.get(it) or RECIPES.get(it)
        if recipe is None:
            raise ValueError(f"unknown craftable: {it}")
        craft_qty[it] += q
        for sub_item, sub_qty in recipe["inputs"].items():
            accumulate(sub_item, sub_qty * q)

    for item, qty in items_qty.items():
        accumulate(item, qty)

    actions = []
    for res, need in resource_need.items():
        opts = input_source_plan(gi, hub, res, need, has_pickaxe)
        if not opts:
            return None
        best = min(opts, key=lambda o: o["ticks"] + o["enteloot_cost"] / 20.0)
        actions += best["actions"]

    for it in sorted(craft_qty.keys(), key=craft_depth):
        actions.append({"type": "craft", "item": it, "quantity": craft_qty[it]})

    return actions


def plan_full_chain(gi, hub: str, item: str, qty: int, has_pickaxe=False):
    """Convenience wrapper around plan_multi_chain for a single top-level item."""
    return plan_multi_chain(gi, hub, {item: qty}, has_pickaxe)


class GraphIndex:
    def __init__(self, level_data: dict, allow_fast: bool = True):
        self.towns = level_data["towns"]
        self.nodes = level_data["nodes"]
        self.routes = level_data["routes"]
        self.allow_fast = allow_fast

        # adjacency using the cheapest-in-ticks edge per (from,to); track toll too
        self.adj = defaultdict(list)  # vertex -> list of (to, weight, toll, fast)
        for r in self.routes:
            a, b = r["between"]
            w = r["weight"]
            toll = r.get("toll", 0)
            fast = toll > 0
            if fast and not allow_fast:
                continue
            self.adj[a].append((b, w, toll, fast))
            self.adj[b].append((a, w, toll, fast))

        self.vertices = set(self.towns.keys()) | set(self.nodes.keys())

        # producers: resource -> list of town names that produce it
        self.producers = defaultdict(list)
        for tname, t in self.towns.items():
            for res in t["production"]["resources"]:
                self.producers[res].append(tname)

        # nodes by resource
        self.nodes_by_resource = defaultdict(list)
        for nid, n in self.nodes.items():
            self.nodes_by_resource[n["resource"]].append(nid)

        self.affinity_towns = [t for t, v in self.towns.items() if "crafting" in v.get("affinities", [])]

        self._dist_cache = {}

    def shortest_paths_from(self, src):
        """Dijkstra by tick-weight (prefers cheapest path; may use fast routes
        if allow_fast). Returns (dist, toll_total, prev) where prev[v] =
        (u, weight, toll, fast) is the edge used to reach v."""
        if src in self._dist_cache:
            return self._dist_cache[src]
        dist = {src: 0}
        toll_total = {src: 0}
        prev = {}
        pq = [(0, 0, src)]
        seen = set()
        while pq:
            d, tl, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            for (v, w, toll, fast) in self.adj.get(u, []):
                nd = d + w
                ntl = tl + toll
                if v not in dist or nd < dist[v] or (nd == dist[v] and ntl < toll_total.get(v, 1e18)):
                    dist[v] = nd
                    toll_total[v] = ntl
                    prev[v] = (u, w, toll, fast)
                    heapq.heappush(pq, (nd, ntl, v))
        result = (dist, toll_total, prev)
        self._dist_cache[src] = result
        return result

    def dist(self, a, b):
        d, _, _ = self.shortest_paths_from(a)
        return d.get(b, None)

    def toll(self, a, b):
        d, tl, _ = self.shortest_paths_from(a)
        if b not in d:
            return None
        return tl.get(b, 0)

    def travel_sequence(self, a, b):
        """List of {'type':'travel','destination':X,'fast':bool} actions
        tracing the shortest tick-path from a to b (empty list if a==b,
        None if unreachable)."""
        if a == b:
            return []
        d, _, prev = self.shortest_paths_from(a)
        if b not in d:
            return None
        chain = []
        cur = b
        while cur != a:
            u, w, toll, fast = prev[cur]
            chain.append({"type": "travel", "destination": cur, "fast": fast})
            cur = u
        chain.reverse()
        return chain

    def nearest_node_for_resource(self, src, resource):
        """Returns (node_id, dist) for the nearest node yielding `resource`."""
        d, _ = self.shortest_paths_from(src)[:2]
        best = None
        for nid in self.nodes_by_resource.get(resource, []):
            if nid in d:
                if best is None or d[nid] < best[1]:
                    best = (nid, d[nid])
        return best

    def nearest_producer_town(self, src, resource, exclude=None):
        d, _ = self.shortest_paths_from(src)[:2]
        best = None
        for tname in self.producers.get(resource, []):
            if exclude and tname == exclude:
                continue
            if tname in d:
                if best is None or d[tname] < best[1]:
                    best = (tname, d[tname])
        return best


def input_source_plan(gi: GraphIndex, hub: str, resource: str, qty_needed: int, has_pickaxe=False):
    """
    Decide the cheapest way to get `qty_needed` units of `resource` available
    at `hub`, choosing between:
      (a) buy locally at hub if hub produces it (1 tick flat)
      (b) travel to nearest OTHER producing town, buy (1 tick), travel back
      (c) travel to nearest resource node, gather enough, travel back
    Returns dict: {method, ticks, enteloot_cost, actions:[...]} where actions
    is a list of primitive-action templates (destination-relative), to be
    stitched in by the caller (who knows current location for the *first*
    leg only; hub<->source round trips are self-contained).
    """
    town = gi.towns.get(hub, {})
    options = []

    if resource in town.get("production", {}).get("resources", {}):
        cost = qty_needed * buy_price(resource)
        options.append({
            "method": "buy_local", "ticks": 1, "enteloot_cost": cost,
            "actions": [{"type": "buy", "item": resource, "quantity": qty_needed}],
        })

    other = gi.nearest_producer_town(hub, resource, exclude=hub)
    if other:
        tname, d = other
        there = gi.travel_sequence(hub, tname)
        back = gi.travel_sequence(tname, hub)
        if there is not None and back is not None:
            cost = qty_needed * buy_price(resource)
            options.append({
                "method": "buy_other", "ticks": 2 * d + 1, "enteloot_cost": cost,
                "actions": there + [{"type": "buy", "item": resource, "quantity": qty_needed}] + back,
            })

    nn = gi.nearest_node_for_resource(hub, resource)
    if nn:
        nid, d = nn
        there = gi.travel_sequence(hub, nid)
        back = gi.travel_sequence(nid, hub)
        if there is not None and back is not None:
            yield_ = gi.nodes[nid]["yield"]
            gt = gi.nodes[nid]["gather-time"]
            if has_pickaxe:
                gt = max(1, gt - 1)
            n_gathers = -(-qty_needed // yield_)  # ceil
            gather_ticks = n_gathers * gt
            actions = there + [{"type": "gather"} for _ in range(n_gathers)] + back
            options.append({
                "method": "gather", "ticks": 2 * d + gather_ticks, "enteloot_cost": 0,
                "actions": actions, "overproduces": n_gathers * yield_ - qty_needed,
            })

    if not options:
        return None
    # Pick by enteloot-per-tick efficiency isn't quite right here (we want the
    # option the caller's higher-level rate function scores); return all,
    # caller picks based on strategy (cheapest ticks vs cheapest enteloot).
    return options


def best_recipe_cycles(gi: GraphIndex, current_location: str, has_pickaxe=False, top_n=8):
    """
    For every sellable recipe, evaluate candidate (hub, sell_town) pairs and
    return the best few cycles ranked by enteloot-profit-per-tick. Each cycle
    starts and ends at `hub` (so it can be repeated), plus a one-time
    travel-in from current_location is reported separately.
    """
    results = []
    for item, recipe in RECIPES.items():
        for hub in gi.towns.keys():
            town = gi.towns[hub]
            affinity = "crafting" in town.get("affinities", [])
            craft_time_per = 1 if affinity else 2
            # batch size: craft a reasonably large batch to amortize travel
            # (scaled below by caller; here we just compute PER-UNIT economics)
            per_unit_input_ticks = 0
            per_unit_input_cost = 0
            feasible = True
            for res, qty in recipe["inputs"].items():
                opts = input_source_plan(gi, hub, res, qty, has_pickaxe)
                if not opts:
                    feasible = False
                    break
                # prefer cheapest enteloot option that doesn't blow up ticks too much:
                # score by ticks + enteloot/10 as a soft combined cost
                best_opt = min(opts, key=lambda o: o["ticks"] + o["enteloot_cost"] / 20.0)
                per_unit_input_ticks += best_opt["ticks"]
                per_unit_input_cost += best_opt["enteloot_cost"]
            if not feasible:
                continue
            per_unit_craft_ticks = craft_time_per

            # best sell town
            best_sell_town, best_price = None, -1
            for tname, t in gi.towns.items():
                price = t.get("item-rates", {}).get(item, 0)
                if price > best_price:
                    best_price, best_sell_town = price, tname
            if best_sell_town is None:
                continue
            d_sell = gi.dist(hub, best_sell_town) or 0
            sell_round_trip = 0 if best_sell_town == hub else 2 * d_sell
            per_unit_sell_ticks = 0  # amortized below with batch size

            results.append({
                "item": item, "hub": hub, "sell_town": best_sell_town,
                "sell_price": best_price,
                "per_unit_input_ticks": per_unit_input_ticks,
                "per_unit_input_cost": per_unit_input_cost,
                "per_unit_craft_ticks": per_unit_craft_ticks,
                "sell_round_trip_ticks": sell_round_trip,
                "hub_dist_from_current": gi.dist(current_location, hub) or 0,
            })

    # Rank using a representative batch size (10 units) to amortize the fixed
    # 1-tick buy/sell/travel overhead realistically.
    BATCH = 10
    scored = []
    for r in results:
        total_ticks = (r["per_unit_input_ticks"] + r["per_unit_craft_ticks"]) * BATCH
        total_ticks += r["sell_round_trip_ticks"] + 1  # +1 sell action
        revenue = r["sell_price"] * BATCH
        cost = r["per_unit_input_cost"] * BATCH
        profit = revenue - cost
        rate = profit / total_ticks if total_ticks > 0 else -1e18
        scored.append({**r, "batch": BATCH, "profit_per_batch": profit,
                        "ticks_per_batch": total_ticks, "rate": rate})
    scored.sort(key=lambda r: -r["rate"])
    return scored[:top_n]


def build_recipe_cycle_actions(gi: GraphIndex, hub: str, sell_town: str, item: str,
                                batch: int, has_pickaxe=False):
    """Concrete, executable action list for ONE full cycle of crafting+selling
    `batch` units of `item`, starting and ending at `hub`."""
    recipe = RECIPES[item]
    actions = []
    for res, qty in recipe["inputs"].items():
        opts = input_source_plan(gi, hub, res, qty * batch, has_pickaxe)
        if not opts:
            return None
        best_opt = min(opts, key=lambda o: o["ticks"] + o["enteloot_cost"] / 20.0)
        actions += best_opt["actions"]
    actions.append({"type": "craft", "item": item, "quantity": batch})
    to_sell = gi.travel_sequence(hub, sell_town)
    back = gi.travel_sequence(sell_town, hub)
    if to_sell is None or back is None:
        return None
    actions += to_sell
    actions.append({"type": "sell", "item": item, "quantity": batch})
    actions += back
    return actions


def best_raw_sell_cycle(gi: GraphIndex, current_location: str, has_pickaxe=False, top_n=5):
    """Gather at a node then sell the raw resource at the nearest town (raw
    sell price is global, so nearest reachable town is fine)."""
    results = []
    for nid, node in gi.nodes.items():
        resource = node["resource"]
        yield_ = node["yield"]
        gt = node["gather-time"]
        if has_pickaxe:
            gt = max(1, gt - 1)
        price = sell_price(resource)
        # nearest town from node
        d, _ = gi.shortest_paths_from(nid)[:2]
        nearest_town = None
        for tname in gi.towns:
            if tname in d:
                if nearest_town is None or d[tname] < nearest_town[1]:
                    nearest_town = (tname, d[tname])
        if nearest_town is None:
            continue
        town_name, d_town = nearest_town
        # cycle: node -> gather x times -> town -> sell -> node ...
        GATHERS = 5
        ticks = GATHERS * gt + d_town + 1 + d_town  # gather, travel to town, sell, travel back
        revenue = GATHERS * yield_ * price
        rate = revenue / ticks if ticks else -1e18
        results.append({
            "resource": resource, "node": nid, "sell_town": town_name,
            "gathers": GATHERS, "ticks_per_cycle": ticks, "revenue_per_cycle": revenue,
            "rate": rate, "node_dist_from_current": gi.dist(current_location, nid),
        })
    results.sort(key=lambda r: -r["rate"])
    return results[:top_n]


def build_raw_sell_cycle_actions(gi: GraphIndex, node: str, sell_town: str, gathers: int):
    to_town = gi.travel_sequence(node, sell_town)
    back = gi.travel_sequence(sell_town, node)
    if to_town is None or back is None:
        return None
    resource = gi.nodes[node]["resource"]
    actions = [{"type": "gather"} for _ in range(gathers)]
    actions += to_town
    total_yield = gathers * gi.nodes[node]["yield"]
    actions.append({"type": "sell", "item": resource, "quantity": total_yield})
    actions += back
    return actions