"""
Age of Enteland - shared solver utilities (Levels 2-4).

The Level 1 solver's "camp the best node" strategy stops being sufficient
once crafting/building (Level 2), fast routes/mines/tools (Level 3), and
upkeep (Level 4) enter the picture: the dominant lever becomes WHICH
upgrades to build, in WHICH towns, and whether the plan actually fits the
Enteloot and tick budgets - not just node selection.

This module factors out everything that Levels 2, 3 and 4 share:
  - graph utilities (dijkstra over standard routes, with an optional
    permanent "boots" travel-time delta applied uniformly),
  - a topologically-safe, budget-agnostic upgrade CANDIDATE ORDER (per-town
    production upgrades by best score/enteloot_cost ratio, then the civic
    chain in an order that always satisfies prerequisites), interleaved
    round-robin across towns so that, all else equal, value is spread
    across towns rather than piled into one (the spec calls out that
    "spreading development across towns earns a multiplier"),
  - a bill-of-materials exploder for components/recipes/tools,
  - a FEASIBILITY-TRIMMED planner: build the full candidate list, then
    binary-search the longest PREFIX of it that actually replays through
    the real Engine with zero invalid actions and within the tick budget.
    This directly fixes the Level 2 solver's core bug, which requested
    every upgrade in every town regardless of whether the run could
    afford it, then shipped a plan with dozens of "cannot afford build"
    / "prerequisite not met" invalid actions and thousands of unused
    ticks at the end.

Level-specific concerns (fast routes, ore/mine gathering, iron-fittings,
tools, upkeep) are layered on top in solver_level3.py / solver_level4.py,
which both call into this module rather than re-implementing it.
"""

import heapq
import math
from collections import defaultdict

from engine import Engine


PRODUCTION_UPGRADES = [
    "farmhouse",
    "pier",
    "fertilised-fields",
    "quarry",
    "woodlands",
    "pottery-house",
]

# A single fixed order for the civic chain that is always prerequisite-safe:
#   rec-center   <- any 1 production upgrade
#   fire-station <- any 2 production upgrades
#   school       <- rec-center (specific)
#   police-station <- fire-station (specific, Level 3+ only, needs iron-fittings)
#   library      <- school (specific)
CIVIC_ORDER = ["rec-center", "fire-station", "school", "police-station", "library"]


# ---------------------------------------------------------------------------
# Graph utilities
# ---------------------------------------------------------------------------

def build_adjacency(routes):
    adj = defaultdict(list)
    for r in routes:
        a, b = r["between"]
        toll = r.get("toll", 0)
        fast = toll > 0
        edge = {"weight": r["weight"], "toll": toll, "fast": fast}
        adj[a].append((b, edge))
        adj[b].append((a, edge))
    return adj


def dijkstra(adj, source, allow_fast=False, travel_delta=0, travel_min=1):
    """Shortest travel-time to every reachable vertex using standard routes
    only (allow_fast=False, the correct default pre-Level-3). `travel_delta`
    lets a caller model a permanent per-edge reduction (the `boots` tool),
    floored at `travel_min` per edge, matching the tool's effect definition.
    """
    dist = {source: 0}
    prev = {}
    pq = [(0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist.get(u):
            continue
        for v, edge in adj.get(u, []):
            if edge["fast"] and not allow_fast:
                continue
            w = edge["weight"]
            if travel_delta:
                w = max(travel_min, w + travel_delta)
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def reconstruct_path(prev, source, target):
    if source == target:
        return [source]
    path = [target]
    while path[-1] != source:
        path.append(prev[path[-1]])
    return list(reversed(path))


def path_actions(adj, source, target, travel_delta=0, travel_min=1):
    """Travel actions for the shortest standard-route path. Fast routes are
    deliberately not auto-selected here (see solver_level3 docstring for
    why) - every hop is a plain `travel` action with `fast` omitted."""
    if source == target:
        return []
    _, prev = dijkstra(adj, source, travel_delta=travel_delta, travel_min=travel_min)
    path = reconstruct_path(prev, source, target)
    return [{"type": "travel", "destination": v} for v in path[1:]]


# ---------------------------------------------------------------------------
# Bill of materials
# ---------------------------------------------------------------------------

def explode(target_items, constants, raw_names):
    """Recursively expand desired (item, quantity) pairs into
    (craft_totals, raw_totals, craft_order), where craft_order is a valid
    topological build order (dependencies before dependants). Understands
    components, recipes, and tools (tools behave like components with
    once_per_run=1 handled by the caller)."""
    components = constants["components"]
    recipes = constants["recipes"]
    tools = constants.get("tools", {})

    def lookup(item):
        return components.get(item) or recipes.get(item) or tools.get(item)

    craft_totals = defaultdict(int)
    raw_totals = defaultdict(int)
    stack = list(target_items)

    while stack:
        item, quantity = stack.pop()
        if quantity <= 0:
            continue
        if item in raw_names:
            raw_totals[item] += quantity
            continue
        definition = lookup(item)
        if definition is None:
            raise ValueError(f"Unknown craftable item in BOM: {item}")
        craft_totals[item] += quantity
        for input_item, amount in definition["inputs"].items():
            stack.append((input_item, amount * quantity))

    craft_order = []
    visited = set()

    def visit(item):
        if item in visited or item not in craft_totals:
            return
        visited.add(item)
        definition = lookup(item)
        for input_item in definition["inputs"]:
            visit(input_item)
        craft_order.append(item)

    for item in craft_totals:
        visit(item)

    return dict(craft_totals), dict(raw_totals), craft_order


# ---------------------------------------------------------------------------
# Upgrade candidate ordering
# ---------------------------------------------------------------------------

def candidate_upgrade_order(constants, town_names, level_number):
    """Global priority order of (town, upgrade) pairs.

    Within a town: production upgrades sorted by enteloot_cost/score_value
    (cheapest score first), then the civic chain in CIVIC_ORDER (which is
    always prerequisite-safe). Across towns: round-robin, so a budget-
    trimmed prefix of this list naturally spreads early value across towns
    rather than maxing out one town before starting the next.
    """
    production = constants["upgrades"]["production"]
    civic = constants["upgrades"]["civic"]

    civic_seq = [u for u in CIVIC_ORDER if civic[u].get("min_level", 2) <= level_number]

    per_town_seq = {}
    for name in town_names:
        prod_sorted = sorted(
            PRODUCTION_UPGRADES,
            key=lambda u: production[u]["enteloot_cost"] / production[u]["score_value"],
        )
        per_town_seq[name] = prod_sorted + civic_seq

    ordered_towns = sorted(town_names)
    max_len = max((len(s) for s in per_town_seq.values()), default=0)
    ordered = []
    for i in range(max_len):
        for t in ordered_towns:
            seq = per_town_seq[t]
            if i < len(seq):
                ordered.append((t, seq[i]))
    return ordered


def upgrades_per_town_from_pairs(pairs):
    result = defaultdict(list)
    for town, upgrade in pairs:
        result[town].append(upgrade)
    return dict(result)


def upgrade_def(constants, upgrade):
    if upgrade in constants["upgrades"]["production"]:
        return constants["upgrades"]["production"][upgrade]
    return constants["upgrades"]["civic"][upgrade]


def bom_for_pairs(constants, pairs):
    """Top-level component requirements + running enteloot/build-tick totals
    for a chosen list of (town, upgrade) pairs."""
    top_level = []
    enteloot_cost = 0
    build_ticks = 0
    score_value = 0
    for _town, upgrade in pairs:
        d = upgrade_def(constants, upgrade)
        for component, qty in d["components"].items():
            top_level.append((component, qty))
        enteloot_cost += d["enteloot_cost"]
        build_ticks += d["build_time"]
        score_value += d["score_value"]
    return top_level, enteloot_cost, build_ticks, score_value


# ---------------------------------------------------------------------------
# Passive production (for trickle-aware raw requirements)
# ---------------------------------------------------------------------------

def passive_resource_at_tick(level, tick):
    inventory = defaultdict(int)
    for town in level["towns"].values():
        rate = town["production"]["rate"]
        if rate <= 0:
            continue
        cycles = tick // rate
        for resource, amount in town["production"]["resources"].items():
            inventory[resource] += cycles * amount
    return dict(inventory)


# ---------------------------------------------------------------------------
# Gathering / town touring
# ---------------------------------------------------------------------------

def choose_gather_node(resource, quantity, nodes, dist_from_hub, gather_delta=0, gather_min=1):
    candidates = []
    for node_name, node in nodes.items():
        if node["resource"] != resource:
            continue
        if node_name not in dist_from_hub:
            continue
        gtime = max(gather_min, node["gather-time"] + gather_delta)
        gathers = math.ceil(quantity / node["yield"])
        travel = 2 * dist_from_hub[node_name]
        ticks = travel + gathers * gtime
        candidates.append((ticks, node_name, gathers, gtime))
    if not candidates:
        raise ValueError(f"No reachable node produces resource {resource}")
    candidates.sort(key=lambda x: (x[0], x[1]))
    ticks, node_name, gathers, gtime = candidates[0]
    return {"node": node_name, "gathers": gathers, "ticks": ticks, "gather_time": gtime}


def choose_town_tour(adj, start, towns, travel_delta=0, travel_min=1):
    remaining = set(towns)
    remaining.discard(start)
    order = []
    current = start
    while remaining:
        distances, _ = dijkstra(adj, current, travel_delta=travel_delta, travel_min=travel_min)
        reachable = [t for t in remaining if t in distances]
        if not reachable:
            raise ValueError(f"Cannot reach remaining towns from {current}: {sorted(remaining)}")
        next_town = min(reachable, key=lambda t: (distances[t], t))
        order.append(next_town)
        remaining.remove(next_town)
        current = next_town
    return order


def best_income_recipe(constants, level, hub, level_number, restrict_to_hub=False,
                        amortize_qty=1):
    """Pick the (recipe, sell_town) combination with the best Enteloot per
    tick (gather + travel + craft + one-time travel to the best-paying
    reachable town + sell). Crafted-good prices vary meaningfully by town
    (item-rates), so restricting the sale to the hub - as the earlier
    version of this function did - leaves real Enteloot on the table
    whenever a nearby town pays noticeably more than the hub for the same
    good.

    `amortize_qty` controls how the ONE-TIME travel costs (the round trip
    out to each input's gather node, and the trip to the sell town) are
    weighted against the recurring per-unit costs (gather + craft time).
    Those trips happen once per tail batch, not once per unit crafted - a
    batch that crafts thousands of units pays that travel exactly once, so
    its per-unit cost is travel/batch_size, not travel. The previous
    default (amortize_qty=1, i.e. charge the FULL round trip against a
    single unit) was fine for the small shortfall-funding batch inside
    build_actions_for_pairs (batch size there really is small), but was
    silently reused for the large end-of-run tail batch too, where it
    systematically under-rated recipes/nodes that are merely a bit farther
    from the hub - even when their price or gather rate would win easily
    once the trip is amortized over a huge batch. Callers building a large
    batch (see plan_income_tail) should pass a large amortize_qty (their
    expected batch size, or a generously large stand-in) so recipe
    selection matches the batch's real, near-steady-state economics.

    `restrict_to_hub=True` reproduces the old hub-only behaviour (sell_town
    forced to hub, no extra travel). Used for the small shortfall-funding
    batch inside build_actions_for_pairs, where selling elsewhere would
    require inserting travel into the middle of the build tour for a
    typically small amount of Enteloot - not worth the complexity there.
    The tail batch (a single big one-shot sale at the very end of the plan)
    is where the sell-town optimization actually pays off, so it's applied
    only there.
    """
    town = level["towns"][hub]
    adj = build_adjacency(level["routes"])
    dist, _ = dijkstra(adj, hub)  # standard shortest distances (no tools)
    craft_time = constants["constants"]["craft_time_affinity"] if "crafting" in town.get("affinities", []) else constants["constants"]["craft_time_base"]
    best = None  # (recipe_name, enteloot_per_tick, price, sell_town, travel_to_sell_town)
    for name, recipe in constants["recipes"].items():
        min_level = recipe.get("min_level")
        if min_level and level_number < min_level:
            continue
        # Gather/craft overhead (recipe- and hub-specific, independent of
        # where we ultimately sell). The 2*best_dist round trip is a
        # ONE-TIME cost for the whole batch, so it's amortized over
        # amortize_qty here rather than charged in full per unit.
        gather_craft_ticks = craft_time  # craft
        feasible = True
        for resource, amt in recipe["inputs"].items():
            best_node = None
            best_dist = None
            for node_name, node in level["nodes"].items():
                if node["resource"] != resource or node_name not in dist:
                    continue
                if best_dist is None or dist[node_name] < best_dist:
                    best_dist = dist[node_name]
                    best_node = node_name
            if best_node is None:
                feasible = False
                break
            gather_time = level["nodes"][best_node]["gather-time"]
            yield_per = level["nodes"][best_node]["yield"]
            gathers = (amt + yield_per - 1) // yield_per
            gather_craft_ticks += (2 * best_dist) / amortize_qty + gathers * gather_time
        if not feasible:
            continue

        sell_town_candidates = [hub] if restrict_to_hub else list(level["towns"])
        for sell_town_name in sell_town_candidates:
            price = level["towns"][sell_town_name].get("item-rates", {}).get(name)
            if price is None:
                continue
            travel_to_sell = dist.get(sell_town_name)
            if travel_to_sell is None:
                continue
            # travel_to_sell is also a one-time trip for the whole batch.
            total_ticks = gather_craft_ticks + travel_to_sell / amortize_qty + 1  # + sell action
            enteloot_per_tick = price / total_ticks
            if best is None or enteloot_per_tick > best[1]:
                best = (name, enteloot_per_tick, price, sell_town_name, travel_to_sell)
    return best


# ---------------------------------------------------------------------------
# Action construction for a chosen set of (town, upgrade) build pairs
# ---------------------------------------------------------------------------

def build_actions_for_pairs(constants, level, pairs, hub, extra_target_items=None,
                             travel_delta=0, travel_min=1, gather_delta=0, gather_min=1,
                             level_number=2, use_upkeep=False):
    """Construct: travel-to-hub -> gather raw materials -> craft components
    (+ any extra_target_items, e.g. tools) at the crafting hub -> tour the
    involved towns building in per-town prerequisite-safe order.

    If the requested upgrades cost more Enteloot than the run starts with,
    an income batch (craft-and-sell of the hub's best-paying recipe) is
    automatically folded in before the build tour, sized to cover the
    shortfall. Without this, a plan can gather/craft every component it
    needs and still have every `build` action rejected for "cannot afford
    build" the moment Enteloot runs out - components alone don't buy
    anything.

    travel_delta/gather_delta let a caller pre-apply tool effects (boots /
    pickaxe) that are already owned by the time this plan executes, so the
    action count/order matches what the Engine will actually charge.
    """
    adj = build_adjacency(level["routes"])
    raw_names = set(constants["resources"])

    top_level, enteloot_cost, build_ticks, score_value = bom_for_pairs(constants, pairs)
    if extra_target_items:
        top_level = list(extra_target_items) + top_level

    income_qty = 0
    income_recipe = None
    shortfall = enteloot_cost - level["run"]["starting_enteloot"]
    if shortfall > 0:
        picked = best_income_recipe(constants, level, hub, level_number, restrict_to_hub=True)
        if picked is not None:
            income_recipe, _epc, price, _sell_town, _travel = picked
            income_qty = math.ceil(shortfall / price)
            top_level = [(income_recipe, income_qty)] + top_level

    craft_totals, raw_totals, craft_order = explode(top_level, constants, raw_names)

    # NOTE: we deliberately do NOT discount raw_totals by passive town
    # trickle projected over the whole run. Crafting/building generally
    # happens early, long before that much trickle has actually
    # accumulated (Assumption 5/6 credit it gradually, on the clock) - so
    # subtracting a full-run trickle estimate here previously caused real
    # "insufficient <resource>" invalid actions whenever gathering was
    # skipped for a resource the plan assumed would already be sitting in
    # inventory. Gathering the full requirement costs a few extra ticks
    # but is always correct; passive trickle then arrives as pure bonus
    # inventory on top of what was actively gathered.
    remaining_raw = dict(raw_totals)

    actions = []
    current = level["run"]["starting_town"]

    actions.extend(path_actions(adj, current, hub, travel_delta, travel_min))
    current = hub

    dist_from_hub, _ = dijkstra(adj, hub, travel_delta=travel_delta, travel_min=travel_min)
    node_choices = {}
    for resource, quantity in remaining_raw.items():
        if quantity <= 0:
            continue
        node_choices[resource] = choose_gather_node(
            resource, quantity, level["nodes"], dist_from_hub, gather_delta, gather_min,
        )

    remaining_nodes = {c["node"] for c in node_choices.values()}
    while remaining_nodes:
        distances, prev = dijkstra(adj, current, travel_delta=travel_delta, travel_min=travel_min)
        reachable = [n for n in remaining_nodes if n in distances]
        if not reachable:
            raise ValueError(f"Cannot reach remaining gather nodes from {current}")
        next_node = min(reachable, key=lambda n: (distances[n], n))
        path = reconstruct_path(prev, current, next_node)
        for dest in path[1:]:
            actions.append({"type": "travel", "destination": dest})
        resource = level["nodes"][next_node]["resource"]
        choice = node_choices[resource]
        for _ in range(choice["gathers"]):
            actions.append({"type": "gather"})
        current = next_node
        remaining_nodes.remove(next_node)

    if current != hub:
        actions.extend(path_actions(adj, current, hub, travel_delta, travel_min))
        current = hub

    for item in craft_order:
        qty = craft_totals[item]
        if qty <= 0:
            continue
        if item in constants.get("tools", {}):
            # tools are once_per_run and crafted one unit at a time
            for _ in range(qty):
                actions.append({"type": "craft", "item": item, "quantity": 1})
        else:
            actions.append({"type": "craft", "item": item, "quantity": qty})

    # Sell any sellable recipe goods produced (i.e. the income batch, if
    # any - recipes are always leaf outputs, never consumed by other
    # recipes/components, so every recipe-typed item crafted here exists
    # purely to be sold for Enteloot).
    for item in craft_order:
        qty = craft_totals[item]
        if qty > 0 and item in constants["recipes"]:
            actions.append({"type": "sell", "item": item, "quantity": qty})

    upgrades_per_town = upgrades_per_town_from_pairs(pairs)
    tour = choose_town_tour(adj, hub, list(upgrades_per_town.keys()), travel_delta, travel_min)
    towns_to_visit = [hub] + tour if hub in upgrades_per_town else tour

    for town in towns_to_visit:
        if current != town:
            actions.extend(path_actions(adj, current, town, travel_delta, travel_min))
            current = town
        for upgrade in upgrades_per_town.get(town, []):
            actions.append({"type": "build", "upgrade": upgrade})
        if use_upkeep and town in upgrades_per_town:
            # Level 4: while we're already at this town for its builds,
            # spend 5 ticks doubling its Enteloot production for the next
            # 50 (or 75, with fire-station) ticks. Town trickle keeps
            # firing on the global clock after we leave (Assumption 5), so
            # this is "free" bonus income that can fund upgrades later in
            # the tour, at a fraction of a build's tick cost.
            actions.append({"type": "upkeep"})

    return actions, {
        "score_value": score_value,
        "enteloot_cost": enteloot_cost,
        "build_ticks": build_ticks,
        "node_choices": node_choices,
        "final_location": current,
    }


# ---------------------------------------------------------------------------
# Feasibility-trimmed planner
# ---------------------------------------------------------------------------

def replay(constants, level, level_number, actions):
    engine = Engine(constants, level, level_number=level_number)
    result = engine.run(actions)
    invalid = [e for e in result["log"] if not e["ok"]]
    return result, invalid


def plan_feasible_prefix(constants, level, level_number, ordered_pairs, hub,
                          extra_target_items=None, travel_delta=0, travel_min=1,
                          gather_delta=0, gather_min=1, use_upkeep=False):
    """Binary-search the longest prefix of `ordered_pairs` whose generated
    action list replays with zero invalid actions and within budget, then
    do a short linear back-off in case feasibility isn't perfectly
    monotonic (e.g. a longer prefix reaches a town tour in a different,
    slightly cheaper order). Returns (actions, metadata, kept_pairs).
    """
    n = len(ordered_pairs)

    def feasible(k):
        pairs = ordered_pairs[:k]
        if not pairs:
            return True, [], {"score_value": 0, "enteloot_cost": 0, "build_ticks": 0}
        try:
            actions, meta = build_actions_for_pairs(
                constants, level, pairs, hub, extra_target_items,
                travel_delta, travel_min, gather_delta, gather_min,
                level_number=level_number, use_upkeep=use_upkeep,
            )
        except ValueError:
            return False, None, None
        result, invalid = replay(constants, level, level_number, actions)
        ok = (not invalid) and result["final_tick"] <= level["run"]["total_ticks"]
        return ok, actions, meta

    lo, hi = 0, n
    best_k = 0
    best_actions, best_meta = [], {"score_value": 0, "enteloot_cost": 0, "build_ticks": 0}
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, actions, meta = feasible(mid)
        if ok:
            best_k, best_actions, best_meta = mid, actions, meta
            lo = mid + 1
        else:
            hi = mid - 1

    # Linear scan forward from best_k in case of non-monotonicity just past
    # the binary-search boundary (cheap safety net; small deltas only).
    k = best_k + 1
    while k <= min(n, best_k + 8):
        ok, actions, meta = feasible(k)
        if ok:
            best_k, best_actions, best_meta = k, actions, meta
        k += 1

    return best_actions, best_meta, ordered_pairs[:best_k]


# ---------------------------------------------------------------------------
# Tail income phase: once every buildable upgrade is exhausted (each can
# only be built once per town, so the candidate list is finite), there is
# nothing left to invest Enteloot in - but leftover ticks are NOT nothing:
# the spec is explicit that "Hoarded Enteloot scores far less than Enteloot
# invested", implying it still scores something, so idle ticks at the end
# of a run are pure waste. This phase converts them into one large
# gather -> craft -> sell batch of the hub's best-paying recipe (sized to
# fit the remaining tick budget almost exactly), optionally preceded by an
# upkeep trigger at the hub on Level 4 for a small bonus during the batch.
# ---------------------------------------------------------------------------

def _tail_batch_actions(constants, level, level_number, hub, current, adj, qty, use_upkeep):
    raw_names = set(constants["resources"])
    picked = best_income_recipe(constants, level, hub, level_number,
                                 amortize_qty=TAIL_AMORTIZE_QTY)
    if picked is None or qty <= 0:
        return [], None
    recipe_name, _epc, _price, sell_town, _travel_to_sell = picked

    actions = []
    actions.extend(path_actions(adj, current, hub))
    if use_upkeep:
        actions.append({"type": "upkeep"})

    craft_totals, raw_totals, craft_order = explode([(recipe_name, qty)], constants, raw_names)
    dist_from_hub, _ = dijkstra(adj, hub)
    node_choices = {}
    for resource, quantity in raw_totals.items():
        if quantity <= 0:
            continue
        node_choices[resource] = choose_gather_node(resource, quantity, level["nodes"], dist_from_hub)

    remaining_nodes = {c["node"] for c in node_choices.values()}
    cur = hub
    while remaining_nodes:
        distances, prev = dijkstra(adj, cur)
        reachable = [n for n in remaining_nodes if n in distances]
        if not reachable:
            raise ValueError("unreachable gather node in tail batch")
        nxt = min(reachable, key=lambda n: (distances[n], n))
        path = reconstruct_path(prev, cur, nxt)
        for dest in path[1:]:
            actions.append({"type": "travel", "destination": dest})
        resource = level["nodes"][nxt]["resource"]
        choice = node_choices[resource]
        for _ in range(choice["gathers"]):
            actions.append({"type": "gather"})
        cur = nxt
        remaining_nodes.remove(nxt)

    if cur != hub:
        actions.extend(path_actions(adj, cur, hub))
        cur = hub

    for item in craft_order:
        q = craft_totals[item]
        if q > 0:
            actions.append({"type": "craft", "item": item, "quantity": q})

    if cur != sell_town:
        actions.extend(path_actions(adj, cur, sell_town))
        cur = sell_town

    actions.append({"type": "sell", "item": recipe_name, "quantity": qty})
    return actions, recipe_name


TAIL_AMORTIZE_QTY = 100_000  # stand-in "large batch size" for one-time-trip
# amortization when picking the tail's recipe - the tail batch is sized to
# consume most of the run's remaining ticks (often thousands of crafted
# units), so its real per-unit cost is dominated by recurring gather/craft
# time, not the one-time round trips. Using the same amortize_qty=1 the
# small shortfall-funding batch uses here would systematically favor
# recipes/nodes merely because they're close to the hub, even when a
# farther node or better-paying town would win easily over a real batch of
# this size. This constant only affects which recipe/sell-town is judged
# "best" - it isn't a literal target quantity.


def _estimate_tail_ticks_per_unit(constants, level, level_number, hub):
    """Rough ticks-per-crafted-unit estimate (craft time + gather time for
    inputs, amortizing travel over a large batch) - used only to pick a
    sensible starting point for the batch-size search, not for correctness."""
    picked = best_income_recipe(constants, level, hub, level_number,
                                 amortize_qty=TAIL_AMORTIZE_QTY)
    if picked is None:
        return None, None
    recipe_name, _epc, _price, _sell_town, _travel_to_sell = picked
    recipe = constants["recipes"][recipe_name]
    town = level["towns"][hub]
    per_item_craft = (constants["constants"]["craft_time_affinity"]
                       if "crafting" in town.get("affinities", [])
                       else constants["constants"]["craft_time_base"])
    adj = build_adjacency(level["routes"])
    dist_from_hub, _ = dijkstra(adj, hub)
    ticks_per_unit = per_item_craft
    for resource, amt in recipe["inputs"].items():
        best_rate = None
        for node in level["nodes"].values():
            if node["resource"] != resource:
                continue
            rate = node["gather-time"] / node["yield"]
            if best_rate is None or rate < best_rate:
                best_rate = rate
        if best_rate is None:
            return None, None
        ticks_per_unit += amt * best_rate
    return recipe_name, ticks_per_unit


def liquidate_inventory_actions(constants, level, inventory, current_location):
    """Sell off everything still sitting in inventory, converting it from
    'held items value' into actual sale revenue / Enteloot.

    Passive town trickle (Assumption 5/6) credits resources from EVERY
    town automatically over the whole run, whether or not the player ever
    visits that town or gathers that resource - only towns whose resources
    happen to be needed for crafting/building ever get consumed. Over a
    run of tens of thousands of ticks across a dozen-plus towns, that
    trickle adds up to a huge pile of raw resources that nothing in the
    plan otherwise touches. It still shows up as 'held items value' at
    scoring time, but the spec is explicit that hoarded value scores less
    than realized value ("Hoarded Enteloot scores far less than Enteloot
    invested"; Level 1 grants an explicit multiplier for items actually
    sold) - so liquidating it is free money the plan was otherwise leaving
    on the table.

    Raw resources sell at a fixed global price regardless of location,
    so no travel is needed - just a `sell` action per resource that still
    has a positive balance. Any leftover *crafted* good (shouldn't
    normally happen, since the tail batch crafts exactly what it sells)
    is sold at whatever the current town pays for it, since it's not
    worth adding travel just to chase a better price on top-up dust.
    """
    raw_names = set(constants["resources"])
    recipes = constants["recipes"]
    town = level["towns"].get(current_location)

    actions = []
    for item, qty in inventory.items():
        if qty <= 0:
            continue
        if item in raw_names:
            actions.append({"type": "sell", "item": item, "quantity": qty})
        elif item in recipes and town and item in town.get("item-rates", {}):
            actions.append({"type": "sell", "item": item, "quantity": qty})
        # components/tools are never sellable - nothing to do for those.
    return actions


def plan_income_tail(constants, level, level_number, hub, prior_actions, use_upkeep=False,
                      max_tail_ticks=None):
    """Append a single income batch to `prior_actions`, sized via a bounded
    local search. Returns (combined_actions, result) - if no income recipe /
    no reachable inputs exist, returns prior_actions unchanged.

    `max_tail_ticks` caps how much of the remaining tick budget the batch is
    allowed to consume (None = old behaviour: use as much of the remaining
    budget as possible). This matters because both the batch-size search
    cost and the resulting action-list size scale with the ticks the batch
    is allowed to spend, while the marginal Enteloot from each additional
    unit sold is identical throughout - if hoarded Enteloot is worth less
    than invested Enteloot (per the spec) but still worth *something*, a
    capped batch captures most of that value for a small fraction of the
    actions/runtime of an uncapped one. Tune the cap per level based on
    measured results (see compare_tail_variants.py) rather than guessing.
    """
    total_ticks = level["run"]["total_ticks"]
    adj = build_adjacency(level["routes"])

    base_result, base_invalid = replay(constants, level, level_number, prior_actions)
    if base_invalid:
        return prior_actions, base_result
    current = base_result["final_location"]
    remaining_ticks = total_ticks - base_result["final_tick"]
    if remaining_ticks <= 0:
        return prior_actions, base_result
    if max_tail_ticks is not None:
        remaining_ticks = min(remaining_ticks, max_tail_ticks)

    # Reserve a small amount of headroom so the batch-size search doesn't
    # spend every last tick, leaving no room for the final inventory
    # liquidation below (each sell action is 1 tick; at most one per raw
    # resource type, so this is a tiny, fixed reservation regardless of
    # how large total_ticks is).
    liquidation_headroom = len(constants["resources"])
    remaining_ticks = max(0, remaining_ticks - liquidation_headroom)

    # Hard ceiling on ticks the batch itself may consume, independent of the
    # overall total_ticks budget. Previously max_tail_ticks only seeded the
    # initial guess - the grow-while-feasible search below would then climb
    # right back past it since its only stopping condition was the run's
    # full tick budget. Enforcing it inside feasible() is what actually caps
    # the batch (and therefore the action count).
    tail_tick_ceiling = base_result["final_tick"] + remaining_ticks

    recipe_name, ticks_per_unit = _estimate_tail_ticks_per_unit(constants, level, level_number, hub)
    if recipe_name is None:
        return prior_actions, base_result

    def feasible(qty):
        try:
            batch, _r = _tail_batch_actions(
                constants, level, level_number, hub, current, adj, qty, use_upkeep,
            )
        except ValueError:
            return False, None
        combined = prior_actions + batch
        result, invalid = replay(constants, level, level_number, combined)
        ok = ((not invalid) and result["final_tick"] <= total_ticks
              and result["final_tick"] <= tail_tick_ceiling)
        return ok, combined

    # Start from an analytic estimate (ignoring travel/toll overhead, hence
    # a safety margin), then locally search up/down for the exact ceiling.
    guess = max(1, int((remaining_ticks / ticks_per_unit) * 0.85))

    best_qty, best_combined = 0, prior_actions
    ok, combined = feasible(guess)
    if ok:
        best_qty, best_combined = guess, combined
        # grow while feasible
        step = max(1, guess // 4)
        qty = guess
        while step >= 1:
            while True:
                ok, combined = feasible(qty + step)
                if ok:
                    qty += step
                    best_qty, best_combined = qty, combined
                else:
                    break
            step //= 2
    else:
        # shrink from guess down to something feasible
        qty = guess
        step = max(1, guess // 4)
        while qty > 0 and not ok:
            qty = max(0, qty - step)
            ok, combined = feasible(qty)
        if ok:
            best_qty, best_combined = qty, combined
            step = max(1, step // 2)
            while step >= 1:
                while True:
                    ok, combined = feasible(best_qty + step)
                    if ok:
                        best_qty, best_combined = best_qty + step, combined
                    else:
                        break
                step //= 2

    final_result, final_invalid = replay(constants, level, level_number, best_combined)

    # Sell off whatever passive trickle / leftovers are still sitting in
    # inventory - this is what the headroom reserved above was for. Try
    # the full liquidation first; if for some reason it doesn't fit (e.g.
    # a level with an unusually tight budget), shrink the batch by one
    # more headroom-sized step and retry once rather than silently
    # dropping real, free Enteloot.
    if not final_invalid:
        liquidation = liquidate_inventory_actions(
            constants, level, final_result["final_inventory"], final_result["final_location"],
        )
        if liquidation:
            candidate = best_combined + liquidation
            candidate_result, candidate_invalid = replay(constants, level, level_number, candidate)
            if not candidate_invalid and candidate_result["final_tick"] <= total_ticks:
                return candidate, candidate_result
            # Retry with a smaller batch to free up the room this needed.
            shrink = max(1, best_qty // 20)
            retry_qty = max(0, best_qty - shrink)
            if retry_qty < best_qty:
                batch, _r = _tail_batch_actions(
                    constants, level, level_number, hub, current, adj, retry_qty, use_upkeep,
                )
                shrunk = prior_actions + batch
                shrunk_result, shrunk_invalid = replay(constants, level, level_number, shrunk)
                if not shrunk_invalid:
                    liquidation = liquidate_inventory_actions(
                        constants, level, shrunk_result["final_inventory"], shrunk_result["final_location"],
                    )
                    candidate = shrunk + liquidation
                    candidate_result, candidate_invalid = replay(constants, level, level_number, candidate)
                    if not candidate_invalid and candidate_result["final_tick"] <= total_ticks:
                        return candidate, candidate_result

    return best_combined, final_result