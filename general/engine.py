"""
Age of Enteland - simulation engine.

Implements the rules from problem_statement/hackathon.md exactly:
  - single global tick clock, cutoff at total_ticks (Assumption 1)
  - sequential, execute-in-order actions (Assumption 2)
  - validate-at-execution-time (Assumption 3)
  - invalid actions are skipped, cost 1 tick, logged, run continues (Assumption 4)
  - passive town trickle (production + enteloot) fires on the clock regardless
    of player location (Assumption 5), auto-credited to the player (Assumption 6)
  - deterministic, no debt, unlimited inventory, fixed prices (Assumptions 7,9,10,11)
  - atomic travel: full edge weight consumed, arrive at the end (Assumption 12)

This engine is written to be level-agnostic: which action types are legal is
governed by `unlocked_actions`, built from constants["level_unlocks"] up to
the given level number. For Level 1 that's exactly:
    travel, buy, sell, gather
craft / build / upkeep raise "not unlocked" -> treated as invalid actions,
so the engine can already safely run a Level-1 action list even though those
handlers are stubbed for later levels.
"""

import json
import math
from collections import defaultdict


def load_json(path):
    with open(path) as f:
        return json.load(f)


class InvalidAction(Exception):
    """Raised internally when an action's prerequisites aren't met."""
    pass


class Engine:
    def __init__(self, constants, level, level_number=1):
        self.C = constants
        self.level = level
        self.level_number = level_number

        run = level["run"]
        self.total_ticks = run["total_ticks"]
        self.tick = 0
        self.enteloot = run["starting_enteloot"]
        self.location = run["starting_town"]
        self.inventory = defaultdict(int)

        self.towns = level["towns"]
        self.nodes = level.get("nodes", {})
        self.routes = level.get("routes", [])

        # per-town set of built upgrade names (starts from level file's
        # pre-built list, if any; grows as we execute `build` actions)
        self.built = {name: set(t.get("upgrades", [])) for name, t in self.towns.items()}
        self.total_score_value = 0  # running tally of built upgrades' score_value

        # unlocked action types for this level
        self.unlocked = set()
        for lvl in range(1, level_number + 1):
            self.unlocked.update(constants["level_unlocks"].get(str(lvl), []))

        # build adjacency: (a,b) -> list of edge dicts {weight, toll, fast}
        self.adj = defaultdict(list)
        for r in self.routes:
            a, b = r["between"]
            fast = r.get("toll", 0) > 0
            edge = {"weight": r["weight"], "toll": r.get("toll", 0), "fast": fast}
            self.adj[a].append((b, edge))
            self.adj[b].append((a, edge))

        # trickle bookkeeping: number of production/enteloot CYCLES already
        # credited per town (not totals - see _sync_trickle for why this
        # matters once amounts can change mid-run via upgrades/upkeep).
        self._credited_cycles_enteloot = defaultdict(int)
        self._credited_cycles_resource = defaultdict(lambda: defaultdict(int))

        # tools owned (permanent effects), Level 3+
        self.owned_tools = set()
        # per-town upkeep boost expiry tick (0 => not active), Level 4
        self.upkeep_boost_end = defaultdict(int)

        self.log = []
        self.stopped = False  # True once total_ticks cutoff reached

        # credit trickle for tick 0 (no-op, but keeps things consistent)
        self._sync_trickle()

    # ---------------------------------------------------------- utilities --
    def _town_production_amount(self, town_name, resource):
        """Current per-cycle amount for a resource at a town, after any
        production-upgrade doubling (floored)."""
        town = self.towns[town_name]
        base = town["production"]["resources"].get(resource, 0)
        for up in self.built.get(town_name, set()):
            up_def = self.C["upgrades"]["production"].get(up)
            if up_def and up_def["boosts"] == resource:
                base = math.floor(base * 2)
        return base

    def _town_enteloot_amount(self, town_name):
        town = self.towns[town_name]
        amount = town["enteloot"]["amount"]
        pct = 0.0
        for up in self.built.get(town_name, set()):
            up_def = self.C["upgrades"]["civic"].get(up)
            if up_def and up_def["effect"]["type"] == "enteloot_amount_pct":
                pct += up_def["effect"]["value"]
        amount = amount * (1 + pct)
        # Level 4 upkeep boost: doubles current Enteloot production while
        # active. Applied on top of the combined percentage bonus, per the
        # spec ("those effects are applied in addition to the combined
        # percentage bonus").
        if self.tick < self.upkeep_boost_end.get(town_name, 0):
            amount = amount * self.C["constants"]["upkeep_boost_multiplier"]
        return math.floor(amount)

    def _upkeep_boost_duration(self, town_name):
        base = self.C["constants"]["upkeep_boost_duration_ticks"]
        pct = 0.0
        for up in self.built.get(town_name, set()):
            up_def = self.C["upgrades"]["civic"].get(up)
            if up_def and up_def["effect"]["type"] == "upkeep_boost_duration_pct":
                pct += up_def["effect"]["value"]
        return math.floor(base * (1 + pct))

    def _town_enteloot_rate(self, town_name):
        town = self.towns[town_name]
        rate = town["enteloot"]["rate"]
        for up in self.built.get(town_name, set()):
            up_def = self.C["upgrades"]["civic"].get(up)
            if up_def and up_def["effect"]["type"] == "enteloot_rate_delta":
                rate = max(up_def["effect"]["min"], rate + up_def["effect"]["value"])
        return rate

    def _sync_trickle(self):
        """Recompute accumulated trickle for every town up to self.tick and
        credit the delta to the player (Assumptions 5 & 6).

        IMPORTANT: this credits NEWLY completed cycles at the amount that
        currently applies, rather than recomputing "cycles * current_amount"
        from scratch. The latter would retroactively apply the current
        (possibly upgrade- or upkeep-boosted) amount to cycles that already
        completed under a different amount, over- or under-crediting the
        player the moment an upgrade is built or an upkeep boost starts or
        expires. Tracking credited CYCLE COUNTS (not credited totals) and
        multiplying only the newly-completed cycles by the amount active
        right now keeps each cycle's credit tied to the amount that was in
        effect when it completed (to within the resolution of one sync).
        """
        for name, town in self.towns.items():
            # resources
            rate = town["production"]["rate"]
            cycles = self.tick // rate if rate > 0 else 0
            for res in town["production"]["resources"]:
                prev_cycles = self._credited_cycles_resource[name][res]
                new_cycles = cycles - prev_cycles
                if new_cycles > 0:
                    amt = self._town_production_amount(name, res)
                    self.inventory[res] += new_cycles * amt
                    self._credited_cycles_resource[name][res] = cycles
            # enteloot
            erate = self._town_enteloot_rate(name)
            ecycles = self.tick // erate if erate > 0 else 0
            prev_ecycles = self._credited_cycles_enteloot[name]
            new_ecycles = ecycles - prev_ecycles
            if new_ecycles > 0:
                eamt = self._town_enteloot_amount(name)
                self.enteloot += new_ecycles * eamt
                self._credited_cycles_enteloot[name] = ecycles

    def _advance(self, ticks):
        """Advance the clock, respecting the total_ticks cutoff. Returns True
        if the action may proceed (had enough room), False if it was cut off."""
        if self.tick + ticks > self.total_ticks:
            self.tick = self.total_ticks
            self._sync_trickle()
            self.stopped = True
            return False
        self.tick += ticks
        self._sync_trickle()
        return True

    def _log(self, action, ok, detail, ticks=0):
        self.log.append({
            "tick": self.tick,
            "action": action,
            "ok": ok,
            "detail": detail,
            "ticks": ticks,
            "enteloot": self.enteloot,
        })

    def _item_def(self, item):
        """Look up an item as a sellable recipe, a construction component,
        or a tool. Returns (def_dict, kind) with kind in
        {"recipe", "component", "tool"}, or (None, None)."""
        if item in self.C["recipes"]:
            return self.C["recipes"][item], "recipe"
        if item in self.C["components"]:
            return self.C["components"][item], "component"
        if item in self.C.get("tools", {}):
            return self.C["tools"][item], "tool"
        return None, None

    def _craft_time_here(self):
        town = self.towns.get(self.location)
        if town and "crafting" in town.get("affinities", []):
            return self.C["constants"]["craft_time_affinity"]
        return self.C["constants"]["craft_time_base"]

    def _do_craft(self, a):
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, int) or qty <= 0:
            raise InvalidAction("malformed craft")
        item_def, kind = self._item_def(item)
        if item_def is None:
            raise InvalidAction("unknown craftable item")
        min_level = item_def.get("min_level")
        if min_level and self.level_number < min_level:
            raise InvalidAction("item not unlocked at this level")
        if kind == "tool":
            if qty != 1:
                raise InvalidAction("tools can only be crafted one at a time")
            if item in self.owned_tools:
                raise InvalidAction("tool already crafted this run (once_per_run)")
        for inp, amt in item_def["inputs"].items():
            if self.inventory.get(inp, 0) < amt * qty:
                raise InvalidAction(f"insufficient {inp} for craft")
        per_item = self._craft_time_here()
        cost = per_item * qty
        if not self._advance(cost):
            return ("cutoff", cost)
        for inp, amt in item_def["inputs"].items():
            self.inventory[inp] -= amt * qty
        if kind == "tool":
            self.owned_tools.add(item)
        else:
            self.inventory[item] += qty
        return (f"crafted {qty} {item}", cost)

    def _check_prerequisite(self, prereq, town_name):
        if prereq is None:
            return True
        built_here = self.built.get(town_name, set())
        if prereq["type"] == "any_production_upgrades":
            count = sum(1 for u in built_here if u in self.C["upgrades"]["production"])
            return count >= prereq["count"]
        if prereq["type"] == "specific_upgrade":
            return prereq["upgrade"] in built_here
        return False

    def _do_build(self, a):
        upgrade = a.get("upgrade")
        if not isinstance(upgrade, str):
            raise InvalidAction("malformed build")
        if upgrade in self.C["upgrades"]["production"]:
            up_def = self.C["upgrades"]["production"][upgrade]
        elif upgrade in self.C["upgrades"]["civic"]:
            up_def = self.C["upgrades"]["civic"][upgrade]
        else:
            raise InvalidAction("unknown upgrade")
        min_level = up_def.get("min_level")
        if min_level and self.level_number < min_level:
            raise InvalidAction("upgrade not unlocked at this level")
        town = self.towns.get(self.location)
        if town is None:
            raise InvalidAction("not at a town")
        if upgrade in self.built.get(self.location, set()):
            raise InvalidAction("already built here")
        if not self._check_prerequisite(up_def.get("prerequisite"), self.location):
            raise InvalidAction("prerequisite not met")
        for comp, amt in up_def["components"].items():
            if self.inventory.get(comp, 0) < amt:
                raise InvalidAction(f"insufficient {comp} to build")
        if up_def["enteloot_cost"] > self.enteloot:
            raise InvalidAction("cannot afford build")
        cost = up_def["build_time"]
        if not self._advance(cost):
            return ("cutoff", cost)
        for comp, amt in up_def["components"].items():
            self.inventory[comp] -= amt
        self.enteloot -= up_def["enteloot_cost"]
        self.built.setdefault(self.location, set()).add(upgrade)
        self.total_score_value += up_def["score_value"]
        return (f"built {upgrade} at {self.location}", cost)

    # ------------------------------------------------------------ actions --
    def _do_travel(self, a):
        dest = a.get("destination")
        fast = a.get("fast", False)
        if not isinstance(dest, str) or not isinstance(fast, bool):
            raise InvalidAction("malformed travel")
        edges = self.adj.get(self.location, [])
        candidates = [e for (d, e) in edges if d == dest and e["fast"] == fast]
        if not candidates:
            raise InvalidAction(f"no {'fast' if fast else 'standard'} route "
                                 f"{self.location}->{dest}")
        edge = min(candidates, key=lambda e: e["weight"])
        if fast and edge["toll"] > self.enteloot:
            raise InvalidAction("cannot afford toll")
        cost = edge["weight"]
        if "boots" in self.owned_tools:
            delta = self.C["tools"]["boots"]["effect"]["value"]
            floor = self.C["tools"]["boots"]["effect"]["min"]
            cost = max(floor, cost + delta)
        if not self._advance(cost):
            return ("cutoff", cost)
        self.enteloot -= edge["toll"]
        self.location = dest
        return (f"traveled to {dest} (fast={fast})", cost)

    def _do_gather(self, a):
        node = self.nodes.get(self.location)
        if node is None:
            raise InvalidAction("not at a node")
        gtime = node["gather-time"]
        if "pickaxe" in self.owned_tools:
            delta = self.C["tools"]["pickaxe"]["effect"]["value"]
            floor = self.C["tools"]["pickaxe"]["effect"]["min"]
            gtime = max(floor, gtime + delta)
        if not self._advance(gtime):
            return ("cutoff", gtime)
        self.inventory[node["resource"]] += node["yield"]
        return (f"gathered {node['yield']} {node['resource']}", gtime)

    def _do_upkeep(self, a):
        town = self.towns.get(self.location)
        if town is None:
            raise InvalidAction("not at a town")
        cost = self.C["constants"]["upkeep_action_ticks"]
        if not self._advance(cost):
            return ("cutoff", cost)
        duration = self._upkeep_boost_duration(self.location)
        # "Triggering it again while already active refreshes the duration
        # rather than stacking the multiplier" - simple overwrite achieves
        # this since the multiplier itself is not tracked as a stack.
        self.upkeep_boost_end[self.location] = self.tick + duration
        return (f"upkeep boost at {self.location} until tick {self.tick + duration}", cost)

    def _do_buy(self, a):
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, int) or qty <= 0:
            raise InvalidAction("malformed buy")
        town = self.towns.get(self.location)
        if town is None or item not in town["production"]["resources"]:
            raise InvalidAction("town does not sell this resource")
        price = self.C["resources"].get(item, {}).get("buy_price")
        if price is None:
            raise InvalidAction("resource not buyable")
        cost_enteloot = price * qty
        if cost_enteloot > self.enteloot:
            raise InvalidAction("cannot afford purchase")
        if not self._advance(1):
            return ("cutoff", 1)
        self.enteloot -= cost_enteloot
        self.inventory[item] += qty
        return (f"bought {qty} {item} for {cost_enteloot}", 1)

    def _do_sell(self, a):
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, int) or qty <= 0:
            raise InvalidAction("malformed sell")
        if self.inventory.get(item, 0) < qty:
            raise InvalidAction("insufficient inventory")
        if item in self.C["resources"]:
            price = self.C["resources"][item]["sell_price"]
        elif item in self.C["recipes"]:
            town = self.towns.get(self.location)
            price = town["item-rates"].get(item) if town else None
            if price is None:
                raise InvalidAction("no item-rate for this good here")
        else:
            raise InvalidAction("unknown sellable item")
        if not self._advance(1):
            return ("cutoff", 1)
        self.inventory[item] -= qty
        gain = price * qty
        self.enteloot += gain
        return (f"sold {qty} {item} for {gain}", 1)

    # -------------------------------------------------------------- driver --
    def run(self, actions):
        for a in actions:
            if self.stopped:
                break
            atype = a.get("type") if isinstance(a, dict) else None
            try:
                if atype not in self.unlocked:
                    raise InvalidAction(f"'{atype}' not unlocked at this level")
                handler = {
                    "travel": self._do_travel,
                    "gather": self._do_gather,
                    "buy": self._do_buy,
                    "sell": self._do_sell,
                    "craft": self._do_craft,
                    "build": self._do_build,
                    "upkeep": self._do_upkeep,
                }.get(atype)
                if handler is None:
                    raise InvalidAction(f"unrecognized type '{atype}'")
                detail, ticks = handler(a)
                if detail == "cutoff":
                    self._log(a, False, "cutoff: skipped, run ends", ticks)
                else:
                    self._log(a, True, detail, ticks)
            except InvalidAction as e:
                if self.stopped:
                    break
                self._advance(self.C["constants"]["invalid_action_ticks"])
                self._log(a, False, f"invalid: {e}",
                           self.C["constants"]["invalid_action_ticks"])
        return self.summary()

    def summary(self):
        return {
            "final_tick": self.tick,
            "final_location": self.location,
            "final_enteloot": self.enteloot,
            "final_inventory": dict(self.inventory),
            "built": {t: sorted(list(u)) for t, u in self.built.items() if u},
            "total_infrastructure_score_value": self.total_score_value,
            "log": self.log,
        }


if __name__ == "__main__":
    constants = load_json("resources.json")
    level = load_json("example_level1.json")
    eng = Engine(constants, level, level_number=1)
    actions = [
        {"type": "travel", "destination": "N1"},
        {"type": "gather"},
        {"type": "gather"},
        {"type": "travel", "destination": "Demacia"},
        {"type": "sell", "item": "wheat", "quantity": 12},
    ]
    result = eng.run(actions)
    print(json.dumps(result, indent=2))