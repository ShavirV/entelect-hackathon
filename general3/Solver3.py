import json, math, heapq, sys
from collections import defaultdict
from Simulator import Simulator
import Solver as S


def build_full_adj(routes):
    adj = defaultdict(list)
    for r in routes:
        a, b = r["between"]
        w, toll = r["weight"], r.get("toll", 0)
        adj[a].append((b, w, toll, toll > 0))
        adj[b].append((a, w, toll, toll > 0))
    return adj


def shortest_path(adj, src, dst, max_toll_budget=None):
    """Find shortest path with optional toll consideration."""
    if src == dst:
        return [], 0
    
    # First, try standard routes only (no tolls)
    dist = {src: 0}
    prev = {}
    pq = [(0, 0, src)]  # (total_weight, total_toll, node)
    seen = set()
    
    while pq:
        d, toll_used, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == dst:
            break
        for (v, w, toll, is_fast) in adj.get(u, []):
            # If we have a toll budget, consider fast routes
            if is_fast and max_toll_budget is not None:
                new_toll = toll_used + toll
                if new_toll > max_toll_budget:
                    continue
            elif is_fast:
                continue  # Ignore fast routes by default
            
            nd = d + w
            key = (nd, new_toll if is_fast else toll_used)
            if nd < dist.get(v, 1 << 30):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, new_toll if is_fast else toll_used, v))
    
    if dst not in dist:
        return None, None
    
    hops = []
    cur = dst
    while cur != src:
        hops.append(cur)
        cur = prev[cur]
    hops.reverse()
    
    return hops, dist[dst]


class ImprovedPlanner:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.sim = Simulator(level, const, level_number=level_num)
        self.adj = build_full_adj(level["routes"])
        self.actions = []
        self.best_loop_score_per_tick = 0
        self.best_loop = None
        self._path_cache = {}
        self._dist_cache = {}
        
        # Track town upgrades for scoring
        self.town_upgrades = {t: {"production": set(), "civic": set()} 
                             for t in self.level["towns"].keys()}
        
        # Tool tracking
        self.tools_crafted = set()
        
    def do(self, action):
        """Execute a single action with validation."""
        sim = self.sim
        if sim.tick >= sim.total_ticks:
            return False
        
        atype = action["type"]
        if atype not in sim.KNOWN or atype not in sim.unlocked:
            return False
        
        handlers = {
            "travel": sim._do_travel,
            "gather": sim._do_gather,
            "buy": sim._do_buy,
            "sell": sim._do_sell,
            "craft": sim._do_craft,
            "build": sim._do_build,
            "upkeep": sim._do_upkeep
        }
        
        valid, ticks, fn, detail = handlers[atype](action)
        if not valid:
            return False
        
        before = sim.tick
        if before + ticks > sim.total_ticks:
            return False
        
        fn()
        sim.tick = before + ticks
        self.actions.append(action)
        
        # Update town upgrades tracking
        if atype == "build":
            upgrade_name = action["upgrade"]
            town = sim.location
            if upgrade_name in self.const["upgrades"]["production"]:
                self.town_upgrades[town]["production"].add(upgrade_name)
            elif upgrade_name in self.const["upgrades"]["civic"]:
                self.town_upgrades[town]["civic"].add(upgrade_name)
        
        return True
    
    def path(self, src, dst, max_toll_budget=None):
        """Get path between locations with caching."""
        key = (src, dst, max_toll_budget)
        if key not in self._path_cache:
            self._path_cache[key] = shortest_path(self.adj, src, dst, max_toll_budget)
        return self._path_cache[key]
    
    def dist(self, src, dst):
        """Get distance between locations."""
        key = (src, dst)
        if key not in self._dist_cache:
            hops, d = self.path(src, dst)
            self._dist_cache[key] = d
        return self._dist_cache.get(key, 1 << 30)
    
    def travel_to(self, dst, consider_tolls=False):
        """Travel to destination, optionally using toll roads."""
        if self.sim.location == dst:
            return True
        
        # Try standard route first
        hops, d = self.path(self.sim.location, dst)
        if hops is None:
            return False
        
        # If considering tolls and we have enough enteloot, try fast route
        if consider_tolls and self.sim.current_enteloot() > 100:
            # Try to find a faster route with tolls
            toll_hops, toll_d = self.path(self.sim.location, dst, max_toll_budget=500)
            if toll_hops and toll_d < d * 0.7:  # At least 30% faster
                # Check if tolls are worth it
                tolls_paid = self._calculate_tolls(toll_hops)
                if self.sim.current_enteloot() >= tolls_paid:
                    hops = toll_hops
                    d = toll_d
        
        for h in hops:
            # Check if edge has toll and we should use fast route
            edge_toll = self._get_edge_toll(self.sim.location, h)
            if edge_toll > 0 and self.sim.current_enteloot() >= edge_toll:
                if not self.do({"type": "travel", "destination": h, "fast": True}):
                    return False
            else:
                if not self.do({"type": "travel", "destination": h}):
                    return False
        
        return True
    
    def _get_edge_toll(self, src, dst):
        """Get toll for an edge."""
        for route in self.level["routes"]:
            a, b = route["between"]
            if (a == src and b == dst) or (a == dst and b == src):
                return route.get("toll", 0)
        return 0
    
    def _calculate_tolls(self, hops):
        """Calculate total tolls for a path."""
        total = 0
        current = self.sim.location
        for h in hops:
            total += self._get_edge_toll(current, h)
            current = h
        return total
    
    def find_nearest_town(self, from_location):
        """Find the nearest town to a location."""
        best_town, best_dist = None, 1 << 30
        for town in self.level["towns"].keys():
            hops, d = self.path(from_location, town)
            if hops is not None and d < best_dist:
                best_town, best_dist = town, d
        return best_town
    
    def find_crafting_town(self):
        """Find a town with crafting affinity."""
        # Try to find a town with crafting affinity
        best_town, best_dist = None, 1 << 30
        current = self.sim.location
        
        # First try to find an affinity town
        for town, data in self.level["towns"].items():
            if "crafting" in data.get("affinities", []):
                hops, d = self.path(current, town)
                if hops is not None and d < best_dist:
                    best_town, best_dist = town, d
        
        # If no affinity town, just use nearest town
        if best_town is None:
            return self.find_nearest_town(current)
        
        return best_town
    
    def ensure_resource(self, res, qty_needed):
        """Ensure we have enough of a resource, gathering if needed."""
        have = self.sim.current_amount(res)
        if have >= qty_needed:
            return True
        
        deficit = qty_needed - have
        node_name = self.find_node_for_resource(res)
        if node_name is None:
            return False
        
        if not self.travel_to(node_name):
            return False
        
        node = self.level["nodes"][node_name]
        yield_amt = node["yield"]
        n_gathers = -(-deficit // yield_amt)  # Ceiling division
        
        for _ in range(n_gathers):
            if not self.do({"type": "gather"}):
                return False
        
        return True
    
    def find_node_for_resource(self, res):
        """Find nearest node for a resource."""
        near = self.sim.location
        best_node, best_d = None, 1 << 30
        
        for name, node in self.level["nodes"].items():
            if node["resource"] != res:
                continue
            hops, d = self.path(near, name)
            if hops is not None and d < best_d:
                best_d, best_node = d, name
        
        return best_node
    
    def get_recipe(self, item):
        """Get recipe data for an item."""
        c = self.const
        if item in c.get("components", {}):
            return c["components"][item]
        if item in c.get("recipes", {}):
            return c["recipes"][item]
        if item in c.get("tools", {}):
            return c["tools"][item]
        return None
    
    def _expand_dependencies(self, item, qty, totals, order, seen, depth=0):
        """Expand dependencies recursively."""
        if depth > 20:  # Prevent infinite recursion
            return
        
        totals[item] = totals.get(item, 0) + qty
        data = self.get_recipe(item)
        if data is None:
            return
        
        for res, amt in data["inputs"].items():
            need = amt * qty
            if res in self.const["resources"]:
                totals[res] = totals.get(res, 0) + need
            else:
                self._expand_dependencies(res, need, totals, order, seen, depth + 1)
        
        if item not in seen:
            seen.add(item)
            order.append(item)
    
    def craft_bundle(self, components):
        """Craft a bundle of items with shared dependencies."""
        if not components:
            return True
        
        totals, order, seen = {}, [], set()
        for name, amt in components.items():
            self._expand_dependencies(name, amt, totals, order, seen)
        
        # Gather raw resources first
        for res, need in totals.items():
            if res in self.const["resources"]:
                have = self.sim.current_amount(res)
                if have < need:
                    if not self.ensure_resource(res, need):
                        return False
        
        # CRITICAL FIX: Travel to a town before crafting
        # We need to be at a town to craft ANYTHING
        craft_town = self.find_crafting_town()
        if craft_town is None:
            return False
        if not self.travel_to(craft_town):
            return False
        
        # Build dependency graph for ordering
        deps = {}
        for item in order:
            data = self.get_recipe(item)
            if data:
                deps[item] = [r for r in data["inputs"].keys() 
                             if r not in self.const["resources"]]
        
        # Craft in dependency order (leaf-first)
        crafted = set()
        max_iterations = len(order) * 2
        iterations = 0
        
        while len(crafted) < len(order) and iterations < max_iterations:
            iterations += 1
            for item in order:
                if item in crafted:
                    continue
                # Check if all dependencies are crafted
                if all(d in crafted or d not in totals for d in deps.get(item, [])):
                    need = totals[item]
                    have = self.sim.current_amount(item)
                    if have < need:
                        if not self.do({"type": "craft", "item": item, "quantity": need - have}):
                            return False
                    crafted.add(item)
        
        return len(crafted) == len(order)
    
    def component_value(self, components):
        """Calculate total value of components."""
        total_value = 0
        for item, qty in components.items():
            if item in self.const.get("recipes", {}):
                recipe = self.const["recipes"][item]
                if recipe.get("sellable", False):
                    prices = []
                    for town in self.level["towns"].values():
                        price = town.get("item-rates", {}).get(item)
                        if price is not None:
                            prices.append(price)
                    if prices:
                        total_value += (sum(prices) / len(prices)) * qty
            elif item in self.const["resources"]:
                total_value += self.const["resources"][item].get("sell_price", 0) * qty
        return total_value
    
    def find_best_loop(self):
        """Find the most profitable loop for gathering/crafting/selling."""
        towns = self.level["towns"]
        nodes = self.level["nodes"]
        
        best = None
        
        # Level 1: Raw resources only
        if self.level_num < 2:
            for node_name, node in nodes.items():
                resource = node["resource"]
                price = self.const["resources"].get(resource, {}).get("sell_price")
                if not price:
                    continue
                
                gtime = node.get("gather-time", 2)
                yield_amt = node["yield"]
                
                # Find nearest town to sell
                best_town, best_dist = None, 1 << 30
                for t in towns:
                    hops, d = self.path(node_name, t)
                    if hops is not None and d < best_dist:
                        best_town, best_dist = t, d
                
                if best_town is None:
                    continue
                
                # Back to node
                hops_back, d_back = self.path(best_town, node_name)
                if hops_back is None:
                    continue
                
                # One gather cycle
                cycle_ticks = gtime + best_dist + 1 + d_back
                revenue_per_tick = (yield_amt * price) / cycle_ticks
                
                cand = {
                    "node": node_name,
                    "resource": resource,
                    "recipe": None,
                    "sell_town": best_town,
                    "price": price,
                    "gtime": gtime,
                    "yield_amt": yield_amt,
                    "sell_dist": best_dist,
                    "back_dist": d_back,
                    "score_per_tick": revenue_per_tick,
                    "gathers_needed": 1,
                    "items_per_cycle": yield_amt
                }
                if best is None or cand["score_per_tick"] > best["score_per_tick"]:
                    best = cand
            
            return best
        
        # Level 2+: Crafting
        SINGLE_RES_RECIPE = {
            "wheat": "bread",
            "wood": "wooden-crafts",
            "stone": "stone-works",
            "sheep": "wool-garments"
        }
        
        for node_name, node in nodes.items():
            resource = node["resource"]
            recipe_name = SINGLE_RES_RECIPE.get(resource)
            if not recipe_name:
                continue
            
            recipe = self.const["recipes"][recipe_name]
            if not recipe.get("sellable", False):
                continue
            
            need_per_item = recipe["inputs"][resource]
            gtime = node.get("gather-time", 2)
            yield_amt = node["yield"]
            
            # Evaluate all affinity town and sell town combinations
            for aff_town, aff_data in towns.items():
                if "crafting" not in aff_data.get("affinities", []):
                    continue
                
                hops1, dist1 = self.path(node_name, aff_town)
                if hops1 is None:
                    continue
                
                for sell_town, sell_data in towns.items():
                    price = sell_data.get("item-rates", {}).get(recipe_name)
                    if price is None:
                        continue
                    
                    hops2, dist2 = self.path(aff_town, sell_town)
                    if hops2 is None:
                        continue
                    
                    hops3, dist3 = self.path(sell_town, node_name)
                    if hops3 is None:
                        continue
                    
                    craft_time = 1  # Affinity town bonus
                    
                    # Calculate optimal batch size
                    gathers_needed = -(-need_per_item // yield_amt)
                    items_per_cycle = (gathers_needed * yield_amt) // need_per_item
                    
                    if items_per_cycle == 0:
                        continue
                    
                    cycle_ticks = (gathers_needed * gtime + dist1 + 
                                  items_per_cycle * craft_time + dist2 + 1 + dist3)
                    cycle_revenue = items_per_cycle * price
                    score_per_tick = cycle_revenue / cycle_ticks
                    
                    cand = {
                        "node": node_name,
                        "resource": resource,
                        "recipe": recipe_name,
                        "affinity_town": aff_town,
                        "sell_town": sell_town,
                        "price": price,
                        "gtime": gtime,
                        "yield_amt": yield_amt,
                        "need_per_item": need_per_item,
                        "aff_dist": dist1,
                        "sell_dist": dist2,
                        "back_dist": dist3,
                        "craft_time": craft_time,
                        "score_per_tick": score_per_tick,
                        "gathers_needed": gathers_needed,
                        "items_per_cycle": items_per_cycle
                    }
                    
                    if best is None or cand["score_per_tick"] > best["score_per_tick"]:
                        best = cand
        
        return best
    
    def find_optimal_batch_size(self, best, remaining_ticks):
        """Find optimal batch size for the current loop."""
        if best is None:
            return 0
        
        raw_mode = best["recipe"] is None
        gtime = best["gtime"]
        yield_amt = best["yield_amt"]
        
        best_g = best["gathers_needed"]
        best_score = 0
        
        # Test increasing batch sizes
        step = max(1, best["gathers_needed"] // 10)
        max_g = min(10000, int(remaining_ticks / 5))
        
        for g in range(best["gathers_needed"], max_g + 1, max(1, step)):
            if raw_mode:
                profit = g * yield_amt * best["price"]
                cost = g * gtime + best["sell_dist"] + 1 + best["back_dist"]
            else:
                items = (g * yield_amt) // best["need_per_item"]
                if items <= 0:
                    continue
                profit = items * best["price"]
                cost = (g * gtime + best["aff_dist"] + 
                       items * best["craft_time"] + best["sell_dist"] + 1 + best["back_dist"])
            
            if cost > remaining_ticks:
                break
            
            score = profit / cost
            if score > best_score:
                best_score = score
                best_g = g
        
        return best_g
    
    def bootstrap_tools(self):
        """Craft tools if available and worthwhile."""
        if self.level_num < 3:
            return
        
        # Check if ore is available
        has_ore = any(n["resource"] == "ore" for n in self.level["nodes"].values())
        if not has_ore:
            return
        
        # Craft tools if we can
        if "craft" in self.sim.unlocked:
            # Need iron-fittings x4, rope x2, planks x2
            ok = self.craft_bundle({"iron-fittings": 4, "rope": 2, "planks": 2})
            if ok:
                # craft_bundle already travels to a town, so we're at a town
                self.do({"type": "craft", "item": "boots", "quantity": 1})
                self.do({"type": "craft", "item": "pickaxe", "quantity": 1})
    
    def compute_upgrade_roi(self, town, upg_name, current_tick):
        """Calculate ROI for a production upgrade."""
        T = self.level["run"]["total_ticks"]
        remaining = T - current_tick
        
        upg_data = self.const["upgrades"]["production"][upg_name]
        effect = upg_data.get("effect", {})
        
        if effect.get("type") != "production_double":
            return None
        
        resource = effect["resource"]
        town_data = self.level["towns"][town]
        
        # Base production
        rate = town_data["production"]["rate"]
        base = town_data["production"]["resources"].get(resource, 0)
        if base == 0:
            return None
        
        sell_price = self.const["resources"][resource]["sell_price"]
        
        # Value added per tick (doubles production)
        value_per_tick = (base * sell_price) / rate
        
        # Cost includes components AND enteloot
        components_cost = self.component_value(upg_data.get("components", {}))
        total_cost = upg_data["enteloot_cost"] + components_cost
        
        # Break-even time
        break_even = total_cost / value_per_tick if value_per_tick > 0 else 1 << 30
        
        return {
            "upgrade": upg_name,
            "resource": resource,
            "value_per_tick": value_per_tick,
            "total_cost": total_cost,
            "break_even_ticks": break_even,
            "net_profit": max(0, (remaining - break_even) * value_per_tick),
            "worth_it": break_even < remaining * 0.7  # 30% buffer
        }
    
    def compute_civic_roi(self, town, upg_name, current_tick):
        """Calculate ROI for a civic upgrade."""
        T = self.level["run"]["total_ticks"]
        remaining = T - current_tick
        
        upg_data = self.const["upgrades"]["civic"][upg_name]
        effect = upg_data.get("effect", {})
        
        town_data = self.level["towns"][town]
        rate = town_data["enteloot"]["rate"]
        base_amount = town_data["enteloot"]["amount"]
        
        # Current civic bonuses
        current_pct = 0
        for u in self.sim.town_upgrades[town]["civic"]:
            u_data = self.const["upgrades"]["civic"].get(u, {})
            u_effect = u_data.get("effect", {})
            if u_effect.get("type") == "enteloot_amount_pct":
                current_pct += u_effect["value"]
        
        # Bonus from this upgrade
        new_pct = effect.get("value", 0)
        if effect.get("type") == "enteloot_rate_delta":
            # Police station: reduces rate
            current_rate = rate
            # Check existing police stations
            for u in self.sim.town_upgrades[town]["civic"]:
                u_data = self.const["upgrades"]["civic"].get(u, {})
                u_effect = u_data.get("effect", {})
                if u_effect.get("type") == "enteloot_rate_delta":
                    current_rate = max(1, current_rate + u_effect["value"])
            
            new_rate = max(1, current_rate + effect["value"])
            cycles_before = remaining // current_rate if current_rate > 0 else 0
            cycles_after = remaining // new_rate if new_rate > 0 else 0
            extra_cycles = cycles_after - cycles_before
            value = extra_cycles * base_amount
        else:
            # Percentage bonus
            new_pct = current_pct + effect["value"]
            base_cycle_value = base_amount
            current_value_per_tick = (base_amount * (1 + current_pct)) / rate
            new_value_per_tick = (base_amount * (1 + new_pct)) / rate
            value_per_tick = new_value_per_tick - current_value_per_tick
            value = value_per_tick * remaining
        
        components_cost = self.component_value(upg_data.get("components", {}))
        total_cost = upg_data["enteloot_cost"] + components_cost
        
        return {
            "upgrade": upg_name,
            "value": value,
            "total_cost": total_cost,
            "net_profit": value - total_cost,
            "worth_it": value > total_cost * 1.2  # 20% buffer
        }
    
    def execute_upgrades(self):
        """Execute upgrades based on ROI analysis."""
        T = self.level["run"]["total_ticks"]
        
        # Production upgrades first
        for town in self.level["towns"].keys():
            if not self.travel_to(town):
                continue
            
            # Check each production upgrade
            for upg_name, upg_data in self.const["upgrades"]["production"].items():
                if upg_name in self.town_upgrades[town]["production"]:
                    continue
                if upg_data.get("min_level", 1) > self.level_num:
                    continue
                
                roi = self.compute_upgrade_roi(town, upg_name, self.sim.tick)
                if roi and roi["worth_it"] and roi["net_profit"] > 0:
                    if self.sim.current_enteloot() >= upg_data["enteloot_cost"]:
                        if self.craft_bundle(upg_data.get("components", {})):
                            if self.travel_to(town):
                                self.do({"type": "build", "upgrade": upg_name})
        
        # Civic upgrades
        for town in self.level["towns"].keys():
            if not self.travel_to(town):
                continue
            
            for upg_name, upg_data in self.const["upgrades"]["civic"].items():
                if upg_name in self.town_upgrades[town]["civic"]:
                    continue
                if upg_data.get("min_level", 1) > self.level_num:
                    continue
                
                # Check prerequisites
                prereq = upg_data.get("prerequisite")
                if prereq:
                    if prereq["type"] == "any_production_upgrades":
                        if len(self.town_upgrades[town]["production"]) < prereq["count"]:
                            continue
                    elif prereq["type"] == "specific_upgrade":
                        req = prereq["upgrade"]
                        if (req not in self.town_upgrades[town]["production"] and 
                            req not in self.town_upgrades[town]["civic"]):
                            continue
                
                roi = self.compute_civic_roi(town, upg_name, self.sim.tick)
                if roi and roi["worth_it"] and roi["net_profit"] > 0:
                    if self.sim.current_enteloot() >= upg_data["enteloot_cost"]:
                        if self.craft_bundle(upg_data.get("components", {})):
                            if self.travel_to(town):
                                self.do({"type": "build", "upgrade": upg_name})
    
    def should_use_upkeep(self, town, current_tick):
        """Determine if upkeep is worth using."""
        if "upkeep" not in self.sim.unlocked:
            return False
        
        T = self.level["run"]["total_ticks"]
        remaining = T - current_tick
        if remaining < 100:  # Not worth it near end
            return False
        
        # Calculate boost value
        boost_duration = self.const["constants"]["upkeep_boost_duration_ticks"]
        boost_mult = self.const["constants"]["upkeep_boost_multiplier"]
        
        # Account for fire-station duration bonus
        duration_bonus = 0
        for u in self.town_upgrades[town]["civic"]:
            eff = self.const["upgrades"]["civic"].get(u, {}).get("effect", {})
            if eff.get("type") == "upkeep_boost_duration_pct":
                duration_bonus += eff["value"]
        
        effective_duration = int(boost_duration * (1 + duration_bonus))
        
        # Value per tick from this town's Enteloot
        town_data = self.level["towns"][town]
        base_rate = town_data["enteloot"]["rate"]
        base_amount = town_data["enteloot"]["amount"]
        
        # Current civic bonuses
        pct = 0
        for u in self.town_upgrades[town]["civic"]:
            u_data = self.const["upgrades"]["civic"].get(u, {})
            u_effect = u_data.get("effect", {})
            if u_effect.get("type") == "enteloot_amount_pct":
                pct += u_effect["value"]
        
        modified_amount = int(base_amount * (1 + pct))
        
        # Extra enteloot from boost (doubles production)
        extra_per_cycle = modified_amount * (boost_mult - 1)
        cycles_during_boost = effective_duration // base_rate
        boost_value = extra_per_cycle * cycles_during_boost
        
        # Opportunity cost: what could we earn in 5 ticks?
        opportunity_cost = self.best_loop_score_per_tick * 5
        
        return boost_value > opportunity_cost * 1.2  # 20% buffer
    
    def execute_upkeep(self):
        """Execute upkeep actions where beneficial."""
        if "upkeep" not in self.sim.unlocked:
            return
        
        T = self.level["run"]["total_ticks"]
        
        # Find towns where upkeep is valuable
        for town in self.level["towns"].keys():
            if not self.travel_to(town):
                continue
            
            if self.should_use_upkeep(town, self.sim.tick):
                # Check if upkeep is already active
                active = False
                for s, e in self.sim.boost_windows.get(town, []):
                    if s <= self.sim.tick < e:
                        active = True
                        break
                
                if not active:
                    self.do({"type": "upkeep"})
    
    def grind(self, tick_budget_limit):
        """Execute the most profitable loop repeatedly."""
        if self.best_loop is None:
            self.best_loop = self.find_best_loop()
            if self.best_loop is None:
                return
            self.best_loop_score_per_tick = self.best_loop["score_per_tick"]
        
        best = self.best_loop
        raw_mode = best["recipe"] is None
        T = self.level["run"]["total_ticks"]
        
        while self.sim.tick < tick_budget_limit:
            remaining = tick_budget_limit - self.sim.tick
            
            # Find optimal batch size
            g = self.find_optimal_batch_size(best, remaining)
            if g < best["gathers_needed"]:
                break
            
            # Execute the loop
            if not self.travel_to(best["node"]):
                break
            
            for _ in range(g):
                if not self.do({"type": "gather"}):
                    break
            
            if raw_mode:
                items = g * best["yield_amt"]
                if not self.travel_to(best["sell_town"]):
                    break
                if not self.do({"type": "sell", "item": best["resource"], "quantity": items}):
                    break
            else:
                items = (g * best["yield_amt"]) // best["need_per_item"]
                if items <= 0:
                    break
                
                if not self.travel_to(best["affinity_town"]):
                    break

                craft_time = best["craft_time"]
                max_craftable = (remaining - best["sell_dist"] - 1) // craft_time
                if max_craftable <= 0:
                    break
                items = min(items, max_craftable)
                
                if not self.do({"type": "craft", "item": best["recipe"], "quantity": items}):
                    break
                
                if best["affinity_town"] != best["sell_town"]:
                    if not self.travel_to(best["sell_town"]):
                        break
                
                if not self.do({"type": "sell", "item": best["recipe"], "quantity": items}):
                    break
    
    def calculate_final_score(self):
        """Calculate final score according to spec."""
        enteloot = self.sim.current_enteloot()
        
        # Held value
        held_value = 0
        for r, cnt in self.sim.current_inventory().items():
            if cnt <= 0:
                continue
            if r in self.const["recipes"] and self.const["recipes"][r].get("sellable"):
                # Use max sell price across towns
                max_price = 0
                for t in self.level["towns"].values():
                    max_price = max(max_price, t.get("item-rates", {}).get(r, 0))
                held_value += cnt * max_price
            elif r in self.const["resources"]:
                held_value += cnt * self.const["resources"][r].get("sell_price", 0)
        
        # Items sold multiplier
        sell_mult = self.const["constants"]["sell_bonus_multiplier"] if self.sim.items_sold_count > 0 else 1.0
        
        base_score = (enteloot + held_value) * sell_mult
        
        # Infrastructure score (Level 2+)
        infra_score = 0
        developed_towns = 0
        
        for town, upgrades in self.town_upgrades.items():
            prod_count = len(upgrades["production"])
            civic_count = len(upgrades["civic"])
            
            # Production upgrades: 1000 each
            for u in upgrades["production"]:
                infra_score += self.const["upgrades"]["production"][u].get("score_value", 1000)
            
            # Civic upgrades: 3000-6000 each
            for u in upgrades["civic"]:
                infra_score += self.const["upgrades"]["civic"][u].get("score_value", 3000)
            
            if prod_count > 0 or civic_count > 0:
                developed_towns += 1
        
        # Spread multiplier: 10% per developed town
        if developed_towns > 0:
            infra_score *= (1 + developed_towns * 0.1)
        
        return base_score + infra_score
    
    def solve(self):
        """Main solve method."""
        T = self.level["run"]["total_ticks"]
        
        # Phase 1: Early game - bootstrap and find best loop
        self.bootstrap_tools()
        self.best_loop = self.find_best_loop()
        if self.best_loop:
            self.best_loop_score_per_tick = self.best_loop["score_per_tick"]
        
        # Phase 2: Mid game - grind and build upgrades
        mid_point = int(T * 0.6)
        self.grind(mid_point)
        
        # Execute upgrades
        self.execute_upgrades()
        
        # Execute upkeep (Level 4)
        self.execute_upkeep()
        
        # Phase 3: Late game - grind remaining time
        self.grind(T)
        
        return self.actions


def solve_level(level, const, level_num):
    """Main solver entry point."""
    planner = ImprovedPlanner(level, const, level_num)
    actions = planner.solve()
    
    # Run simulation to verify
    sim = Simulator(level, const, level_number=level_num)
    log = sim.run(actions)
    
    print(f"Generated {len(actions)} actions")
    invalid = [e for e in log if not e.valid]
    print(f"Invalid actions: {len(invalid)} / {len(log)}")
    
    if invalid:
        print("First few invalid actions:")
        for e in invalid[:5]:
            print(f"  {e.index}: {e.action} - {e.detail}")
    
    print("Final summary:")
    print(sim.summary())
    
    # Calculate final score
    final_score = planner.calculate_final_score()
    print(f"Calculated final score: {final_score:.2f}")
    
    return actions


if __name__ == "__main__":
    lvl_num = int(sys.argv[1])
    
    with open(f"Level{lvl_num}.json") as f:
        level = json.load(f)
    with open("constants.json") as f:
        const = json.load(f)
    
    actions = solve_level(level, const, lvl_num)
    
    with open(f"output_level{lvl_num}_v3.txt", "w") as f:
        json.dump({"actions": actions}, f)
    print(f"Wrote output_level{lvl_num}_v3.txt")