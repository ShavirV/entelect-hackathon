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

        # trickle bookkeeping: last-credited totals per town
        self._credited_enteloot = defaultdict(int)
        self._credited_resources = defaultdict(lambda: defaultdict(int))

        self.log = []
        self.stopped = False  # True once total_ticks cutoff reached

        # credit trickle for tick 0 (no-op, but keeps things consistent)
        self._sync_trickle()

    # ---------------------------------------------------------- utilities --
    def _town_production_amount(self, town_name, resource):
        """Current per-cycle amount for a resource at a town, after any
        production-upgrade doubling (floored). Level 1 has no upgrades."""
        town = self.towns[town_name]
        base = town["production"]["resources"].get(resource, 0)
        # production upgrades (level 2+) would double this; not active here.
        for up in town.get("upgrades", []):
            up_def = self.C["upgrades"]["production"].get(up)
            if up_def and up_def["boosts"] == resource:
                base = math.floor(base * 2)
        return base

    def _town_enteloot_amount(self, town_name):
        town = self.towns[town_name]
        amount = town["enteloot"]["amount"]
        # civic upgrades / upkeep would modify this in later levels.
        return math.floor(amount)

    def _sync_trickle(self):
        """Recompute accumulated trickle for every town up to self.tick and
        credit the delta to the player (Assumptions 5 & 6)."""
        for name, town in self.towns.items():
            # resources
            rate = town["production"]["rate"]
            cycles = self.tick // rate if rate > 0 else 0
            for res in town["production"]["resources"]:
                amt = self._town_production_amount(name, res)
                total = cycles * amt
                delta = total - self._credited_resources[name][res]
                if delta > 0:
                    self.inventory[res] += delta
                    self._credited_resources[name][res] = total
            # enteloot
            erate = town["enteloot"]["rate"]
            ecycles = self.tick // erate if erate > 0 else 0
            eamt = self._town_enteloot_amount(name)
            etotal = ecycles * eamt
            edelta = etotal - self._credited_enteloot[name]
            if edelta > 0:
                self.enteloot += edelta
                self._credited_enteloot[name] = etotal

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
        if not self._advance(gtime):
            return ("cutoff", gtime)
        self.inventory[node["resource"]] += node["yield"]
        return (f"gathered {node['yield']} {node['resource']}", gtime)

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
