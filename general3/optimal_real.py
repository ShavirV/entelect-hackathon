import json
import sys
import math
from collections import defaultdict
from Simulator import Simulator

class RealOptimizer:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.sim = Simulator(level, const, level_number=level_num)
        self.actions = []
        
        # Build adjacency
        self.adj = defaultdict(list)
        for route in level["routes"]:
            a, b = route["between"]
            w, toll = route["weight"], route.get("toll", 0)
            self.adj[a].append((b, w, toll))
            self.adj[b].append((a, w, toll))
        
        # Pre-compute distances
        self._dist_cache = {}
        self._path_cache = {}
        
        # Track state
        self.tools_crafted = set()
        self.town_upgrades = {t: {"production": set(), "civic": set()} 
                             for t in level["towns"].keys()}
        self.boost_windows = defaultdict(list)
    
    def do(self, action):
        """Execute action with validation."""
        sim = self.sim
        if sim.tick >= sim.total_ticks:
            return False
        
        atype = action["type"]
        if atype not in sim.KNOWN:
            return False
        if atype not in sim.unlocked:
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
        
        # Update tracking
        if atype == "build":
            upgrade_name = action["upgrade"]
            town = sim.location
            if upgrade_name in self.const["upgrades"]["production"]:
                self.town_upgrades[town]["production"].add(upgrade_name)
            elif upgrade_name in self.const["upgrades"]["civic"]:
                self.town_upgrades[town]["civic"].add(upgrade_name)
        
        return True
    
    def dist(self, a, b):
        """Get shortest distance between locations."""
        if a == b:
            return 0
        key = (a, b)
        if key in self._dist_cache:
            return self._dist_cache[key]
        
        # Dijkstra
        dist = {a: 0}
        prev = {}
        pq = [(0, a)]
        seen = set()
        
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == b:
                break
            for v, w, _ in self.adj.get(u, []):
                if v not in dist or d + w < dist[v]:
                    dist[v] = d + w
                    prev[v] = u
                    heapq.heappush(pq, (d + w, v))
        
        if b not in dist:
            self._dist_cache[key] = 999999
            return 999999
        
        self._dist_cache[key] = dist[b]
        return dist[b]
    
    def path(self, a, b):
        """Get shortest path between locations."""
        if a == b:
            return []
        key = (a, b)
        if key in self._path_cache:
            return self._path_cache[key]
        
        dist = {a: 0}
        prev = {}
        pq = [(0, a)]
        seen = set()
        
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == b:
                break
            for v, w, _ in self.adj.get(u, []):
                if v not in dist or d + w < dist[v]:
                    dist[v] = d + w
                    prev[v] = u
                    heapq.heappush(pq, (d + w, v))
        
        if b not in dist:
            self._path_cache[key] = None
            return None
        
        hops = []
        cur = b
        while cur != a:
            hops.append(cur)
            cur = prev[cur]
        hops.reverse()
        self._path_cache[key] = hops
        return hops
    
    def travel_to(self, dest, fast=False):
        """Travel to destination."""
        if self.sim.location == dest:
            return True
        
        hops = self.path(self.sim.location, dest)
        if hops is None:
            return False
        
        for h in hops:
            # Check if fast route exists
            edge_toll = self._get_toll(self.sim.location, h)
            if edge_toll > 0 and fast and self.sim.current_enteloot() >= edge_toll:
                if not self.do({"type": "travel", "destination": h, "fast": True}):
                    return False
            else:
                if not self.do({"type": "travel", "destination": h}):
                    return False
        
        return True
    
    def _get_toll(self, a, b):
        """Get toll between two locations."""
        for route in self.level["routes"]:
            x, y = route["between"]
            if (x == a and y == b) or (x == b and y == a):
                return route.get("toll", 0)
        return 0
    
    def gather_resource(self, resource, amount_needed):
        """Gather enough of a resource."""
        have = self.sim.current_amount(resource)
        if have >= amount_needed:
            return True
        
        deficit = amount_needed - have
        
        # Find best node for this resource
        best_node = None
        best_dist = 999999
        best_yield = 0
        
        for node_name, node_data in self.level["nodes"].items():
            if node_data["resource"] != resource:
                continue
            d = self.dist(self.sim.location, node_name)
            yield_amt = node_data["yield"]
            # Score: yield / (distance + gather_time)
            gather_time = node_data.get("gather-time", 2)
            score = yield_amt / (d + gather_time)
            if score > best_score:
                best_score = score
                best_node = node_name
                best_yield = yield_amt
        
        if best_node is None:
            return False
        
        if not self.travel_to(best_node):
            return False
        
        gathers_needed = math.ceil(deficit / best_yield)
        for _ in range(gathers_needed):
            if not self.do({"type": "gather"}):
                return False
        
        return True
    
    def craft_items(self, item, quantity):
        """Craft items, handling dependencies."""
        # Find recipe
        recipe = None
        if item in self.const.get("recipes", {}):
            recipe = self.const["recipes"][item]
        elif item in self.const.get("components", {}):
            recipe = self.const["components"][item]
        elif item in self.const.get("tools", {}):
            recipe = self.const["tools"][item]
        
        if recipe is None:
            return False
        
        # Gather inputs
        for input_item, amount in recipe["inputs"].items():
            total_needed = amount * quantity
            if input_item in self.const["resources"]:
                if not self.gather_resource(input_item, total_needed):
                    return False
            else:
                # Need to craft dependency
                if not self.craft_items(input_item, total_needed):
                    return False
        
        # Find crafting town with affinity
        craft_town = self.sim.location
        for town, data in self.level["towns"].items():
            if "crafting" in data.get("affinities", []):
                craft_town = town
                break
        
        if self.sim.location != craft_town:
            if not self.travel_to(craft_town):
                return False
        
        # Check if we're at a town
        if self.sim.location not in self.level["towns"]:
            # Travel to nearest town
            nearest = list(self.level["towns"].keys())[0]
            if not self.travel_to(nearest):
                return False
        
        return self.do({"type": "craft", "item": item, "quantity": quantity})
    
    def sell_items(self, item, quantity):
        """Sell items at best town."""
        # Find best town to sell
        best_town = None
        best_price = 0
        
        for town, data in self.level["towns"].items():
            if item in self.const["resources"]:
                price = self.const["resources"][item].get("sell_price", 0)
            else:
                price = data.get("item-rates", {}).get(item, 0)
            
            if price > best_price:
                best_price = price
                best_town = town
        
        if best_town is None or best_price == 0:
            return False
        
        if self.sim.location != best_town:
            if not self.travel_to(best_town):
                return False
        
        return self.do({"type": "sell", "item": item, "quantity": quantity})

# ============================================================
# REAL OPTIMIZED STRATEGIES
# ============================================================

def level1_optimized(opt):
    """Level 1: Gather high-value resources repeatedly."""
    print("  Phase 1: Find best resources")
    
    # Evaluate all nodes
    node_values = []
    for node_name, node_data in opt.level["nodes"].items():
        resource = node_data["resource"]
        sell_price = opt.const["resources"].get(resource, {}).get("sell_price", 0)
        if sell_price == 0:
            continue
        yield_amt = node_data["yield"]
        gtime = node_data.get("gather-time", 2)
        
        # Score = (yield * price) / (gather_time + travel_time)
        # Travel to Demacia (starting town) and back
        travel_to = opt.dist("Demacia", node_name)
        travel_back = opt.dist(node_name, "Demacia")
        total_cycle_time = travel_to + gtime + 1 + travel_back  # +1 for sell action
        value_per_tick = (yield_amt * sell_price) / total_cycle_time
        
        node_values.append({
            "node": node_name,
            "resource": resource,
            "yield": yield_amt,
            "price": sell_price,
            "gtime": gtime,
            "travel_to": travel_to,
            "travel_back": travel_back,
            "cycle_time": total_cycle_time,
            "value_per_tick": value_per_tick
        })
    
    node_values.sort(key=lambda x: x["value_per_tick"], reverse=True)
    
    print(f"  Best node: {node_values[0]['node']} ({node_values[0]['resource']})")
    print(f"  Value per tick: {node_values[0]['value_per_tick']:.2f}")
    
    # Use the best node
    best = node_values[0]
    total_ticks = opt.level["run"]["total_ticks"]
    
    # Calculate how many cycles we can do
    cycles = int(total_ticks / best["cycle_time"])
    print(f"  Can do {cycles} cycles")
    
    for i in range(cycles):
        if i % 50 == 0:
            print(f"    Cycle {i+1}/{cycles}")
        
        if not opt.travel_to(best["node"]):
            break
        if not opt.do({"type": "gather"}):
            break
        if not opt.travel_to("Demacia"):
            break
        if not opt.do({"type": "sell", "item": best["resource"], "quantity": best["yield"]}):
            break
    
    return opt.actions

def level2_optimized(opt):
    """Level 2: Optimize crafting with bulk operations."""
    print("  Phase 1: Finding best recipe")
    
    # Find best recipe
    best_recipe = None
    best_profit_per_tick = 0
    
    for recipe_name, recipe_data in opt.const["recipes"].items():
        if not recipe_data.get("sellable", False):
            continue
        
        # Calculate average sell price
        total_price = 0
        count = 0
        for town_data in opt.level["towns"].values():
            price = town_data.get("item-rates", {}).get(recipe_name, 0)
            if price > 0:
                total_price += price
                count += 1
        
        if count == 0:
            continue
        
        avg_price = total_price / count
        
        # Calculate input costs and gathering requirements
        total_input_cost = 0
        gathering_needed = {}
        
        for res, amt in recipe_data["inputs"].items():
            buy_price = opt.const["resources"].get(res, {}).get("buy_price", 0)
            total_input_cost += buy_price * amt
            
            # Find best node for this resource
            best_node = None
            best_yield = 0
            for node_name, node_data in opt.level["nodes"].items():
                if node_data["resource"] == res:
                    if node_data["yield"] > best_yield:
                        best_yield = node_data["yield"]
                        best_node = node_name
            
            if best_node:
                gathering_needed[res] = {
                    "node": best_node,
                    "yield": best_yield,
                    "needed": amt,
                    "gathers": math.ceil(amt / best_yield)
                }
        
        profit = avg_price - total_input_cost
        if profit <= 0:
            continue
        
        # Calculate time per batch
        # Travel to each node, gather, travel to craft town, craft, travel to sell town, sell
        craft_town = None
        for town, data in opt.level["towns"].items():
            if "crafting" in data.get("affinities", []):
                craft_town = town
                break
        if craft_town is None:
            craft_town = list(opt.level["towns"].keys())[0]
        
        # Find sell town
        sell_town = None
        best_sell_price = 0
        for town, data in opt.level["towns"].items():
            price = data.get("item-rates", {}).get(recipe_name, 0)
            if price > best_sell_price:
                best_sell_price = price
                sell_town = town
        
        if sell_town is None:
            continue
        
        # Calculate total time for one batch (1 item)
        total_time = 0
        start = "Demacia"
        current = start
        
        for res, info in gathering_needed.items():
            # Travel from current to node
            total_time += opt.dist(current, info["node"])
            current = info["node"]
            # Gather
            total_time += info["gathers"] * opt.level["nodes"][info["node"]].get("gather-time", 2)
        
        # Travel to craft town
        total_time += opt.dist(current, craft_town)
        current = craft_town
        
        # Craft (1 tick with affinity)
        total_time += 1
        
        # Travel to sell town
        total_time += opt.dist(current, sell_town)
        current = sell_town
        
        # Sell
        total_time += 1
        
        profit_per_tick = profit / total_time if total_time > 0 else 0
        
        if profit_per_tick > best_profit_per_tick:
            best_profit_per_tick = profit_per_tick
            best_recipe = {
                "name": recipe_name,
                "profit": profit,
                "profit_per_tick": profit_per_tick,
                "craft_town": craft_town,
                "sell_town": sell_town,
                "sell_price": best_sell_price,
                "gathering": gathering_needed,
                "time_per_item": total_time
            }
    
    if best_recipe is None:
        print("  No profitable recipe found")
        return []
    
    print(f"  Best recipe: {best_recipe['name']}")
    print(f"  Profit per tick: {best_recipe['profit_per_tick']:.2f}")
    
    # Execute in bulk
    total_ticks = opt.level["run"]["total_ticks"]
    
    # Calculate optimal batch size
    items_per_batch = 1
    max_batch_time = min(1000, total_ticks // 10)  # Don't spend more than 1000 ticks per batch
    
    while (items_per_batch + 1) * best_recipe["time_per_item"] < max_batch_time:
        items_per_batch += 1
    
    print(f"  Batch size: {items_per_batch} items")
    
    while opt.sim.tick < total_ticks:
        remaining = total_ticks - opt.sim.tick
        if remaining < best_recipe["time_per_item"]:
            break
        
        # Determine batch size for this iteration
        batch = min(items_per_batch, int(remaining / best_recipe["time_per_item"]))
        if batch == 0:
            break
        
        # Gather all resources
        current = opt.sim.location
        for res, info in best_recipe["gathering"].items():
            total_needed = info["needed"] * batch
            gathers_needed = math.ceil(total_needed / info["yield"])
            
            if opt.sim.location != info["node"]:
                if not opt.travel_to(info["node"]):
                    break
            
            for _ in range(gathers_needed):
                if not opt.do({"type": "gather"}):
                    break
        
        # Travel to craft town
        if opt.sim.location != best_recipe["craft_town"]:
            if not opt.travel_to(best_recipe["craft_town"]):
                break
        
        # Craft
        if not opt.do({"type": "craft", "item": best_recipe["name"], "quantity": batch}):
            break
        
        # Travel to sell town
        if opt.sim.location != best_recipe["sell_town"]:
            if not opt.travel_to(best_recipe["sell_town"]):
                break
        
        # Sell
        if not opt.do({"type": "sell", "item": best_recipe["name"], "quantity": batch}):
            break
        
        if len(opt.actions) % 1000 == 0:
            print(f"    Progress: {opt.sim.tick}/{total_ticks} ticks, {len(opt.actions)} actions")
    
    return opt.actions

def level3_optimized(opt):
    """Level 3: Tools + optimized crafting."""
    print("  Phase 1: Tool Rush")
    
    # Check if tools are available
    if "tools" in opt.const and opt.level_num >= 3:
        # Craft tools if beneficial
        try:
            # Find ore node
            ore_node = None
            for node_name, node_data in opt.level["nodes"].items():
                if node_data["resource"] == "ore":
                    ore_node = node_name
                    break
            
            if ore_node:
                # Gather ore
                if opt.travel_to(ore_node):
                    # Need 4 ore for 2 iron-fittings
                    opt.do({"type": "gather"})
                    opt.do({"type": "gather"})
                    
                    # Find crafting town
                    craft_town = "Demacia"
                    for town, data in opt.level["towns"].items():
                        if "crafting" in data.get("affinities", []):
                            craft_town = town
                            break
                    
                    if opt.travel_to(craft_town):
                        opt.do({"type": "craft", "item": "iron-fittings", "quantity": 2})
                        opt.do({"type": "craft", "item": "rope", "quantity": 2})
                        opt.do({"type": "craft", "item": "planks", "quantity": 2})
                        opt.do({"type": "craft", "item": "boots", "quantity": 1})
                        opt.do({"type": "craft", "item": "pickaxe", "quantity": 1})
                        print("  Tools crafted!")
        except:
            pass
    
    # Now do optimized crafting
    print("  Phase 2: Optimized Crafting")
    return level2_optimized(opt)

def level4_optimized(opt):
    """Level 4: Full optimization with upgrades and upkeep."""
    print("  Phase 1: Tool Rush")
    
    # Tools
    if "tools" in opt.const and opt.level_num >= 3:
        try:
            ore_node = None
            for node_name, node_data in opt.level["nodes"].items():
                if node_data["resource"] == "ore":
                    ore_node = node_name
                    break
            
            if ore_node:
                if opt.travel_to(ore_node):
                    opt.do({"type": "gather"})
                    opt.do({"type": "gather"})
                    
                    craft_town = "Demacia"
                    for town, data in opt.level["towns"].items():
                        if "crafting" in data.get("affinities", []):
                            craft_town = town
                            break
                    
                    if opt.travel_to(craft_town):
                        opt.do({"type": "craft", "item": "iron-fittings", "quantity": 2})
                        opt.do({"type": "craft", "item": "rope", "quantity": 2})
                        opt.do({"type": "craft", "item": "planks", "quantity": 2})
                        opt.do({"type": "craft", "item": "boots", "quantity": 1})
                        opt.do({"type": "craft", "item": "pickaxe", "quantity": 1})
                        print("  Tools crafted!")
        except:
            pass
    
    print("  Phase 2: Build Production Upgrades")
    
    # Build production upgrades where profitable
    T = opt.level["run"]["total_ticks"]
    
    for town, town_data in opt.level["towns"].items():
        # For each production upgrade
        for upgrade_name, upgrade_data in opt.const["upgrades"]["production"].items():
            if upgrade_data.get("min_level", 1) > opt.level_num:
                continue
            if upgrade_name in opt.town_upgrades[town]["production"]:
                continue
            
            # Check if upgrade is worth it
            resource = upgrade_data.get("effect", {}).get("resource")
            if resource is None:
                continue
            
            base_prod = town_data["production"]["resources"].get(resource, 0)
            if base_prod == 0:
                continue
            
            sell_price = opt.const["resources"].get(resource, {}).get("sell_price", 0)
            if sell_price == 0:
                continue
            
            # Value added per tick
            rate = town_data["production"]["rate"]
            value_per_tick = (base_prod * sell_price) / rate
            
            remaining = T - opt.sim.tick
            total_value = value_per_tick * remaining
            
            # Cost of components
            component_cost = 0
            for comp, amt in upgrade_data.get("components", {}).items():
                # Estimate component cost
                if comp in opt.const.get("recipes", {}):
                    recipe = opt.const["recipes"][comp]
                    for res, r_amt in recipe.get("inputs", {}).items():
                        component_cost += opt.const["resources"].get(res, {}).get("buy_price", 0) * r_amt * amt
                elif comp in opt.const.get("components", {}):
                    recipe = opt.const["components"][comp]
                    for res, r_amt in recipe.get("inputs", {}).items():
                        component_cost += opt.const["resources"].get(res, {}).get("buy_price", 0) * r_amt * amt
            
            total_cost = upgrade_data["enteloot_cost"] + component_cost
            
            if total_value > total_cost * 1.5:  # Worth it
                print(f"  Building {upgrade_name} in {town} (value: {total_value:.0f}, cost: {total_cost:.0f})")
                
                # Craft components
                if opt.travel_to(town):
                    for comp, amt in upgrade_data.get("components", {}).items():
                        if not opt.do({"type": "craft", "item": comp, "quantity": amt}):
                            break
                    
                    if opt.do({"type": "build", "upgrade": upgrade_name}):
                        print(f"    Built {upgrade_name}")
    
    print("  Phase 3: Optimized Crafting")
    actions = level2_optimized(opt)
    
    print("  Phase 4: Upkeep")
    if "upkeep" in opt.sim.unlocked:
        # Find best town for upkeep
        best_town = None
        best_value = 0
        for town, town_data in opt.level["towns"].items():
            amount = town_data["enteloot"]["amount"]
            rate = town_data["enteloot"]["rate"]
            value = amount / rate
            if value > best_value:
                best_value = value
                best_town = town
        
        if best_town:
            # Do upkeep periodically
            for _ in range(min(5, (T - opt.sim.tick) // 100)):
                if opt.travel_to(best_town):
                    if not opt.do({"type": "upkeep"}):
                        break
    
    return actions

# ============================================================
# MAIN
# ============================================================

def solve_level(level_num):
    with open(f"Level{level_num}.json") as f:
        level = json.load(f)
    with open("constants.json") as f:
        const = json.load(f)
    
    print(f"\n{'='*60}")
    print(f"Solving Level {level_num} with Real Optimizer")
    print(f"{'='*60}")
    
    opt = RealOptimizer(level, const, level_num)
    
    if level_num == 1:
        actions = level1_optimized(opt)
    elif level_num == 2:
        actions = level2_optimized(opt)
    elif level_num == 3:
        actions = level3_optimized(opt)
    elif level_num == 4:
        actions = level4_optimized(opt)
    else:
        print("Unknown level")
        return
    
    # Run simulation
    sim = Simulator(level, const, level_number=level_num)
    log = sim.run(actions)
    
    invalid = [e for e in log if not e.valid]
    print(f"\nResults:")
    print(f"  Total actions: {len(actions)}")
    print(f"  Invalid actions: {len(invalid)}")
    print(f"  Final tick: {sim.tick}")
    print(f"  Final Enteloot: {sim.current_enteloot():.0f}")
    
    summary = sim.summary()
    print(f"  Held Value: {summary['held_value']:.0f}")
    print(f"  Upgrades Built: {summary['upgrades_built']}")
    print(f"  Items Sold: {summary['items_sold_count']}")
    print(f"  Estimated Score: {summary['estimated_score']:.0f}")
    
    # Save
    with open(f"real_optimized_level{level_num}.txt", "w") as f:
        json.dump({"actions": actions}, f)
    print(f"  Saved to real_optimized_level{level_num}.txt")
    
    return actions

if __name__ == "__main__":
    import heapq  # For Dijkstra
    level_num = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    solve_level(level_num)