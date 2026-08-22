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
    KNOWN = {"travel", "gather", "buy", "sell", "craft", "build", "upkeep"}

    def __init__(self, level_json, constants_json, level_number=1):
        self.level = level_number
        c = constants_json
        self.const = c["constants"]
        self.resources = c["resources"]
        self.node_types = c["node_types"]
        self.recipes = c.get("recipes", {})
        self.components = c.get("components", {})
        self.upgrades = c.get("upgrades", {})
        self.tools = c.get("tools", {})
        self.unlocked = set()
        for lvl, toks in c["level_unlocks"].items():
            if int(lvl) <= self.level:
                self.unlocked.update(toks)

        self.total_ticks = level_json["run"]["total_ticks"]
        self.start_town = level_json["run"]["starting_town"]
        self.starting_enteloot = level_json["run"]["starting_enteloot"]
        self.towns = level_json["towns"]
        self.nodes = level_json.get("nodes", {})
        self.routes = level_json.get("routes", [])

        self.adj = defaultdict(list)
        for r in self.routes:
            a, b = r["between"]
            w, toll = r["weight"], r.get("toll", 0)
            fast = toll > 0
            self.adj[a].append((b, w, toll, fast))
            self.adj[b].append((a, w, toll, fast))

        self.town_upgrades = {t: {"production": set(d.get("upgrades", [])), "civic": set()} for t, d in self.towns.items()}
        self.boost_windows = defaultdict(list)

        self.tick = 0
        self.location = self.start_town
        self.enteloot_txn = 0.0
        self.resource_txn = {r: 0 for r in self.resources}
        self.item_txn = defaultdict(int)
        self.items_sold_count = 0
        self.log = []
        self.tools_crafted = set()

    def _floor(self, x):
        return math.floor(x)

    def _prod_multiplier(self, town, resource):
        m = 1
        for u in self.town_upgrades[town]["production"]:
            eff = self.upgrades.get("production", {}).get(u, {}).get("effect", {})
            if eff.get("type") == "production_double" and eff.get("resource") == resource:
                m *= 2
        return m

    def _civic_pct(self, town):
        pct = 0.0
        for u in self.town_upgrades[town]["civic"]:
            eff = self.upgrades.get("civic", {}).get(u, {}).get("effect", {})
            if eff.get("type") == "enteloot_amount_pct":
                pct += eff["value"]
        return pct

    def _boost_cycles(self, town, t, rate):
        extra = 0
        for (s, e) in self.boost_windows[town]:
            e2 = min(e, t)
            if e2 <= s:
                continue
            extra += self._floor(e2 / rate) - self._floor(s / rate)
        return extra

    def _trickle_enteloot(self, t):
        total = 0
        for town, data in self.towns.items():
            rate, amt = data["enteloot"]["rate"], data["enteloot"]["amount"]
            pct = self._civic_pct(town)
            base_amt = self._floor(amt * (1 + pct))
            cycles = self._floor(t / rate)
            total += cycles * base_amt
            total += self._boost_cycles(town, t, rate) * base_amt
        return total

    def _trickle_resource(self, t, resource):
        total = 0
        for town, data in self.towns.items():
            rate = data["production"]["rate"]
            base = data["production"]["resources"].get(resource, 0)
            if not base:
                continue
            mult = self._prod_multiplier(town, resource)
            total += self._floor(t / rate) * base * mult
        return total

    def current_enteloot(self, t=None):
        t = self.tick if t is None else t
        return self.starting_enteloot + self._trickle_enteloot(t) + self.enteloot_txn

    def current_amount(self, resource, t=None):
        t = self.tick if t is None else t
        if resource in self.resources:
            return self._trickle_resource(t, resource) + self.resource_txn.get(resource, 0)
        return self.item_txn.get(resource, 0)

    def current_inventory(self, t=None):
        t = self.tick if t is None else t
        inv = {r: self.current_amount(r, t) for r in self.resources}
        inv.update(self.item_txn)
        return inv

    def _do_travel(self, a):
        dest = a.get("destination")
        fast = bool(a.get("fast", False))
        if not isinstance(dest, str):
            return False, 1, None, "missing/invalid destination"
        if fast and "fast_routes" not in self.unlocked:
            return False, 1, None, f"fast routes not unlocked at level {self.level}"
        match = None
        for (nbr, w, toll, isf) in self.adj.get(self.location, []):
            if nbr == dest and isf == fast:
                match = (w, toll)
                break
        if match is None:
            return False, 1, None, f"no {'fast ' if fast else ''}route '{self.location}'->'{dest}'"
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
        return True, weight, apply, f"travelled to {dest}"

    def _do_gather(self, a):
        node = self.nodes.get(self.location)
        if node is None:
            return False, 1, None, f"not at a node ('{self.location}')"
        if node["type"] == "mine" and "mine_nodes" not in self.unlocked:
            return False, 1, None, "mine nodes not unlocked"
        gtime = node.get("gather-time", self.node_types.get(node["type"], {}).get("gather_time", 2))
        if "pickaxe" in self.tools_crafted:
            gtime = max(gtime - 1, 1)
        gtime = max(gtime, self.const["min_gather_ticks"])
        yield_amt, resource = node["yield"], node["resource"]

        def apply():
            self.resource_txn[resource] = self.resource_txn.get(resource, 0) + yield_amt
        return True, gtime, apply, f"gathered {yield_amt} {resource}"

    def _do_buy(self, a):
        item, qty = a.get("item"), a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed buy"
        town = self.towns.get(self.location)
        if town is None:
            return False, 1, None, "not at a town"
        if item not in town["production"]["resources"]:
            return False, 1, None, f"{self.location} doesn't sell {item}"
        price = self.resources.get(item, {}).get("buy_price")
        if price is None:
            return False, 1, None, f"{item} cannot be bought"
        cost = price * qty
        if self.current_enteloot() < cost:
            return False, 1, None, "insufficient enteloot"

        def apply():
            self.enteloot_txn -= cost
            self.resource_txn[item] = self.resource_txn.get(item, 0) + qty
        return True, 1, apply, f"bought {qty} {item}"

    def _do_sell(self, a):
        item, qty = a.get("item"), a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed sell"
        have = self.current_amount(item)
        if have < qty:
            return False, 1, None, f"not enough {item} ({have}<{qty})"
        is_recipe = item in self.recipes and self.recipes[item].get("sellable", False)
        town = self.towns.get(self.location)
        if town is None:
            return False, 1, None, "not at a town"
        if is_recipe:
            price = town.get("item-rates", {}).get(item)
            if price is None:
                return False, 1, None, f"{self.location} doesn't buy {item}"
        else:
            price = self.resources.get(item, {}).get("sell_price")
            if price is None:
                return False, 1, None, f"{item} cannot be sold"
        revenue = price * qty

        def apply():
            if item in self.resources:
                self.resource_txn[item] = self.resource_txn.get(item, 0) - qty
            else:
                self.item_txn[item] -= qty
            self.enteloot_txn += revenue
            self.items_sold_count += qty
        return True, 1, apply, f"sold {qty} {item} for {revenue}"

    def _lookup_craftable(self, item):
        if item in self.recipes:
            return self.recipes[item], "recipe"
        if item in self.components:
            return self.components[item], "component"
        if item in self.tools:
            return self.tools[item], "tool"
        return None, None

    def _do_craft(self, a):
        if "craft" not in self.unlocked:
            return False, 1, None, "craft not unlocked"
        item, qty = a.get("item"), a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed craft"
        data, kind = self._lookup_craftable(item)
        if data is None:
            return False, 1, None, f"unknown item {item}"
        if data.get("min_level") and self.level < data["min_level"]:
            return False, 1, None, f"{item} needs level {data['min_level']}"
        if kind == "tool":
            if item in self.tools_crafted:
                return False, 1, None, f"{item} already crafted this run"
            qty = 1
        inputs = data["inputs"]
        for res, amt in inputs.items():
            have = self.current_amount(res)
            if have < amt * qty:
                return False, 1, None, f"not enough {res} ({have}<{amt*qty})"
        craft_time = self.const["craft_time_base"]
        town = self.towns.get(self.location, {})
        if "crafting" in town.get("affinities", []):
            craft_time = self.const["craft_time_affinity"]
        total_time = craft_time * qty

        def apply():
            for res, amt in inputs.items():
                if res in self.resources:
                    self.resource_txn[res] = self.resource_txn.get(res, 0) - amt * qty
                else:
                    self.item_txn[res] -= amt * qty
            if kind == "tool":
                self.tools_crafted.add(item)
            else:
                self.item_txn[item] += qty
        return True, total_time, apply, f"crafted {qty} {item}"

    def _do_build(self, a):
        if "build" not in self.unlocked:
            return False, 1, None, "build not unlocked"
        name = a.get("upgrade")
        if not isinstance(name, str):
            return False, 1, None, "malformed build"
        town_name = self.location
        town = self.towns.get(town_name)
        if town is None:
            return False, 1, None, "not at a town"
        data = self.upgrades.get("production", {}).get(name)
        is_prod = True
        if data is None:
            data = self.upgrades.get("civic", {}).get(name)
            is_prod = False
        if data is None:
            return False, 1, None, f"unknown upgrade {name}"
        if data.get("min_level") and self.level < data["min_level"]:
            return False, 1, None, f"{name} needs level {data['min_level']}"
        uset = self.town_upgrades[town_name]["production" if is_prod else "civic"]
        if name in uset:
            return False, 1, None, f"{name} already built in {town_name}"
        prereq = data.get("prerequisite")
        if prereq:
            if prereq["type"] == "any_production_upgrades":
                if len(self.town_upgrades[town_name]["production"]) < prereq["count"]:
                    return False, 1, None, f"need {prereq['count']} production upgrades"
            elif prereq["type"] == "specific_upgrade":
                req = prereq["upgrade"]
                if req not in self.town_upgrades[town_name]["production"] and req not in self.town_upgrades[town_name]["civic"]:
                    return False, 1, None, f"need {req} first"
        comps = data.get("components", {})
        for cname, amt in comps.items():
            if self.current_amount(cname) < amt:
                return False, 1, None, f"not enough {cname}"
        cost = data.get("enteloot_cost", 0)
        if self.current_enteloot() < cost:
            return False, 1, None, "insufficient enteloot"
        build_time = data.get("build_time", 3)

        def apply():
            for cname, amt in comps.items():
                if cname in self.resources:
                    self.resource_txn[cname] = self.resource_txn.get(cname, 0) - amt
                else:
                    self.item_txn[cname] -= amt
            self.enteloot_txn -= cost
            uset.add(name)
        return True, build_time, apply, f"built {name} in {town_name}"

    def _do_upkeep(self, a):
        if "upkeep" not in self.unlocked:
            return False, 1, None, "upkeep not unlocked"
        town_name = self.location
        if town_name not in self.towns:
            return False, 1, None, "not at a town"
        dur = self.const.get("upkeep_boost_duration_ticks", 50)
        for u in self.town_upgrades[town_name]["civic"]:
            eff = self.upgrades.get("civic", {}).get(u, {}).get("effect", {})
            if eff.get("type") == "upkeep_boost_duration_pct":
                dur = int(dur * (1 + eff["value"]))
        ticks = self.const.get("upkeep_action_ticks", 5)

        def apply():
            start = self.tick + ticks
            self.boost_windows[town_name].append((start, start + dur))
        return True, ticks, apply, f"upkeep boosted {town_name}"

    def run(self, actions):
        inv_ticks = self.const.get("invalid_action_ticks", 1)
        handlers = {"travel": self._do_travel, "gather": self._do_gather, "buy": self._do_buy,
                    "sell": self._do_sell, "craft": self._do_craft, "build": self._do_build,
                    "upkeep": self._do_upkeep}
        for i, a in enumerate(actions):
            if self.tick >= self.total_ticks:
                self.log.append(LogEntry(i, a, False, 0, self.tick, self.current_enteloot(), "run ended"))
                continue
            if not isinstance(a, dict) or "type" not in a:
                self.tick = min(self.tick + inv_ticks, self.total_ticks)
                self.log.append(LogEntry(i, a, False, inv_ticks, self.tick, self.current_enteloot(), "malformed"))
                continue
            atype = a["type"]
            if atype not in self.KNOWN:
                valid, ticks, fn, detail = False, inv_ticks, None, f"unrecognized '{atype}'"
            elif atype not in self.unlocked:
                valid, ticks, fn, detail = False, inv_ticks, None, f"{atype} not unlocked"
            else:
                valid, ticks, fn, detail = handlers[atype](a)
            before = self.tick
            if not valid:
                self.tick = min(self.tick + inv_ticks, self.total_ticks)
                self.log.append(LogEntry(i, a, False, self.tick - before, self.tick, self.current_enteloot(), detail))
                continue
            if before + ticks > self.total_ticks:
                self.tick = self.total_ticks
                self.log.append(LogEntry(i, a, False, 0, self.tick, self.current_enteloot(), "exceeds total_ticks"))
                continue
            fn()
            self.tick = before + ticks
            self.log.append(LogEntry(i, a, True, ticks, self.tick, self.current_enteloot(), detail))
        self.tick = self.total_ticks
        return self.log

    def summary(self):
        inv = self.current_inventory()
        enteloot = self.current_enteloot()
        held = 0
        for r, cnt in inv.items():
            if cnt <= 0:
                continue
            if r in self.recipes and self.recipes[r].get("sellable"):
                price = 0
                for t in self.towns.values():
                    price = max(price, t.get("item-rates", {}).get(r, 0))
                held += cnt * price
            elif r in self.resources:
                held += cnt * self.resources[r].get("sell_price", 0)
        mult = self.const.get("sell_bonus_multiplier", 1.5) if self.items_sold_count > 0 else 1.0
        score = (enteloot + held) * mult
        built = sum(len(v["production"]) + len(v["civic"]) for v in self.town_upgrades.values())
        return {"final_tick": self.tick, "final_enteloot": round(enteloot, 2), "held_value": round(held, 2),
                "items_sold_count": self.items_sold_count, "mult": mult, "upgrades_built": built,
                "estimated_score": round(score, 2)}