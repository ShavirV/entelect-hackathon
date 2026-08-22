import json, math, heapq, sys
from collections import defaultdict
from Simulator import Simulator
import Solver as S


def build_full_adj(routes):
    adj = defaultdict(list)
    for r in routes:
        a, b = r["between"]
        w, toll = r["weight"], r.get("toll", 0)
        adj[a].append((b, w, toll > 0))
        adj[b].append((a, w, toll > 0))
    return adj


def shortest_path(adj, src, dst):
    if src == dst:
        return []
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
        return None
    hops = []
    cur = dst
    while cur != src:
        hops.append(cur)
        cur = prev[cur]
    hops.reverse()
    return hops


class Planner:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.sim = Simulator(level, const, level_number=level_num)
        self.adj = build_full_adj(level["routes"])
        self.actions = []
        self.handlers = {"travel": self.sim._do_travel, "gather": self.sim._do_gather,
                          "buy": self.sim._do_buy, "sell": self.sim._do_sell,
                          "craft": self.sim._do_craft, "build": self.sim._do_build,
                          "upkeep": self.sim._do_upkeep}
        self._path_cache = {}

    def do(self, action):
        sim = self.sim
        if sim.tick >= sim.total_ticks:
            return False
        atype = action["type"]
        if atype not in sim.KNOWN or atype not in sim.unlocked:
            return False
        valid, ticks, fn, detail = self.handlers[atype](action)
        if not valid:
            return False
        before = sim.tick
        if before + ticks > sim.total_ticks:
            return False
        fn()
        sim.tick = before + ticks
        self.actions.append(action)
        return True

    def path(self, src, dst):
        key = (src, dst)
        if key not in self._path_cache:
            self._path_cache[key] = shortest_path(self.adj, src, dst)
        return self._path_cache[key]

    def travel_to(self, dst):
        if self.sim.location == dst:
            return True
        hops = self.path(self.sim.location, dst)
        if hops is None:
            return False
        for h in hops:
            if not self.do({"type": "travel", "destination": h}):
                return False
        return True

    def get_recipe(self, item):
        c = self.const
        if item in c.get("components", {}):
            return c["components"][item]
        if item in c.get("recipes", {}):
            return c["recipes"][item]
        if item in c.get("tools", {}):
            return c["tools"][item]
        return None

    def find_node_for_resource(self, res, near=None):
        near = near or self.sim.location
        best_node, best_d = None, 1 << 30
        for name, node in self.level["nodes"].items():
            if node["resource"] != res:
                continue
            hops = self.path(near, name)
            if hops is None:
                continue
            d = len(hops) and sum(1 for _ in hops) or 0
            # compute real weight distance instead of hop count
            d = self._dist_of(near, name)
            if d is not None and d < best_d:
                best_d, best_node = d, name
        return best_node

    def _dist_of(self, src, dst):
        if src == dst:
            return 0
        dist = {src: 0}
        pq = [(0, src)]
        seen = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == dst:
                return d
            for (v, w, fast) in self.adj.get(u, []):
                if fast:
                    continue
                nd = d + w
                if nd < dist.get(v, 1 << 30):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist.get(dst)

    def ensure_resource(self, res, qty_needed):
        have = self.sim.current_amount(res)
        if have >= qty_needed:
            return True
        deficit = qty_needed - have
        node_type_info = self.const["node_types"]
        # find nearest node producing this raw resource
        node_name = self.find_node_for_resource(res)
        if node_name is None:
            return False
        if node_name != self.sim.location:
            if not self.travel_to(node_name):
                return False
        node = self.level["nodes"][node_name]
        yield_amt = node["yield"]
        n_gathers = -(-deficit // yield_amt)
        for _ in range(n_gathers):
            if not self.do({"type": "gather"}):
                return False
        return True

    def _expand(self, item, qty, totals, order, seen):
        totals[item] = totals.get(item, 0) + qty
        data = self.get_recipe(item)
        if data is None:
            return
        for res, amt in data["inputs"].items():
            need = amt * qty
            if res in self.const["resources"]:
                totals[res] = totals.get(res, 0) + need
            else:
                self._expand(res, need, totals, order, seen)
        if item not in seen:
            seen.add(item)
            order.append(item)

    def craft_item(self, item, qty):
        """Craft `qty` of `item`, recursively crafting/gathering any needed
        sub-components and raw resources first, using total-demand
        accounting so shared intermediates aren't double-consumed."""
        if qty <= 0:
            return True
        totals, order, seen = {}, [], set()
        self._expand(item, qty, totals, order, seen)
        for res, need in totals.items():
            if res in self.const["resources"]:
                have = self.sim.current_amount(res)
                if have < need:
                    if not self.ensure_resource(res, need):
                        return False
        for it in order:
            need = totals[it]
            have = self.sim.current_amount(it)
            if have >= need:
                continue
            if not self.do({"type": "craft", "item": it, "quantity": need - have}):
                return False
        return True

    def craft_bundle(self, components):
        """Craft a dict of {item: qty} top-level requirements (e.g. an
        upgrade's `components`) sharing sub-component demand correctly."""
        totals, order, seen = {}, [], set()
        for name, amt in components.items():
            self._expand(name, amt, totals, order, seen)
        for res, need in totals.items():
            if res in self.const["resources"]:
                have = self.sim.current_amount(res)
                if have < need:
                    if not self.ensure_resource(res, need):
                        return False
        for it in order:
            need = totals[it]
            have = self.sim.current_amount(it)
            if have >= need:
                continue
            if not self.do({"type": "craft", "item": it, "quantity": need - have}):
                return False
        return True

    # ---------------- tool bootstrap (level 3+) ----------------
    def bootstrap_tools(self):
        if self.level_num < 3:
            return
        if "ore" not in [n["resource"] for n in self.level["nodes"].values()]:
            return
        # need: iron-fittings x4 (2 for boots, 2 for pickaxe) -> ore x8, wood x4
        # rope x2 (for boots) -> sheep x4 ; planks x2 (for pickaxe) -> wood x4 (extra)
        ok = self.craft_bundle({"iron-fittings": 4, "rope": 2, "planks": 2})
        if ok:
            self.do({"type": "craft", "item": "boots", "quantity": 1})
            self.do({"type": "craft", "item": "pickaxe", "quantity": 1})

    # ---------------- upgrade opportunity analysis ----------------
    def prod_upgrade_map(self):
        m = {}
        for name, data in self.const["upgrades"]["production"].items():
            m[data["boosts"]] = name
        return m

    def compute_opportunities(self):
        T = self.level["run"]["total_ticks"]
        res_price = {k: v["sell_price"] for k, v in self.const["resources"].items()}
        prod_map = self.prod_upgrade_map()
        opps = defaultdict(lambda: {"prod": [], "civic_value": 0, "civic_cost": 0, "total": 0})
        for town, data in self.level["towns"].items():
            rate = data["production"]["rate"]
            town_total = 0
            for res, base in data["production"]["resources"].items():
                if base <= 0 or res not in prod_map:
                    continue
                upg = prod_map[res]
                updata = self.const["upgrades"]["production"][upg]
                if updata.get("min_level", 1) > self.level_num:
                    continue
                cycles = T // rate
                value = cycles * base * res_price.get(res, 0)
                cost = updata["enteloot_cost"]
                if value > cost:
                    opps[town]["prod"].append((upg, res, value, cost))
                    town_total += (value - cost)
            # civic chain value (rec-center + school + library) -> only meaningful if a prod upgrade exists
            erate, eamt = data["enteloot"]["rate"], data["enteloot"]["amount"]
            cycles_e = T // erate
            pct_total = 0.2 + 0.5 + 0.5
            delta = math.floor(eamt * (1 + pct_total)) - math.floor(eamt)
            civic_value = cycles_e * delta
            civic_cost = 1200 + 2000 + 2500
            if civic_value > civic_cost and opps[town]["prod"]:
                opps[town]["civic_value"] = civic_value
                opps[town]["civic_cost"] = civic_cost
                town_total += (civic_value - civic_cost)
            opps[town]["total"] = town_total
        return opps

    def execute_upgrades(self):
        opps = self.compute_opportunities()
        ordered_towns = sorted(opps.keys(), key=lambda t: -opps[t]["total"])
        for town in ordered_towns:
            info = opps[town]
            if not info["prod"] and not info["civic_value"]:
                continue
            if not self.travel_to(town):
                continue
            for (upg, res, value, cost) in sorted(info["prod"], key=lambda x: -(x[2] - x[3])):
                updata = self.const["upgrades"]["production"][upg]
                if self.sim.current_enteloot() < updata["enteloot_cost"]:
                    continue
                comps = updata.get("components", {})
                if self.craft_bundle(comps) and self.travel_to(town):
                    self.do({"type": "build", "upgrade": upg})
            if info["civic_value"] and self.sim.town_upgrades[town]["production"]:
                for upg in ("rec-center", "school", "library"):
                    updata = self.const["upgrades"]["civic"][upg]
                    if updata.get("min_level", 1) > self.level_num:
                        continue
                    prereq = updata.get("prerequisite")
                    if prereq:
                        if prereq["type"] == "any_production_upgrades":
                            if len(self.sim.town_upgrades[town]["production"]) < prereq["count"]:
                                continue
                        elif prereq["type"] == "specific_upgrade":
                            req = prereq["upgrade"]
                            if req not in self.sim.town_upgrades[town]["production"] and req not in self.sim.town_upgrades[town]["civic"]:
                                continue
                    if self.sim.current_enteloot() < updata["enteloot_cost"]:
                        continue
                    comps = updata.get("components", {})
                    if self.craft_bundle(comps) and self.travel_to(town):
                        self.do({"type": "build", "upgrade": upg})

    # ---------------- grind phase ----------------
    def grind(self, tick_budget_limit):
        lvl_copy = json.loads(json.dumps(self.level))
        lvl_copy["run"]["total_ticks"] = tick_budget_limit
        result = S.generate_actions(lvl_copy, self.const, self.level_num, start_at=self.sim.location)
        if not result:
            return
        actions, best = result
        for a in actions:
            if self.sim.tick >= self.sim.total_ticks:
                break
            self.do(a)


def solve_level(level, const, level_num):
    p = Planner(level, const, level_num)
    T = level["run"]["total_ticks"]

    if level_num == 1:
        # no craft/build available; reuse existing raw-loop grinder for full ticks
        actions, best = S.generate_actions(level, const, level_num)
        for a in actions:
            p.do(a)
        return p.actions

    p.bootstrap_tools()

    if level_num >= 2:
        reserve_tail = max(1500, int(T * 0.05))
    else:
        reserve_tail = 0

    budget_limit = max(p.sim.tick + 1, T - reserve_tail)
    p.grind(budget_limit)

    p.execute_upgrades()

    # spend any remaining ticks grinding again
    remaining = T - p.sim.tick
    if remaining > 50:
        p.grind(T)

    return p.actions


if __name__ == "__main__":
    lvl_num = int(sys.argv[1])
    with open(f"Level{lvl_num}.json") as f:
        level = json.load(f)
    with open("constants.json") as f:
        const = json.load(f)

    actions = solve_level(level, const, lvl_num)
    print(f"Generated {len(actions)} actions")

    sim = Simulator(level, const, level_number=lvl_num)
    log = sim.run(actions)
    invalid = [e for e in log if not e.valid]
    print(f"Invalid actions: {len(invalid)} / {len(log)}")
    for e in invalid[:10]:
        print(" ", e.index, e.action, e.detail)
    print(sim.summary())

    with open(f"output_level{lvl_num}_v2.txt", "w") as f:
        json.dump({"actions": actions}, f)
    print(f"Wrote output_level{lvl_num}_v2.txt")