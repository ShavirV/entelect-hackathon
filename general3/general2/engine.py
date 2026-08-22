"""
Age of Enteland — simulation engine.

This is a from-scratch reimplementation of the rules described in the
specification PDF. It is the source of truth used by the solver to test
candidate action sequences, and can be used standalone to validate/score
any actions.json file before you submit it.

=====================================================================
IMPORTANT DOCUMENTED ASSUMPTION (please read)
=====================================================================
The spec is ambiguous about WHO receives a town's passive trickle
(production + Enteloot). Two readings are possible:

  (A) GLOBAL: every town's trickle accrues to the player automatically,
      regardless of the player's location (this is how Assumption 5
      "Passive Systems Fire on the Clock ... regardless of where the
      player is" and Assumption 6 "Auto storage of generated resources
      ... automatically assigned and accessible to the player" read
      literally), or

  (B) LOCAL: a town's trickle only becomes collectible when the player
      is physically standing in that town (more conventional for a
      city-builder, but then Assumption 5's "regardless of where the
      player is" phrase would be pointless).

This engine implements (A) GLOBAL by default because it matches the
literal wording of Assumptions 5+6 and the worked-example phrasing
("the player has accumulated 8 wheat and 8 sheep"), and because the
goal text frames the player as managing the *whole region's* economy.
It is exposed as a single flag (`GLOBAL_PASSIVE_INCOME`) so you can
flip it and re-run everything if real judging results show otherwise.
This is exactly the kind of thing to check once you get real feedback.
=====================================================================
"""
from __future__ import annotations
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# Load constants from JSON
with open('constants.json') as f:
    _CONSTANTS_DATA = json.load(f)

_CONSTANTS = _CONSTANTS_DATA['constants']
RESOURCES = _CONSTANTS_DATA['resources']
RECIPES = _CONSTANTS_DATA['recipes']
COMPONENTS = _CONSTANTS_DATA['components']
UPGRADES = _CONSTANTS_DATA['upgrades']
TOOLS = _CONSTANTS_DATA['tools']
LEVEL_UNLOCKS = _CONSTANTS_DATA['level_unlocks']

# Derived constants
PRODUCTION_UPGRADES = UPGRADES.get('production', {})
CIVIC_UPGRADES = UPGRADES.get('civic', {})
ALL_UPGRADES = {**PRODUCTION_UPGRADES, **CIVIC_UPGRADES}
CRAFTABLES = {**RECIPES, **COMPONENTS}

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

def features_for_level(level):
    """Get features available at a given level"""
    level = str(level)
    features = {
        'travel': False,
        'buy': False,
        'sell': False,
        'gather': False,
        'crafting': False,
        'building': False,
        'recipes': False,
        'components': False,
        'production_upgrades': False,
        'civic_upgrades': False,
        'fast_routes': False,
        'mines': False,
        'ore': False,
        'iron_fittings': False,
        'tools': False,
        'police_station': False,
        'upkeep': False,
    }
    
    if level in LEVEL_UNLOCKS:
        for feature in LEVEL_UNLOCKS[level]:
            if feature == 'fast_routes':
                features['fast_routes'] = True
            elif feature == 'mine_nodes':
                features['mines'] = True
            elif feature == 'ore':
                features['ore'] = True
            elif feature == 'iron-fittings':
                features['iron_fittings'] = True
            elif feature == 'tools':
                features['tools'] = True
            elif feature == 'police-station':
                features['police_station'] = True
            elif feature == 'upkeep':
                features['upkeep'] = True
            elif feature == 'craft':
                features['crafting'] = True
            elif feature == 'build':
                features['building'] = True
            elif feature == 'recipes':
                features['recipes'] = True
            elif feature == 'construction_components':
                features['components'] = True
            elif feature == 'production_upgrades':
                features['production_upgrades'] = True
            elif feature == 'civic_upgrades':
                features['civic_upgrades'] = True
    
    # Level 1 always has basic features
    if level == '1':
        features['travel'] = True
        features['buy'] = True
        features['sell'] = True
        features['gather'] = True
    # Level 2+ has all level 1 features plus more
    elif int(level) >= 2:
        features['travel'] = True
        features['buy'] = True
        features['sell'] = True
        features['gather'] = True
    
    return features

GLOBAL_PASSIVE_INCOME = True  # see docstring above


@dataclass
class TownState:
    name: str
    production_rate: int
    production_resources: dict            # resource -> base amount per cycle
    affinities: list
    item_rates: dict
    enteloot_rate: int
    enteloot_amount: int                   # base amount per cycle
    upgrades: set = field(default_factory=set)
    # incremental trickle counters: leftover ticks-in-cycle per key
    leftover: dict = field(default_factory=lambda: defaultdict(int))
    upkeep_until: int = 0                  # tick at which upkeep boost expires


class Simulator:
    def __init__(self, level_data: dict, level_number: int):
        self.level_number = level_number
        self.features = features_for_level(level_number)

        run = level_data["run"]
        self.total_ticks = run["total_ticks"]
        self.start_town = run["starting_town"]
        self.tick = 0
        self.location = self.start_town
        self.enteloot = run["starting_enteloot"]
        self.inventory = defaultdict(int)
        self.tools = set()
        self.log = []
        self.cutoff_hit = False

        self.towns: dict[str, TownState] = {}
        for name, t in level_data["towns"].items():
            self.towns[name] = TownState(
                name=name,
                production_rate=t["production"]["rate"],
                production_resources=dict(t["production"]["resources"]),
                affinities=list(t.get("affinities", [])),
                item_rates=dict(t.get("item-rates", {})),
                enteloot_rate=t["enteloot"]["rate"],
                enteloot_amount=t["enteloot"]["amount"],
                upgrades=set(t.get("upgrades", [])),
            )

        self.nodes = level_data["nodes"]
        self.routes_raw = level_data["routes"]

        # adjacency: vertex -> list of dict(to, weight, toll, fast)
        self.adj = defaultdict(list)
        for r in self.routes_raw:
            a, b = r["between"]
            w = r["weight"]
            toll = r.get("toll", 0)
            fast = toll > 0
            self.adj[a].append({"to": b, "weight": w, "toll": toll, "fast": fast})
            self.adj[b].append({"to": a, "weight": w, "toll": toll, "fast": fast})

        self.last_settled_tick = 0

    # ------------------------------------------------------------------
    # Passive trickle
    # ------------------------------------------------------------------
    def _civic_amount_pct(self, town: TownState) -> int:
        pct = 0
        for up in town.upgrades:
            if up in CIVIC_UPGRADES and CIVIC_UPGRADES[up]["effect"].get("type") == "enteloot_amount_pct":
                pct += CIVIC_UPGRADES[up]["effect"].get("value", 0)
        return pct

    def _enteloot_rate(self, town: TownState) -> int:
        rate = town.enteloot_rate
        if "police-station" in town.upgrades:
            rate = max(1, rate - CIVIC_UPGRADES["police-station"]["effect"].get("value", 0))
        return max(1, rate)

    def _enteloot_amount(self, town: TownState) -> int:
        base = town.enteloot_amount
        pct = self._civic_amount_pct(town)
        mult = (1 + pct / 100.0)
        if self.tick < town.upkeep_until:
            mult *= 2
        return int(math.floor(base * mult))

    def _resource_amount(self, town: TownState, resource: str) -> int:
        base = town.production_resources.get(resource, 0)
        boosted = base
        for up, info in PRODUCTION_UPGRADES.items():
            if info.get("boosts") == resource and up in town.upgrades:
                boosted = base * 2
        return int(math.floor(boosted))

    def _settle(self, new_tick: int):
        """Advance all towns' passive trickle from self.last_settled_tick to new_tick."""
        if not GLOBAL_PASSIVE_INCOME:
            # LOCAL variant: only settle the town the player is currently AT.
            towns_to_settle = [self.towns[self.location]] if self.location in self.towns else []
        else:
            towns_to_settle = list(self.towns.values())

        delta = new_tick - self.last_settled_tick
        if delta > 0:
            for town in towns_to_settle:
                # resources
                for resource in town.production_resources:
                    key = f"res:{resource}"
                    town.leftover[key] += delta
                    rate = max(1, town.production_rate)
                    cycles = town.leftover[key] // rate
                    town.leftover[key] %= rate
                    if cycles:
                        amt = self._resource_amount(town, resource)
                        self.inventory[resource] += cycles * amt
                # enteloot
                key = "enteloot"
                town.leftover[key] += delta
                rate = self._enteloot_rate(town)
                cycles = town.leftover[key] // rate
                town.leftover[key] %= rate
                if cycles:
                    amt = self._enteloot_amount(town)
                    self.enteloot += cycles * amt
        self.last_settled_tick = new_tick

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _log(self, entry: dict):
        entry["tick_after"] = self.tick
        entry["enteloot_after"] = self.enteloot
        self.log.append(entry)

    def _would_cutoff(self, cost: int) -> bool:
        return self.tick + cost > self.total_ticks

    def _apply_cutoff(self, action_desc: str):
        self.cutoff_hit = True
        self._settle(self.total_ticks)
        self.tick = self.total_ticks
        self._log({"action": action_desc, "result": "cutoff", "ticks_used": 0})

    def _invalid(self, action_desc: str, reason: str):
        if self._would_cutoff(1):
            self._apply_cutoff(action_desc)
            return
        self._settle(self.tick + 1)
        self.tick += 1
        self._log({"action": action_desc, "result": "invalid", "reason": reason, "ticks_used": 1})

    def _craft_time(self, item: str) -> int:
        town = self.towns.get(self.location)
        base = 2
        if town and "crafting" in town.affinities:
            base = 1
        return base

    def _gather_time(self, base: int) -> int:
        if "pickaxe" in self.tools:
            return max(1, base - 1)
        return base

    def _travel_time(self, base: int) -> int:
        if "boots" in self.tools:
            return max(1, base - 1)
        return base

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def do_travel(self, destination: str, fast: bool = False):
        desc = f"travel -> {destination}" + (" (fast)" if fast else "")
        edges = [e for e in self.adj.get(self.location, []) if e["to"] == destination and e["fast"] == bool(fast)]
        if not edges:
            self._invalid(desc, "no such route")
            return
        edge = min(edges, key=lambda e: e["weight"])
        weight = self._travel_time(edge["weight"])
        toll = edge["toll"]
        if self.enteloot < toll:
            self._invalid(desc, "cannot afford toll")
            return
        if self._would_cutoff(weight):
            self._apply_cutoff(desc)
            return
        self.enteloot -= toll
        self._settle(self.tick + weight)
        self.tick += weight
        self.location = destination
        self._log({"action": desc, "result": "ok", "ticks_used": weight, "toll_paid": toll})

    def do_gather(self):
        desc = "gather"
        node = self.nodes.get(self.location)
        if node is None:
            self._invalid(desc, "not at a resource node")
            return
        if node["type"] == "mine" and not self.features["mines"]:
            self._invalid(desc, "mines not enabled this level")
            return
        gt = self._gather_time(node["gather-time"])
        if self._would_cutoff(gt):
            self._apply_cutoff(desc)
            return
        self._settle(self.tick + gt)
        self.tick += gt
        self.inventory[node["resource"]] += node["yield"]
        self._log({"action": desc, "result": "ok", "ticks_used": gt,
                   "gained": {node["resource"]: node["yield"]}})

    def do_buy(self, item: str, quantity: int):
        desc = f"buy {quantity}x {item}"
        town = self.towns.get(self.location)
        if town is None:
            self._invalid(desc, "not at a town")
            return
        if item not in RESOURCES or buy_price(item) is None:
            self._invalid(desc, "resource not buyable")
            return
        if item not in town.production_resources:
            self._invalid(desc, "town does not produce this resource")
            return
        if not isinstance(quantity, int) or quantity <= 0:
            self._invalid(desc, "bad quantity")
            return
        cost = quantity * buy_price(item)
        if self.enteloot < cost:
            self._invalid(desc, "insufficient enteloot")
            return
        if self._would_cutoff(1):
            self._apply_cutoff(desc)
            return
        self.enteloot -= cost
        self._settle(self.tick + 1)
        self.tick += 1
        self.inventory[item] += quantity
        self._log({"action": desc, "result": "ok", "ticks_used": 1, "spent": cost,
                   "gained": {item: quantity}})

    def do_sell(self, item: str, quantity: int):
        desc = f"sell {quantity}x {item}"
        town = self.towns.get(self.location)
        if town is None:
            self._invalid(desc, "not at a town")
            return
        if not isinstance(quantity, int) or quantity <= 0:
            self._invalid(desc, "bad quantity")
            return
        if self.inventory.get(item, 0) < quantity:
            self._invalid(desc, "insufficient inventory")
            return
        if item in RESOURCES:
            price = sell_price(item)
        elif item in RECIPES:
            price = town.item_rates.get(item)
            if price is None:
                self._invalid(desc, "town has no item-rate for this good")
                return
        else:
            self._invalid(desc, "item not sellable")
            return
        if self._would_cutoff(1):
            self._apply_cutoff(desc)
            return
        revenue = quantity * price
        self._settle(self.tick + 1)
        self.tick += 1
        self.inventory[item] -= quantity
        self.enteloot += revenue
        self._log({"action": desc, "result": "ok", "ticks_used": 1, "earned": revenue})

    def do_craft(self, item: str, quantity: int):
        desc = f"craft {quantity}x {item}"
        if not self.features["crafting"]:
            self._invalid(desc, "crafting not enabled this level")
            return
        if item not in CRAFTABLES:
            self._invalid(desc, "unknown recipe")
            return
        if item == "iron-fittings" and not self.features["mines"]:
            self._invalid(desc, "iron-fittings requires ore (level 3+)")
            return
        if not isinstance(quantity, int) or quantity <= 0:
            self._invalid(desc, "bad quantity")
            return
        recipe = CRAFTABLES[item]
        ct = self._craft_time(item)
        total_ticks = ct * quantity
        needed = {k: v * quantity for k, v in recipe["inputs"].items()}
        for k, v in needed.items():
            if self.inventory.get(k, 0) < v:
                self._invalid(desc, f"insufficient {k}")
                return
        if self._would_cutoff(total_ticks):
            self._apply_cutoff(desc)
            return
        for k, v in needed.items():
            self.inventory[k] -= v
        self._settle(self.tick + total_ticks)
        self.tick += total_ticks
        self.inventory[item] += quantity
        self._log({"action": desc, "result": "ok", "ticks_used": total_ticks,
                   "consumed": needed, "gained": {item: quantity}})

    def do_build(self, upgrade: str):
        desc = f"build {upgrade}"
        if not self.features["building"]:
            self._invalid(desc, "building not enabled this level")
            return
        if upgrade not in ALL_UPGRADES:
            self._invalid(desc, "unknown upgrade")
            return
        if upgrade == "police-station" and not self.features["mines"]:
            self._invalid(desc, "police-station requires iron-fittings (level 3+)")
            return
        town = self.towns.get(self.location)
        if town is None:
            self._invalid(desc, "not at a town")
            return
        if upgrade in town.upgrades:
            self._invalid(desc, "already built in this town")
            return
        info = ALL_UPGRADES[upgrade]
        prereq = info.get("prerequisite")
        if prereq is not None:
            kind = prereq.get("type")
            if kind == "any_production_upgrades":
                val = prereq.get("count", 1)
                built_prod = sum(1 for u in town.upgrades if u in PRODUCTION_UPGRADES)
                if built_prod < val:
                    self._invalid(desc, "prerequisite not met (production upgrades)")
                    return
            elif kind == "specific_upgrade":
                val = prereq.get("upgrade")
                if val not in town.upgrades:
                    self._invalid(desc, f"prerequisite not met (needs {val})")
                    return
        components = info["components"]
        for k, v in components.items():
            if self.inventory.get(k, 0) < v:
                self._invalid(desc, f"insufficient component {k}")
                return
        cost = info["enteloot_cost"]
        if self.enteloot < cost:
            self._invalid(desc, "insufficient enteloot")
            return
        build_time = info["build_time"]
        if self._would_cutoff(build_time):
            self._apply_cutoff(desc)
            return
        for k, v in components.items():
            self.inventory[k] -= v
        self.enteloot -= cost
        self._settle(self.tick + build_time)
        self.tick += build_time
        town.upgrades.add(upgrade)
        self._log({"action": desc, "result": "ok", "ticks_used": build_time,
                   "spent_enteloot": cost, "consumed": components,
                   "score_value": info["score_value"]})

    def do_craft_tool(self, tool: str):
        desc = f"craft-tool {tool}"
        if not self.features["tools"]:
            self._invalid(desc, "tools not enabled this level")
            return
        if tool not in TOOLS:
            self._invalid(desc, "unknown tool")
            return
        if tool in self.tools:
            self._invalid(desc, "tool already crafted")
            return
        info = TOOLS[tool]
        ct = self._craft_time(tool)
        for k, v in info["inputs"].items():
            if self.inventory.get(k, 0) < v:
                self._invalid(desc, f"insufficient {k}")
                return
        if self._would_cutoff(ct):
            self._apply_cutoff(desc)
            return
        for k, v in info["inputs"].items():
            self.inventory[k] -= v
        self._settle(self.tick + ct)
        self.tick += ct
        self.tools.add(tool)
        self._log({"action": desc, "result": "ok", "ticks_used": ct})

    def do_upkeep(self):
        desc = "upkeep"
        if not self.features["upkeep"]:
            self._invalid(desc, "upkeep not enabled this level")
            return
        town = self.towns.get(self.location)
        if town is None:
            self._invalid(desc, "not at a town")
            return
        cost = 5
        if self._would_cutoff(cost):
            self._apply_cutoff(desc)
            return
        self._settle(self.tick + cost)
        self.tick += cost
        duration = 50
        if "fire-station" in town.upgrades:
            duration = int(duration * (1 + CIVIC_UPGRADES["fire-station"]["effect"].get("value", 0) / 100.0))
        town.upkeep_until = self.tick + duration
        self._log({"action": desc, "result": "ok", "ticks_used": cost, "boost_until": town.upkeep_until})

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run_actions(self, actions: list):
        for a in actions:
            if self.tick >= self.total_ticks:
                # still log skip per spec but this is effectively free-wheeling
                self._log({"action": "post-cutoff", "result": "skipped", "ticks_used": 0})
                continue
            if not isinstance(a, dict) or "type" not in a:
                self._invalid(str(a), "malformed action")
                continue
            t = a.get("type")
            try:
                if t == "travel":
                    if "destination" not in a or not isinstance(a["destination"], str):
                        self._invalid("travel", "missing destination")
                        continue
                    self.do_travel(a["destination"], bool(a.get("fast", False)))
                elif t == "gather":
                    self.do_gather()
                elif t == "buy":
                    self.do_buy(a.get("item"), a.get("quantity"))
                elif t == "sell":
                    self.do_sell(a.get("item"), a.get("quantity"))
                elif t == "craft":
                    item = a.get("item")
                    if item in TOOLS:
                        self.do_craft_tool(item)
                    else:
                        self.do_craft(item, a.get("quantity"))
                elif t == "build":
                    self.do_build(a.get("upgrade"))
                elif t == "upkeep":
                    self.do_upkeep()
                else:
                    self._invalid(str(a), "unrecognized type")
            except Exception as e:  # never let a bad action crash the run
                self._invalid(str(a), f"exception: {e}")
        # settle to the end of the run for scoring purposes
        self._settle(self.total_ticks)

    # ------------------------------------------------------------------
    # Scoring proxy (spec gives the *drivers* of score, not exact formula)
    # ------------------------------------------------------------------
    def summarize(self) -> dict:
        invested_enteloot = 0
        infra_score = 0
        towns_with_upgrades = 0
        total_upgrades_built = 0
        for town in self.towns.values():
            if town.upgrades:
                towns_with_upgrades += 1
            for up in town.upgrades:
                total_upgrades_built += 1
                info = ALL_UPGRADES.get(up, {})
                infra_score += info.get("score_value", 0)
                invested_enteloot += info.get("enteloot_cost", 0)

        n_towns = len(self.towns)
        n_possible_upgrades = len(ALL_UPGRADES) * n_towns
        distribution_multiplier = 1.0
        if n_possible_upgrades > 0:
            distribution_multiplier = 1.0 + (total_upgrades_built / n_possible_upgrades)
            # breadth bonus: reward spreading across towns specifically
            if n_towns > 0:
                distribution_multiplier += 0.5 * (towns_with_upgrades / n_towns)

        held_value = 0
        for item, qty in self.inventory.items():
            if qty <= 0:
                continue
            if item in RESOURCES:
                held_value += qty * sell_price(item)
            elif item in RECIPES:
                # approximate using average item-rate across towns
                rates = [t.item_rates.get(item, 0) for t in self.towns.values()]
                avg = sum(rates) / len(rates) if rates else 0
                held_value += qty * avg

        sells = sum(1 for e in self.log if e.get("result") == "ok" and e.get("action", "").startswith("sell"))
        invalid = sum(1 for e in self.log if e.get("result") == "invalid")

        proxy_score_l1 = self.enteloot + held_value  # rough L1 proxy (+ items-sold multiplier, weight unknown)
        proxy_score_l2plus = infra_score * distribution_multiplier + 0.01 * self.enteloot

        return {
            "level": self.level_number,
            "final_tick": self.tick,
            "final_enteloot": self.enteloot,
            "cutoff_hit": self.cutoff_hit,
            "inventory_nonzero": {k: v for k, v in self.inventory.items() if v},
            "held_value_proxy": round(held_value, 2),
            "upgrades_built_total": total_upgrades_built,
            "towns_with_upgrades": towns_with_upgrades,
            "n_towns": n_towns,
            "infra_score_raw": infra_score,
            "distribution_multiplier_proxy": round(distribution_multiplier, 3),
            "invested_enteloot": invested_enteloot,
            "n_actions": len(self.log),
            "n_invalid_actions": invalid,
            "n_sell_actions": sells,
            "proxy_score_level1": round(proxy_score_l1, 2),
            "proxy_score_level2plus": round(proxy_score_l2plus, 2),
        }


def load_level(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def run_actions_file(level_json_path: str, level_number: int, actions_path: str):
    level_data = load_level(level_json_path)
    sim = Simulator(level_data, level_number)
    with open(actions_path) as f:
        actions = json.load(f)["actions"]
    sim.run_actions(actions)
    return sim


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        print("usage: python engine.py <level.json> <level_number> <actions.json>")
        sys.exit(1)
    sim = run_actions_file(sys.argv[1], int(sys.argv[2]), sys.argv[3])
    print(json.dumps(sim.summarize(), indent=2))