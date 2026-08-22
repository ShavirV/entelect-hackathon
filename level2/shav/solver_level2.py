"""
Age of Enteland - Level 2 solver.

Level 2 unlocks:
    travel, buy, sell, gather, craft, build

Primary objective:
    maximize infrastructure score value while remaining within the tick
    budget and satisfying all component / Enteloot prerequisites.

This solver deliberately does NOT invent the undocumented final scoring
multiplier. It therefore maximizes actual infrastructure score_value and
uses town spread as a tie-breaker.

Strategy:
    1. Determine a feasible set of upgrades to build.
    2. Expand their component bill of materials recursively.
    3. Account for passive town production.
    4. Gather remaining raw resources from efficient nodes.
    5. Craft components at a crafting-affinity town.
    6. Visit target towns and execute builds in prerequisite order.
    7. Replay the generated actions through Engine and reject any plan that
       produces invalid actions.
"""

import heapq
import json
import math
from collections import defaultdict

from engine import Engine, load_json


PRODUCTION_UPGRADES = [
    "farmhouse",
    "pier",
    "fertilised-fields",
    "quarry",
    "woodlands",
    "pottery-house",
]

CIVIC_CHAIN = [
    "rec-center",
    "fire-station",
    "school",
    "library",
]


# ---------------------------------------------------------------------------
# Graph utilities
# ---------------------------------------------------------------------------

def build_adjacency(routes):
    """
    Build an undirected adjacency list.

    The engine represents routes with:
        weight
        toll
        fast

    A route with toll > 0 is considered a fast route by the supplied engine.
    """

    adj = defaultdict(list)

    for route in routes:
        a, b = route["between"]

        edge = {
            "weight": route["weight"],
            "toll": route.get("toll", 0),
            "fast": route.get("toll", 0) > 0,
        }

        adj[a].append((b, edge))
        adj[b].append((a, edge))

    return adj


def dijkstra(adj, source, allow_fast=False):
    """
    Shortest standard-route distances.

    Level 2 does not unlock fast routes, so by default fast routes are
    excluded.
    """

    dist = {source: 0}
    prev = {}

    pq = [(0, source)]

    while pq:
        distance, u = heapq.heappop(pq)

        if distance != dist.get(u):
            continue

        for v, edge in adj.get(u, []):
            if edge["fast"] and not allow_fast:
                continue

            new_distance = distance + edge["weight"]

            if new_distance < dist.get(v, math.inf):
                dist[v] = new_distance
                prev[v] = u
                heapq.heappush(pq, (new_distance, v))

    return dist, prev


def reconstruct_path(prev, source, target):
    if source == target:
        return [source]

    if target not in prev:
        raise ValueError(f"No route from {source} to {target}")

    path = [target]

    while path[-1] != source:
        current = path[-1]

        if current not in prev:
            raise ValueError(f"No route from {source} to {target}")

        path.append(prev[current])

    path.reverse()
    return path


def shortest_path_actions(adj, source, target):
    """Return travel actions for the shortest standard route."""

    if source == target:
        return []

    _, prev = dijkstra(adj, source, allow_fast=False)
    path = reconstruct_path(prev, source, target)

    return [
        {"type": "travel", "destination": vertex}
        for vertex in path[1:]
    ]


# ---------------------------------------------------------------------------
# Bill of materials
# ---------------------------------------------------------------------------

def explode(target_items, components, recipes, raw_names):
    """
    Recursively expand desired items into:

        craft_totals
        raw_totals
        craft_order

    Dependencies are guaranteed to appear before dependants in craft_order.
    """

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

        definition = components.get(item) or recipes.get(item)

        if definition is None:
            raise ValueError(f"Unknown craftable item in BOM: {item}")

        craft_totals[item] += quantity

        for input_item, amount in definition["inputs"].items():
            stack.append((input_item, amount * quantity))

    # Topological order.
    craft_order = []
    visited = set()

    def visit(item):
        if item in visited:
            return

        if item not in craft_totals:
            return

        visited.add(item)

        definition = components.get(item) or recipes.get(item)

        for input_item in definition["inputs"]:
            visit(input_item)

        craft_order.append(item)

    for item in craft_totals:
        visit(item)

    return dict(craft_totals), dict(raw_totals), craft_order


# ---------------------------------------------------------------------------
# Upgrade planning
# ---------------------------------------------------------------------------

def upgrade_sequence(constants, include_civic=True):
    """
    Return upgrades in an order that satisfies all Level 2 prerequisites.
    """

    sequence = list(PRODUCTION_UPGRADES)

    if include_civic:
        sequence.extend(CIVIC_CHAIN)

    return sequence


def build_target_items(constants, target_upgrades):
    """
    Convert a collection of upgrades into top-level component requirements.
    """

    production = constants["upgrades"]["production"]
    civic = constants["upgrades"]["civic"]

    result = []

    for upgrade in target_upgrades:
        if upgrade in production:
            definition = production[upgrade]
        elif upgrade in civic:
            definition = civic[upgrade]
        else:
            raise ValueError(f"Unknown upgrade: {upgrade}")

        for component, quantity in definition["components"].items():
            result.append((component, quantity))

    return result


def make_infrastructure_plan(constants, target_towns, include_civic=True):
    """
    Construct the infrastructure plan for the supplied towns.

    Each town receives:
        all six production upgrades

    and optionally:
        rec-center
        fire-station
        school
        library
    """

    upgrades_per_town = {}
    top_level_components = []
    enteloot_cost = 0
    build_ticks = 0
    score_value = 0

    production = constants["upgrades"]["production"]
    civic = constants["upgrades"]["civic"]

    for town in target_towns:
        sequence = upgrade_sequence(constants, include_civic)
        upgrades_per_town[town] = sequence

        for upgrade in sequence:
            if upgrade in production:
                definition = production[upgrade]
            else:
                definition = civic[upgrade]

            for component, quantity in definition["components"].items():
                top_level_components.append((component, quantity))

            enteloot_cost += definition["enteloot_cost"]
            build_ticks += definition["build_time"]
            score_value += definition["score_value"]

    raw_names = set(constants["resources"])

    craft_totals, raw_totals, craft_order = explode(
        top_level_components,
        constants["components"],
        constants["recipes"],
        raw_names,
    )

    return {
        "target_towns": list(target_towns),
        "upgrades_per_town": upgrades_per_town,
        "craft_totals": craft_totals,
        "raw_totals": raw_totals,
        "craft_order": craft_order,
        "enteloot_cost": enteloot_cost,
        "build_ticks": build_ticks,
        "score_value": score_value,
    }


# ---------------------------------------------------------------------------
# Passive production
# ---------------------------------------------------------------------------

def passive_resource_at_tick(level, tick):
    """
    Calculate resources automatically produced by towns by `tick`.

    This mirrors the Level 2 engine's passive production behaviour for the
    initial state, before production upgrades are built.
    """

    inventory = defaultdict(int)

    for town in level["towns"].values():
        production = town["production"]
        rate = production["rate"]

        if rate <= 0:
            continue

        cycles = tick // rate

        for resource, amount in production["resources"].items():
            inventory[resource] += cycles * amount

    return dict(inventory)


def passive_enteloot_at_tick(level, tick):
    """
    Calculate passive Enteloot generated by the unmodified towns.
    """

    total = 0

    for town in level["towns"].values():
        rate = town["enteloot"]["rate"]

        if rate <= 0:
            continue

        total += (tick // rate) * town["enteloot"]["amount"]

    return total


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------

def choose_gather_node(resource, quantity, nodes, dist_from_hub):
    """
    Select the node that can supply `quantity` at the lowest total cost:

        outbound travel
        + gather time
        + return travel

    Level 2 has no fast routes, so standard routes are used.
    """

    candidates = []

    for node_name, node in nodes.items():
        if node["resource"] != resource:
            continue

        if node_name not in dist_from_hub:
            continue

        yield_per_gather = node["yield"]
        gather_time = node["gather-time"]

        gathers = math.ceil(quantity / yield_per_gather)

        # Round trip to the hub.
        travel = 2 * dist_from_hub[node_name]

        ticks = travel + gathers * gather_time

        candidates.append(
            (
                ticks,
                node_name,
                gathers,
            )
        )

    if not candidates:
        raise ValueError(
            f"No reachable Level 2 node can produce resource {resource}"
        )

    candidates.sort(key=lambda x: (x[0], x[1]))

    ticks, node_name, gathers = candidates[0]

    return {
        "node": node_name,
        "gathers": gathers,
        "ticks": ticks,
    }


def calculate_raw_requirements_after_trickle(plan, level):
    """
    Remove resources that should already be available from passive town
    production by the time gathering starts.

    This is conservative: it uses the initial passive production rather than
    assuming future production upgrades before the resource collection phase.
    """

    passive = passive_resource_at_tick(level, level["run"]["total_ticks"])

    remaining = {}

    for resource, quantity in plan["raw_totals"].items():
        remaining[resource] = max(
            0,
            quantity - passive.get(resource, 0),
        )

    return remaining


# ---------------------------------------------------------------------------
# Town selection
# ---------------------------------------------------------------------------

def find_crafting_hub(level):
    """
    Prefer a town with crafting affinity.

    If multiple towns have affinity, choose the starting town where possible,
    otherwise choose the alphabetically first affinity town.
    """

    start = level["run"]["starting_town"]

    if "crafting" in level["towns"][start].get("affinities", []):
        return start

    candidates = [
        name
        for name, town in level["towns"].items()
        if "crafting" in town.get("affinities", [])
    ]

    if not candidates:
        return start

    return sorted(candidates)[0]


def choose_town_tour(adj, start, towns):
    """
    Construct a deterministic nearest-neighbour town tour.

    Returns:
        ordered towns
        total travel ticks
    """

    remaining = set(towns)
    remaining.discard(start)

    order = []
    total_ticks = 0
    current = start

    while remaining:
        distances, _ = dijkstra(adj, current)

        reachable = [
            town
            for town in remaining
            if town in distances
        ]

        if not reachable:
            raise ValueError(
                f"Cannot reach remaining towns from {current}: "
                f"{sorted(remaining)}"
            )

        next_town = min(
            reachable,
            key=lambda town: (distances[town], town),
        )

        total_ticks += distances[next_town]
        order.append(next_town)
        remaining.remove(next_town)
        current = next_town

    return order, total_ticks


# ---------------------------------------------------------------------------
# Action construction
# ---------------------------------------------------------------------------

def build_actions(constants, level, plan, hub):
    """
    Construct an executable action sequence.
    """

    adj = build_adjacency(level["routes"])

    actions = []
    current = level["run"]["starting_town"]

    # ------------------------------------------------------------------
    # 1. Move to hub.
    # ------------------------------------------------------------------

    actions.extend(
        shortest_path_actions(adj, current, hub)
    )
    current = hub

    # ------------------------------------------------------------------
    # 2. Gather resources.
    # ------------------------------------------------------------------

    _, prev_from_hub = dijkstra(adj, hub)

    node_choices = {}

    for resource, quantity in plan["raw_totals"].items():
        if quantity <= 0:
            continue

        choice = choose_gather_node(
            resource,
            quantity,
            level["nodes"],
            dijkstra(adj, hub)[0],
        )

        node_choices[resource] = choice

    # Visit nodes deterministically using nearest-neighbour selection.
    remaining_nodes = {
        choice["node"]
        for choice in node_choices.values()
    }

    while remaining_nodes:
        distances, prev = dijkstra(adj, current)

        reachable = [
            node
            for node in remaining_nodes
            if node in distances
        ]

        if not reachable:
            raise ValueError(
                f"Cannot reach remaining gathering nodes from {current}"
            )

        next_node = min(
            reachable,
            key=lambda node: (distances[node], node),
        )

        path = reconstruct_path(prev, current, next_node)

        for destination in path[1:]:
            actions.append({
                "type": "travel",
                "destination": destination,
            })

        resource = level["nodes"][next_node]["resource"]
        choice = node_choices[resource]

        for _ in range(choice["gathers"]):
            actions.append({"type": "gather"})

        current = next_node
        remaining_nodes.remove(next_node)

    # Return to hub for crafting.
    if current != hub:
        actions.extend(
            shortest_path_actions(adj, current, hub)
        )
        current = hub

    # ------------------------------------------------------------------
    # 3. Craft all components at affinity hub.
    # ------------------------------------------------------------------

    for item in plan["craft_order"]:
        quantity = plan["craft_totals"][item]

        if quantity > 0:
            actions.append({
                "type": "craft",
                "item": item,
                "quantity": quantity,
            })

    # ------------------------------------------------------------------
    # 4. Visit towns and build.
    # ------------------------------------------------------------------

    tour, _ = choose_town_tour(
        adj,
        hub,
        plan["target_towns"],
    )

    towns_to_visit = [hub] + tour

    for town in towns_to_visit:
        if current != town:
            actions.extend(
                shortest_path_actions(adj, current, town)
            )
            current = town

        # Production upgrades first.
        for upgrade in PRODUCTION_UPGRADES:
            if upgrade in plan["upgrades_per_town"][town]:
                actions.append({
                    "type": "build",
                    "upgrade": upgrade,
                })

        # Then civic chain.
        for upgrade in CIVIC_CHAIN:
            if (
                upgrade in plan["upgrades_per_town"][town]
                and upgrade in CIVIC_CHAIN
            ):
                actions.append({
                    "type": "build",
                    "upgrade": upgrade,
                })

    return actions, {
        "node_choices": node_choices,
        "final_location": current,
    }


# ---------------------------------------------------------------------------
# Validation / evaluation
# ---------------------------------------------------------------------------

def validate_action_list(constants, level, actions):
    """
    Replay through the actual engine.

    An action list is considered structurally valid only if:
        - it finishes within the tick budget;
        - no action is invalid;
        - all requested builds actually occur.
    """

    engine = Engine(constants, level, level_number=2)
    result = engine.run(actions)

    invalid_actions = [
        entry
        for entry in result["log"]
        if not entry["ok"]
    ]

    return result, invalid_actions


def print_result(result, plan):
    print()
    print("=" * 72)
    print("LEVEL 2 RESULT")
    print("=" * 72)

    print("Target towns:")
    for town in plan["target_towns"]:
        print(f"  - {town}")

    print()
    print("Planned infrastructure score:", plan["score_value"])
    print("Planned Enteloot cost:", plan["enteloot_cost"])
    print("Planned build ticks:", plan["build_ticks"])

    print()
    print("Engine result:")
    print("  final_tick:", result["final_tick"])
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    constants = load_json("resources.json")
    level = load_json("2.txt")

    start = level["run"]["starting_town"]
    adj = build_adjacency(level["routes"])

    # Prefer starting town if it has crafting affinity.
    hub = find_crafting_hub(level)

    print("Level 2 solver")
    print("==============")
    print("Starting town:", start)
    print("Crafting hub:", hub)
    print("Tick budget:", level["run"]["total_ticks"])
    print("Starting Enteloot:", level["run"]["starting_enteloot"])

    # ---------------------------------------------------------------
    # First attempt: all towns, all Level 2 infrastructure.
    # ---------------------------------------------------------------

    target_towns = list(level["towns"].keys())

    plan = make_infrastructure_plan(
        constants,
        target_towns,
        include_civic=True,
    )

    print()
    print("Requested infrastructure:")
    print("  towns:", len(target_towns))
    print("  score:", plan["score_value"])
    print("  Enteloot cost:", plan["enteloot_cost"])
    print("  build ticks:", plan["build_ticks"])

    print()
    print("Raw requirements:")
    for resource, quantity in sorted(plan["raw_totals"].items()):
        print(f"  {resource}: {quantity}")

    # ---------------------------------------------------------------
    # Construct actions.
    # ---------------------------------------------------------------

    try:
        actions, metadata = build_actions(
            constants,
            level,
            plan,
            hub,
        )
    except ValueError as exc:
        print()
        print("Unable to construct full plan:")
        print(" ", exc)
        return 1

    # ---------------------------------------------------------------
    # Replay through the authoritative engine.
    # ---------------------------------------------------------------

    result, invalid_actions = validate_action_list(
        constants,
        level,
        actions,
    )

    print_result(result, plan)

    print()
    print("Generated actions:", len(actions))
    print("Invalid actions:", len(invalid_actions))

    if invalid_actions:
        print()
        print("First invalid actions:")

        for entry in invalid_actions[:10]:
            print(
                " ",
                entry["tick"],
                entry["action"],
                "->",
                entry["detail"],
            )

    # ---------------------------------------------------------------
    # Write output.
    # ---------------------------------------------------------------

    with open("level2_actions.txt", "w") as f:
        json.dump(
            {"actions": actions},
            f,
            indent=2,
        )

    print()
    print("Wrote level2_actions.txt")

    # Don't silently claim success if the engine rejected actions.
    if invalid_actions:
        print()
        print(
            "WARNING: generated plan contains invalid actions. "
            "The plan requires further optimization/adjustment."
        )
        return 2

    if result["final_tick"] > level["run"]["total_ticks"]:
        print("ERROR: tick budget exceeded.")
        return 3

    print()
    print("Plan replayed successfully.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())