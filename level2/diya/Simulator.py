"""
Age of Enteland - Simulation Engine (Level 1)

Implements the Level 1 ruleset from the spec:
  - travel, gather, buy, sell actions
  - global tick clock, sequential execution, validation-at-execution
  - invalid actions skipped (1 tick, logged), run never halts
  - passive town trickle (resources + Enteloot) accrues automatically to the
    player regardless of location (Assumption 5 & 6)
  - crafting / building / upkeep are disabled at Level 1 -> invalid actions

All resource prices, node gather-times, feature unlock levels, and scoring
constants (sell bonus multiplier, tick minimums, rounding rule) are loaded
from constants.json - the single global-constants source shared across every
level, per the spec's "Data organisation" note - rather than being
hardcoded here. This keeps the engine reusable for Levels 2-4 without
touching this file's logic when new mechanics are wired up.
"""

import json
import math
from dataclasses import dataclass


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
    # action-type names we know how to execute; everything else in
    # level_unlocks (recipes, fast_routes, mine_nodes, etc.) describes
    # *features* rather than top-level action types and is handled
    # separately (e.g. the "fast" flag on travel).
    KNOWN_ACTION_TYPES = {"travel", "gather", "buy", "sell", "craft", "build", "upkeep"}

    def __init__(self, level_json: dict, constants_json: dict, level_number: int = 1):
        self.level = level_number
        self.const = constants_json["constants"]
        self.resources = constants_json["resources"]
        self.node_types = constants_json["node_types"]
        self.level_unlocks_raw = constants_json["level_unlocks"]

        # cumulative set of unlocked tokens (action types + feature flags)
        # for levels 1..self.level
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

        # adjacency: vertex -> list of (neighbour, weight, toll, is_fast)
        self.adj = {}
        for r in self.routes:
            a, b = r["between"]
            w, toll = r["weight"], r.get("toll", 0)
            is_fast = toll > 0
            self.adj.setdefault(a, []).append((b, w, toll, is_fast))
            self.adj.setdefault(b, []).append((a, w, toll, is_fast))

        # --- mutable run state ---
        self.tick = 0
        self.location = self.start_town
        self.enteloot_txn = 0.0          # net Enteloot from buy/sell/toll
        self.resource_txn = {r: 0 for r in self.resources}  # net from gather/buy/sell
        self.items_sold_count = 0
        self.log = []

    # -----------------------------------------------------------------
    # Passive trickle helpers (Assumption 5 & 6: auto-credited globally)
    # -----------------------------------------------------------------
    def _floor(self, x):
        # rounding rule is a named constant ("floor") so this is the one
        # place that would change if constants.json ever said otherwise.
        assert self.const["rounding"] == "floor"
        return math.floor(x)

    def _trickle_enteloot(self, t):
        total = 0
        for town in self.towns.values():
            rate = town["enteloot"]["rate"]
            amount = town["enteloot"]["amount"]
            cycles = self._floor(t / rate)
            total += cycles * amount
        return total

    def _trickle_resource(self, t, resource):
        total = 0
        for town in self.towns.values():
            prod = town["production"]
            rate = prod["rate"]
            amt = prod["resources"].get(resource, 0)
            if amt:
                cycles = self._floor(t / rate)
                total += cycles * amt
        return total

    def current_enteloot(self, t=None):
        t = self.tick if t is None else t
        return self.starting_enteloot + self._trickle_enteloot(t) + self.enteloot_txn

    def current_inventory(self, t=None):
        t = self.tick if t is None else t
        return {r: self._trickle_resource(t, r) + self.resource_txn[r] for r in self.resources}

    def current_amount(self, resource, t=None):
        t = self.tick if t is None else t
        return self._trickle_resource(t, resource) + self.resource_txn[resource]

    # -----------------------------------------------------------------
    # Action handlers. Each returns (valid, ticks_cost, apply_fn, detail).
    # apply_fn (only present if valid) commits all state changes EXCEPT
    # self.tick, which run() advances centrally after checking the
    # total_ticks budget (Assumption 1).
    # -----------------------------------------------------------------
    def _do_travel(self, a):
        dest = a.get("destination")
        fast = a.get("fast", False)
        if not isinstance(dest, str):
            return False, 1, None, "missing/invalid destination"
        if fast and "fast_routes" not in self.unlocked:
            return False, 1, None, f"fast routes not unlocked at level {self.level}"

        options = self.adj.get(self.location, [])
        match = None
        for (nbr, w, toll, is_fast) in options:
            if nbr == dest and is_fast == bool(fast):
                match = (w, toll)
                break
        if match is None:
            return False, 1, None, f"no {'fast ' if fast else ''}route {self.location}->{dest}"
        weight, toll = match
        weight = max(weight, self.const["min_travel_ticks"])
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
            return False, 1, None, "not at a resource node"
        node_type_info = self.node_types.get(node["type"], {})
        gtime = node.get("gather-time", node_type_info.get("gather_time", 2))
        gtime = max(gtime, self.const["min_gather_ticks"])
        if node["type"] == "mine" and "mine_nodes" not in self.unlocked:
            return False, 1, None, f"mine nodes not unlocked at level {self.level}"
        yield_amt = node["yield"]
        resource = node["resource"]

        def apply():
            self.resource_txn[resource] += yield_amt

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
            self.resource_txn[item] += qty

        return True, 1, apply, f"bought {qty} {item} for {cost}"

    def _do_sell(self, a):
        item = a.get("item")
        qty = a.get("quantity")
        if not isinstance(item, str) or not isinstance(qty, (int, float)) or qty <= 0:
            return False, 1, None, "malformed sell"
        if item not in self.resources:
            # crafted goods disabled at Level 1
            return False, 1, None, f"{item} not sellable at level 1"
        have = self.current_amount(item)
        if have < qty:
            return False, 1, None, f"not enough {item} ({have} < {qty})"
        sell_price = self.resources[item]["sell_price"]
        revenue = sell_price * qty

        def apply():
            self.resource_txn[item] -= qty
            self.enteloot_txn += revenue
            self.items_sold_count += qty

        return True, 1, apply, f"sold {qty} {item} for {revenue}"

    def _do_disabled(self, name):
        return False, 1, None, f"{name} not unlocked at level {self.level}"

    # -----------------------------------------------------------------
    def run(self, actions: list):
        invalid_ticks = self.const["invalid_action_ticks"]

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
            }.get(atype)

            if atype not in self.KNOWN_ACTION_TYPES:
                valid, ticks, apply_fn, detail = False, invalid_ticks, None, f"unrecognized type '{atype}'"
            elif atype not in self.unlocked:
                valid, ticks, apply_fn, detail = self._do_disabled(atype)
                ticks = invalid_ticks
            elif handler is None:
                # craft/build/upkeep are unlocked at a later level but not
                # yet implemented in this Level-1 engine build
                valid, ticks, apply_fn, detail = False, invalid_ticks, None, f"'{atype}' not yet implemented"
            else:
                valid, ticks, apply_fn, detail = handler(a)

            tick_before = self.tick

            if not valid:
                self.tick = min(self.tick + invalid_ticks, self.total_ticks)
                self.log.append(LogEntry(i, a, False, self.tick - tick_before,
                                          self.tick, self.current_enteloot(), detail))
                continue

            if tick_before + ticks > self.total_ticks:
                # Assumption 1: action does not execute; clock jumps to
                # total_ticks and the run ends cleanly.
                self.tick = self.total_ticks
                self.log.append(LogEntry(i, a, False, 0, self.tick,
                                          self.current_enteloot(),
                                          "would exceed total_ticks - skipped, run ended"))
                continue

            apply_fn()
            self.tick = tick_before + ticks
            self.log.append(LogEntry(i, a, True, ticks, self.tick,
                                      self.current_enteloot(), detail))

        self.tick = self.total_ticks
        return self.log

    # -----------------------------------------------------------------
    def summary(self):
        """Returns summary with both score calculation and engine-compatible format."""
        inv = self.current_inventory()
        enteloot = self.current_enteloot()
        held_value = sum(inv[r] * self.resources[r]["sell_price"] for r in self.resources)
        base_score = enteloot + held_value
        multiplier = self.const["sell_bonus_multiplier"] if self.items_sold_count > 0 else 1.0
        score = base_score * multiplier
        
        return {
            # Engine-compatible format
            "final_tick": self.tick,
            "final_location": self.location,
            "final_enteloot": enteloot,
            "final_inventory": inv,
            "log": [
                {
                    "tick": entry.tick_after,
                    "action": entry.action,
                    "ok": entry.valid,
                    "detail": entry.detail,
                    "ticks": entry.ticks_used,
                    "enteloot": entry.enteloot_after,
                }
                for entry in self.log
            ],
            # Additional Simulator-specific scoring fields
            "items_sold_count": self.items_sold_count,
            "held_value": held_value,
            "sell_bonus_multiplier_applied": multiplier,
            "estimated_score": round(score, 2),
        }