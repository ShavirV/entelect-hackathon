"""
Age of Enteland - Simulation Engine (Level 1 & 2 Support)
"""

import json
import math
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class LogEntry:
    index: int
    action: dict
    valid: bool
    ticks_used: int
    tick_after: int
    enteloot_after: float
    detail: str


class Simulator:
    KNOWN_ACTION_TYPES = {"travel", "gather", "buy", "sell", "craft", "build", "upkeep"}

    def __init__(self, level_json: dict, constants_json: dict, level_number: int = 1):
        self.level = level_number
        self.const = constants_json["constants"]
        self.resources = constants_json["resources"]
        self.node_types = constants_json["node_types"]
        self.recipes = constants_json.get("recipes", {})
        self.components = constants_json.get("components", {})
        self.upgrades = constants_json.get("upgrades", {})
        self.level_unlocks_raw = constants_json["level_unlocks"]

        # Unlocked features
        self.unlocked = set()
        for lvl_str, tokens in self.level_unlocks_raw.items():
            if int(lvl_str) <= self.level:
                self.unlocked.update(tokens)

        self.total_ticks = level_json["run"]["total_ticks"]
        self.start_town = level_json["run"]["starting_town"]
        self.starting_enteloot = level_json["run"]["starting_enteloot"]

        self.towns = level_json["towns"]
        self.nodes = level_json.get("nodes", {})
        self.routes = level_json.get("routes", [])

        # Build adjacency - FIXED
        self.adj = defaultdict(list)
        for r in self.routes:
            a, b = r["between"]
            w = r["weight"]
            toll = r.get("toll", 0)
            is_fast = toll > 0
            self.adj[a].append((b, w, toll, is_fast))
            self.adj[b].append((a, w, toll, is_fast))
        
        # DEBUG: Print routes to verify
        print(f"Loaded {len(self.routes)} routes")
        print(f"Adjacency for Demacia: {self.adj.get('Demacia', [])}")

        # Track upgrades built per town
        self.town_upgrades = {}
        for town_name, town_data in self.towns.items():
            self.town_upgrades[town_name] = {
                "production": set(town_data.get("upgrades", [])),
                "civic": set()
            }

        # Mutable run state
        self.tick = 0
        self.location = self.start_town
        self.enteloot_txn = 0.0
        self.resource_txn = {r: 0 for r in self.resources}
        self.items_sold_count = 0
        self.log = []
        self.tools_crafted = set()
        self.active_boosts = {}

    def _floor(self, x):
        assert self.const["rounding"] == "floor"
        return math.floor(x)

    def _get_town_production(self, town_name, tick):
        """Get production for a town with upgrades applied"""
        town = self.towns[town_name]
        rate = town["production"]["rate"]
        resources = town["production"]["resources"].copy()
        
        # Apply production upgrades
        for upgrade in self.town_upgrades[town_name]["production"]:
            upgrade_data = self.upgrades.get("production", {}).get(upgrade)
            if upgrade_data and upgrade_data.get("effect", {}).get("type") == "production_double":
                resource = upgrade_data["effect"]["resource"]
                if resource in resources:
                    resources[resource] = resources[resource] * 2
        
        return rate, resources

    def _trickle_enteloot(self, t):
        total = 0
        for town_name, town_data in self.towns.items():
            rate = town_data["enteloot"]["rate"]
            amount = town_data["enteloot"]["amount"]
            
            # Apply civic upgrades
            civic_multiplier = 1.0
            for upgrade in self.town_upgrades[town_name]["civic"]:
                upgrade_data = self.upgrades.get("civic", {}).get(upgrade)
                if upgrade_data and upgrade_data.get("effect", {}).get("type") == "enteloot_amount_pct":
                    civic_multiplier += upgrade_data["effect"]["value"]
            
            cycles = self._floor(t / rate)
            total += cycles * amount * civic_multiplier
        
        return total

    def _trickle_resource(self, t, resource):
        total = 0
        for town_name in self.towns:
            rate, resources = self._get_town_production(town_name, t)
            amt = resources.get(resource, 0)
            if amt:
                cycles = self._floor(t / rate)
                total += cycles * amt
        return total

    def current_enteloot(self, t=None):
        t = self.tick if t is None else t
        return self.starting_enteloot + self._trickle_enteloot(t) + self.enteloot_txn

    def current_inventory(self, t=None):
        t = self.tick if t is None else t
        return {r: self._trickle_resource(t, r) + self.resource_txn.get(r, 0) for r in self.resources}

    def current_amount(self, resource, t=None):
        t = self.tick if t is None else t
        return self._trickle_resource(t, resource) + self.resource_txn.get(resource, 0)

    # ============================================================
    # ACTION HANDLERS
    # ============================================================

    def _do_travel(self, a):
        dest = a.get("destination")
        fast = a.get("fast", False)
        if not isinstance(dest, str):
            return False, 1, None, "missing/invalid destination"
        
        if dest == self.location:
            return False, 1, None, "already at destination"
        
        if fast and "fast_routes" not in self.unlocked:
            return False, 1, None, f"fast routes not unlocked at level {self.level}"

        options = self.adj.get(self.location, [])
        match = None
        for (nbr, w, toll, is_fast) in options:
            if nbr == dest and is_fast == bool(fast):
                match = (w, toll)
                break
        
        if match is None:
            return False, 1, None, f"no {'fast ' if fast else ''}route from '{self.location}' to '{dest}'"
        
        weight, toll = match
        weight = max(weight, self.const["min_travel_ticks"])
        
        if "boots" in self.tools_crafted:
            weight = max(weight - 1, 1)
        
        if toll > 0 and self.current_enteloot() < toll:
            return False, 1, None, f"cannot afford toll {toll}"

        def apply():
            if toll > 0:
                self.enteloot_txn -= toll
            self.location = dest

        return True, weight, apply, f"travelled to {dest} (toll={toll})"

    def _do_gather(self, a):
        node = self.nodes.get(self.location)
        if node is None:
            return False, 1, None, f"not at a resource node (at '{self.location}')"
        
        node_type_info = self.node_types.get(node["type"], {})
        gtime = node.get("gather-time", node_type_info.get("gather_time", 2))
        
        if "pickaxe" in self.tools_crafted:
            gtime = max(gtime - 1, 1)
        
        gtime = max(gtime, self.const["min_gather_ticks"])
        
        if node["type"] == "mine" and "mine_nodes" not in self.unlocked:
            return False, 1, None, f"mine nodes not unlocked at level {self.level}"
        
        yield_amt = node["yield"]
        resource = node["resource"]

        def apply():
            self.resource_txn[resource] = self.resource_txn.get(resource, 0) + yield_amt

        return True, gtime, apply, f"gathered {yield_amt} {resource}"

    def _do_buy(self, a):
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed buy"
        
        town = self.towns.get(self.location)
        if town is None:
            return False, 1, None, "not at a town"
        
        if item not in town["production"]["resources"]:
            return False, 1, None, f"{self.location} does not sell {item}"
        
        buy_price = self.resources[item]["buy_price"]
        if buy_price is None:
            return False, 1, None, f"{item} cannot be bought"
        
        cost = buy_price * qty
        if self.current_enteloot() < cost:
            return False, 1, None, "insufficient enteloot"

        def apply():
            self.enteloot_txn -= cost
            self.resource_txn[item] = self.resource_txn.get(item, 0) + qty

        return True, 1, apply, f"bought {qty} {item} for {cost}"

    def _do_sell(self, a):
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed sell"
        
        have = self.current_amount(item)
        if have < qty:
            return False, 1, None, f"not enough {item} ({have} < {qty})"
        
        # Check if it's a crafted good (recipe)
        is_recipe = item in self.recipes and self.recipes[item].get("sellable", False)
        
        if is_recipe:
            town = self.towns.get(self.location)
            if town is None:
                return False, 1, None, "not at a town"
            item_rates = town.get("item-rates", {})
            sell_price = item_rates.get(item)
            if sell_price is None:
                return False, 1, None, f"{self.location} does not buy {item}"
        else:
            sell_price = self.resources[item]["sell_price"]
            if sell_price is None:
                return False, 1, None, f"{item} cannot be sold"
        
        revenue = sell_price * qty

        def apply():
            self.resource_txn[item] = self.resource_txn.get(item, 0) - qty
            self.enteloot_txn += revenue
            self.items_sold_count += qty

        return True, 1, apply, f"sold {qty} {item} for {revenue}"

    def _do_craft(self, a):
        if "craft" not in self.unlocked:
            return False, 1, None, f"craft not unlocked at level {self.level}"
        
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed craft"
        
        # Check if it's a recipe or component
        recipe_data = self.recipes.get(item)
        component_data = self.components.get(item)
        
        if not recipe_data and not component_data:
            return False, 1, None, f"unknown recipe/component: {item}"
        
        inputs = recipe_data["inputs"] if recipe_data else component_data["inputs"]
        
        # Check if we have enough resources
        for resource, amount in inputs.items():
            have = self.current_amount(resource)
            if have < amount * qty:
                return False, 1, None, f"not enough {resource} ({have} < {amount * qty})"
        
        # Craft time: 2 ticks base, 1 with affinity
        craft_time = self.const["craft_time_base"]
        town = self.towns.get(self.location, {})
        if "crafting" in town.get("affinities", []):
            craft_time = self.const["craft_time_affinity"]
        
        total_time = craft_time * qty

        def apply():
            for resource, amount in inputs.items():
                self.resource_txn[resource] = self.resource_txn.get(resource, 0) - (amount * qty)
            self.resource_txn[item] = self.resource_txn.get(item, 0) + qty

        return True, total_time, apply, f"crafted {qty} {item}"

    def _do_build(self, a):
        if "build" not in self.unlocked:
            return False, 1, None, f"build not unlocked at level {self.level}"
        
        upgrade_name = a.get("upgrade")
        if not isinstance(upgrade_name, str):
            return False, 1, None, "malformed build"
        
        town_name = self.location
        town = self.towns.get(town_name)
        if town is None:
            return False, 1, None, "not at a town"
        
        # Check if it's a production or civic upgrade
        upgrade_data = self.upgrades.get("production", {}).get(upgrade_name)
        is_production = True
        if upgrade_data is None:
            upgrade_data = self.upgrades.get("civic", {}).get(upgrade_name)
            is_production = False
        
        if upgrade_data is None:
            return False, 1, None, f"unknown upgrade: {upgrade_name}"
        
        # Check if already built
        upgrade_set = self.town_upgrades[town_name]["production"] if is_production else self.town_upgrades[town_name]["civic"]
        if upgrade_name in upgrade_set:
            return False, 1, None, f"{upgrade_name} already built in {town_name}"
        
        # Check prerequisites
        prereq = upgrade_data.get("prerequisite")
        if prereq:
            if prereq.get("type") == "any_production_upgrades":
                count = prereq.get("count", 1)
                if len(self.town_upgrades[town_name]["production"]) < count:
                    return False, 1, None, f"need {count} production upgrades in {town_name}"
            elif prereq.get("type") == "specific_upgrade":
                required = prereq.get("upgrade")
                if required not in self.town_upgrades[town_name]["production"] and required not in self.town_upgrades[town_name]["civic"]:
                    return False, 1, None, f"need {required} in {town_name}"
        
        # Check components
        components = upgrade_data.get("components", {})
        for component_name, quantity in components.items():
            have = self.current_amount(component_name)
            if have < quantity:
                return False, 1, None, f"not enough {component_name} ({have} < {quantity})"
        
        # Check Enteloot
        cost = upgrade_data.get("enteloot_cost", 0)
        if self.current_enteloot() < cost:
            return False, 1, None, f"insufficient enteloot ({self.current_enteloot()} < {cost})"
        
        build_time = upgrade_data.get("build_time", 3)

        def apply():
            for component_name, quantity in components.items():
                self.resource_txn[component_name] = self.resource_txn.get(component_name, 0) - quantity
            self.enteloot_txn -= cost
            if is_production:
                self.town_upgrades[town_name]["production"].add(upgrade_name)
            else:
                self.town_upgrades[town_name]["civic"].add(upgrade_name)

        return True, build_time, apply, f"built {upgrade_name} in {town_name}"

    def _do_upkeep(self, a):
        if "upkeep" not in self.unlocked:
            return False, 1, None, f"upkeep not unlocked at level {self.level}"
        
        town_name = self.location
        if town_name not in self.towns:
            return False, 1, None, "not at a town"
        
        duration = self.const.get("upkeep_boost_duration_ticks", 50)
        if "fire-station" in self.town_upgrades[town_name]["civic"]:
            duration = int(duration * 1.5)
        
        def apply():
            self.active_boosts[town_name] = duration

        return True, self.const.get("upkeep_action_ticks", 5), apply, f"upkeep boosted {town_name}"

    # ============================================================
    # RUN
    # ============================================================

    def run(self, actions: list):
        invalid_ticks = self.const.get("invalid_action_ticks", 1)

        for i, a in enumerate(actions):
            if self.tick >= self.total_ticks:
                self.log.append(LogEntry(i, a, False, 0, self.tick,
                                          self.current_enteloot(), "run already ended"))
                continue

            if not isinstance(a, dict) or "type" not in a:
                self.tick = min(self.tick + invalid_ticks, self.total_ticks)
                self.log.append(LogEntry(i, a, False, invalid_ticks, self.tick,
                                          self.current_enteloot(), "malformed action entry"))
                continue

            atype = a["type"]
            
            handler = {
                "travel": self._do_travel,
                "gather": self._do_gather,
                "buy": self._do_buy,
                "sell": self._do_sell,
                "craft": self._do_craft,
                "build": self._do_build,
                "upkeep": self._do_upkeep,
            }.get(atype)

            if atype not in self.KNOWN_ACTION_TYPES:
                valid, ticks, apply_fn, detail = False, invalid_ticks, None, f"unrecognized type '{atype}'"
            elif atype not in self.unlocked:
                valid, ticks, apply_fn, detail = False, invalid_ticks, None, f"{atype} not unlocked at level {self.level}"
            elif handler is None:
                valid, ticks, apply_fn, detail = False, invalid_ticks, None, f"'{atype}' not implemented"
            else:
                valid, ticks, apply_fn, detail = handler(a)

            tick_before = self.tick

            if not valid:
                self.tick = min(self.tick + invalid_ticks, self.total_ticks)
                self.log.append(LogEntry(i, a, False, self.tick - tick_before,
                                          self.tick, self.current_enteloot(), detail))
                continue

            if tick_before + ticks > self.total_ticks:
                self.tick = self.total_ticks
                self.log.append(LogEntry(i, a, False, 0, self.tick,
                                          self.current_enteloot(),
                                          "would exceed total_ticks - skipped"))
                continue

            apply_fn()
            self.tick = tick_before + ticks
            self.log.append(LogEntry(i, a, True, ticks, self.tick,
                                      self.current_enteloot(), detail))

        self.tick = self.total_ticks
        return self.log

    # ============================================================
    # SUMMARY
    # ============================================================

    def summary(self):
        inv = self.current_inventory()
        enteloot = self.current_enteloot()
        
        held_value = 0
        for r, count in inv.items():
            if count > 0:
                # Check if it's a crafted good
                if r in self.recipes and self.recipes[r].get("sellable", False):
                    # Use average or use the starting town's rate
                    town = self.towns.get(self.start_town, {})
                    price = town.get("item-rates", {}).get(r, 0)
                    if price == 0:
                        # Try to find any town that buys it
                        for t_name, t_data in self.towns.items():
                            price = t_data.get("item-rates", {}).get(r, 0)
                            if price > 0:
                                break
                    held_value += count * price
                else:
                    held_value += count * self.resources.get(r, {}).get("sell_price", 0)
        
        base_score = enteloot + held_value
        multiplier = self.const.get("sell_bonus_multiplier", 1.5) if self.items_sold_count > 0 else 1.0
        score = base_score * multiplier
        
        return {
            "final_tick": self.tick,
            "final_enteloot": enteloot,
            "final_inventory": inv,
            "items_sold_count": self.items_sold_count,
            "held_value": held_value,
            "sell_bonus_multiplier_applied": multiplier,
            "estimated_score": round(score, 2),
        }