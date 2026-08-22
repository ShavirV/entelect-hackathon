import json
import sys
import math
import heapq
from collections import defaultdict
from Simulator import Simulator

class LevelAwareSolver:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.sim = Simulator(level, const, level_number=level_num)
        self.actions = []
        
        # Get unlocked actions for this level
        self.unlocked = set()
        for lvl, toks in const["level_unlocks"].items():
            if int(lvl) <= level_num:
                self.unlocked.update(toks)
        
        print(f"Level {level_num} unlocked actions: {sorted(self.unlocked)}")
        
        # Build adjacency
        self.adj = defaultdict(list)
        for route in level["routes"]:
            a, b = route["between"]
            w, toll = route["weight"], route.get("toll", 0)
            self.adj[a].append((b, w, toll))
            self.adj[b].append((a, w, toll))
        
        # Pre-compute distances
        self._dist_cache = {}
        
        # Track state
        self.tools_crafted = set()
        self.town_upgrades = {t: {"production": set(), "civic": set()} 
                             for t in level["towns"].keys()}
    
    def do(self, action):
        """Execute action with validation."""
        sim = self.sim
        if sim.tick >= sim.total_ticks:
            return False
        
        atype = action["type"]
        if atype not in sim.KNOWN:
            return False
        if atype not in self.unlocked:
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
                    heapq.heappush(pq, (d + w, v))
        
        if b not in dist:
            self._dist_cache[key] = 999999
            return 999999
        
        self._dist_cache[key] = dist[b]
        return dist[b]
    
    def travel_to(self, dest):
        """Travel to destination."""
        if self.sim.location == dest:
            return True
        
        # Use Dijkstra to find path
        dist = {self.sim.location: 0}
        prev = {}
        pq = [(0, self.sim.location)]
        seen = set()
        
        while pq:
            d, u = heapq.heappop(pq)
            if u in seen:
                continue
            seen.add(u)
            if u == dest:
                break
            for v, w, _ in self.adj.get(u, []):
                if v not in dist or d + w < dist[v]:
                    dist[v] = d + w
                    prev[v] = u
                    heapq.heappush(pq, (d + w, v))
        
        if dest not in dist:
            return False
        
        # Reconstruct path
        hops = []
        cur = dest
        while cur != self.sim.location:
            hops.append(cur)
            cur = prev[cur]
        hops.reverse()
        
        for h in hops:
            if not self.do({"type": "travel", "destination": h}):
                return False
        
        return True

# ============================================================
# LEVEL-SPECIFIC STRATEGIES (RESPECTING UNLOCKS)
# ============================================================

def solve_level1(opt):
    """Level 1: Only travel, gather, buy, sell are allowed."""
    print("  Phase 1: Finding best resources")
    
    # Evaluate all nodes
    node_values = []
    start = opt.level["run"]["starting_town"]
    
    for node_name, node_data in opt.level["nodes"].items():
        resource = node_data["resource"]
        sell_price = opt.const["resources"].get(resource, {}).get("sell_price", 0)
        if sell_price == 0:
            continue
        yield_amt = node_data["yield"]
        gtime = node_data.get("gather-time", 2)
        
        # Score = (yield * price) / (gather_time + travel_to + travel_back + sell_time)
        travel_to = opt.dist(start, node_name)
        travel_back = opt.dist(node_name, start)
        total_cycle = travel_to + gtime + 1 + travel_back  # +1 for sell
        value_per_tick = (yield_amt * sell_price) / total_cycle if total_cycle > 0 else 0
        
        node_values.append({
            "node": node_name,
            "resource": resource,
            "yield": yield_amt,
            "price": sell_price,
            "gtime": gtime,
            "travel_to": travel_to,
            "travel_back": travel_back,
            "cycle_time": total_cycle,
            "value_per_tick": value_per_tick
        })
    
    node_values.sort(key=lambda x: x["value_per_tick"], reverse=True)
    
    if not node_values:
        return []
    
    best = node_values[0]
    print(f"  Best node: {best['node']} ({best['resource']})")
    print(f"  Value per tick: {best['value_per_tick']:.2f}")
    
    total_ticks = opt.level["run"]["total_ticks"]
    cycles = int(total_ticks / best["cycle_time"])
    print(f"  Can do {cycles} cycles")
    
    for _ in range(cycles):
        if not opt.travel_to(best["node"]):
            break
        if not opt.do({"type": "gather"}):
            break
        if not opt.travel_to(start):
            break
        if not opt.do({"type": "sell", "item": best["resource"], "quantity": best["yield"]}):
            break
    
    return opt.actions

def solve_level2(opt):
    """Level 2: Crafting and building enabled."""
    print("  Phase 1: Finding best recipe")
    
    # Find best craftable item
    best_recipe = None
    best_value = 0
    craft_town = None
    
    # Find crafting affinity town
    for town, data in opt.level["towns"].items():
        if "crafting" in data.get("affinities", []):
            craft_town = town
            break
    if craft_town is None:
        craft_town = list(opt.level["towns"].keys())[0]
    
    print(f"  Using craft town: {craft_town}")
    
    for recipe_name, recipe_data in opt.const["recipes"].items():
        if not recipe_data.get("sellable", False):
            continue
        
        # Find best sell price
        sell_town = None
        best_price = 0
        for town, data in opt.level["towns"].items():
            price = data.get("item-rates", {}).get(recipe_name, 0)
            if price > best_price:
                best_price = price
                sell_town = town
        
        if best_price == 0:
            continue
        
        # Calculate cost and gathering requirements
        total_cost = 0
        gather_info = {}
        
        for res, amt in recipe_data["inputs"].items():
            # Check buy price (if available)
            buy_price = opt.const["resources"].get(res, {}).get("buy_price")
            if buy_price is not None:
                total_cost += buy_price * amt
            
            # Find node for this resource
            best_node = None
            best_yield = 0
            for node_name, node_data in opt.level["nodes"].items():
                if node_data["resource"] == res:
                    if node_data["yield"] > best_yield:
                        best_yield = node_data["yield"]
                        best_node = node_name
            
            if best_node:
                gather_info[res] = {
                    "node": best_node,
                    "yield": best_yield,
                    "needed": amt,
                    "gathers": math.ceil(amt / best_yield)
                }
        
        profit = best_price - total_cost
        if profit <= 0:
            continue
        
        # Calculate time per item
        total_time = 0
        current = craft_town
        
        for res, info in gather_info.items():
            travel = opt.dist(current, info["node"])
            total_time += travel
            current = info["node"]
            total_time += info["gathers"] * 2  # gather time
        
        # Travel back to craft town
        total_time += opt.dist(current, craft_town)
        current = craft_town
        
        # Craft (1 tick with affinity, 2 without)
        craft_time = 1 if "crafting" in opt.level["towns"].get(craft_town, {}).get("affinities", []) else 2
        total_time += craft_time
        
        # Travel to sell town
        total_time += opt.dist(current, sell_town)
        current = sell_town
        
        # Sell
        total_time += 1
        
        value_per_tick = profit / total_time if total_time > 0 else 0
        
        if value_per_tick > best_value:
            best_value = value_per_tick
            best_recipe = {
                "name": recipe_name,
                "profit": profit,
                "sell_price": best_price,
                "sell_town": sell_town,
                "craft_town": craft_town,
                "gather_info": gather_info,
                "time_per_item": total_time
            }
    
    if best_recipe is None:
        print("  No profitable recipe found, falling back to gathering")
        return solve_level1(opt)
    
    print(f"  Best recipe: {best_recipe['name']}")
    print(f"  Profit per tick: {best_value:.2f}")
    
    total_ticks = opt.level["run"]["total_ticks"]
    
    # Do batches
    while opt.sim.tick < total_ticks:
        remaining = total_ticks - opt.sim.tick
        if remaining < best_recipe["time_per_item"]:
            break
        
        # How many items can we craft?
        batch_size = min(10, int(remaining / best_recipe["time_per_item"]))
        if batch_size == 0:
            break
        
        # Gather resources
        for res, info in best_recipe["gather_info"].items():
            total_needed = info["needed"] * batch_size
            gathers = math.ceil(total_needed / info["yield"])
            
            if opt.sim.location != info["node"]:
                if not opt.travel_to(info["node"]):
                    break
            
            for _ in range(gathers):
                if not opt.do({"type": "gather"}):
                    break
        
        # Travel to craft town
        if opt.sim.location != best_recipe["craft_town"]:
            if not opt.travel_to(best_recipe["craft_town"]):
                break
        
        # Craft
        if not opt.do({"type": "craft", "item": best_recipe["name"], "quantity": batch_size}):
            break
        
        # Travel to sell town
        if opt.sim.location != best_recipe["sell_town"]:
            if not opt.travel_to(best_recipe["sell_town"]):
                break
        
        # Sell
        if not opt.do({"type": "sell", "item": best_recipe["name"], "quantity": batch_size}):
            break
        
        if len(opt.actions) % 1000 == 0:
            print(f"    Progress: {opt.sim.tick}/{total_ticks} ticks, {len(opt.actions)} actions")
    
    return opt.actions

def solve_level3(opt):
    """Level 3: Tools, fast routes, and mining."""
    print("  Phase 1: Tool Rush (if tools available)")
    
    # Get ore if available
    if "tools" in opt.unlocked:
        ore_node = None
        for node_name, node_data in opt.level["nodes"].items():
            if node_data["resource"] == "ore":
                ore_node = node_name
                break
        
        if ore_node:
            # Find crafting town
            craft_town = None
            for town, data in opt.level["towns"].items():
                if "crafting" in data.get("affinities", []):
                    craft_town = town
                    break
            if craft_town is None:
                craft_town = list(opt.level["towns"].keys())[0]
            
            # Gather ore
            if opt.travel_to(ore_node):
                # Need 4 ore for 2 iron-fittings
                node_yield = opt.level["nodes"][ore_node]["yield"]
                gathers_needed = math.ceil(4 / node_yield)
                for _ in range(gathers_needed):
                    if not opt.do({"type": "gather"}):
                        break
                
                # Travel to craft town
                if opt.travel_to(craft_town):
                    opt.do({"type": "craft", "item": "iron-fittings", "quantity": 2})
                    opt.do({"type": "craft", "item": "rope", "quantity": 2})
                    opt.do({"type": "craft", "item": "planks", "quantity": 2})
                    opt.do({"type": "craft", "item": "boots", "quantity": 1})
                    opt.do({"type": "craft", "item": "pickaxe", "quantity": 1})
                    print("  Tools crafted!")
    
    # Now do crafting
    print("  Phase 2: Crafting Loop")
    return solve_level2(opt)

def solve_level4(opt):
    """Level 4: All features unlocked."""
    print("  Phase 1: Tool Rush")
    
    # Get tools
    if "tools" in opt.unlocked:
        ore_node = None
        for node_name, node_data in opt.level["nodes"].items():
            if node_data["resource"] == "ore":
                ore_node = node_name
                break
        
        if ore_node:
            craft_town = None
            for town, data in opt.level["towns"].items():
                if "crafting" in data.get("affinities", []):
                    craft_town = town
                    break
            if craft_town is None:
                craft_town = list(opt.level["towns"].keys())[0]
            
            if opt.travel_to(ore_node):
                node_yield = opt.level["nodes"][ore_node]["yield"]
                gathers_needed = math.ceil(4 / node_yield)
                for _ in range(gathers_needed):
                    if not opt.do({"type": "gather"}):
                        break
                
                if opt.travel_to(craft_town):
                    opt.do({"type": "craft", "item": "iron-fittings", "quantity": 2})
                    opt.do({"type": "craft", "item": "rope", "quantity": 2})
                    opt.do({"type": "craft", "item": "planks", "quantity": 2})
                    opt.do({"type": "craft", "item": "boots", "quantity": 1})
                    opt.do({"type": "craft", "item": "pickaxe", "quantity": 1})
                    print("  Tools crafted!")
    
    print("  Phase 2: Build Production Upgrades")
    
    # Build upgrades
    T = opt.level["run"]["total_ticks"]
    for town, town_data in opt.level["towns"].items():
        for upgrade_name, upgrade_data in opt.const["upgrades"]["production"].items():
            if upgrade_data.get("min_level", 1) > opt.level_num:
                continue
            if upgrade_name in opt.town_upgrades[town]["production"]:
                continue
            
            # Check if worth it
            resource = upgrade_data.get("effect", {}).get("resource")
            if resource is None:
                continue
            
            base_prod = town_data["production"]["resources"].get(resource, 0)
            if base_prod == 0:
                continue
            
            sell_price = opt.const["resources"].get(resource, {}).get("sell_price", 0)
            if sell_price == 0:
                continue
            
            # Value per tick
            rate = town_data["production"]["rate"]
            value_per_tick = (base_prod * sell_price) / rate
            remaining = T - opt.sim.tick
            total_value = value_per_tick * remaining * 0.5  # Half remaining (conservative)
            
            # Cost
            enteloot_cost = upgrade_data["enteloot_cost"]
            component_cost = 0
            for comp, amt in upgrade_data.get("components", {}).items():
                # Rough estimate
                if comp in opt.const.get("components", {}):
                    comp_data = opt.const["components"][comp]
                    for res, r_amt in comp_data.get("inputs", {}).items():
                        component_cost += opt.const["resources"].get(res, {}).get("buy_price", 0) * r_amt * amt
                elif comp in opt.const.get("recipes", {}):
                    recipe = opt.const["recipes"][comp]
                    for res, r_amt in recipe.get("inputs", {}).items():
                        component_cost += opt.const["resources"].get(res, {}).get("buy_price", 0) * r_amt * amt
            
            total_cost = enteloot_cost + component_cost
            
            if total_value > total_cost * 1.2 and opt.sim.current_enteloot() > total_cost * 0.8:
                if opt.travel_to(town):
                    # Craft components
                    for comp, amt in upgrade_data.get("components", {}).items():
                        # Need to craft components
                        if comp in opt.const.get("components", {}):
                            # Find what we need
                            comp_data = opt.const["components"][comp]
                            for res, r_amt in comp_data.get("inputs", {}).items():
                                if res in opt.const["resources"]:
                                    # Gather if needed
                                    pass  # Simplified
                        opt.do({"type": "craft", "item": comp, "quantity": amt})
                    
                    if opt.do({"type": "build", "upgrade": upgrade_name}):
                        print(f"    Built {upgrade_name} in {town}")
    
    print("  Phase 3: Crafting Loop")
    actions = solve_level2(opt)
    
    print("  Phase 4: Upkeep")
    if "upkeep" in opt.unlocked:
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
        
        if best_town and opt.travel_to(best_town):
            # Do upkeep a few times
            for _ in range(3):
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
    print(f"Solving Level {level_num}")
    print(f"{'='*60}")
    
    opt = LevelAwareSolver(level, const, level_num)
    
    if level_num == 1:
        actions = solve_level1(opt)
    elif level_num == 2:
        actions = solve_level2(opt)
    elif level_num == 3:
        actions = solve_level3(opt)
    elif level_num == 4:
        actions = solve_level4(opt)
    else:
        print(f"Unknown level {level_num}")
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
    with open(f"fixed_level{level_num}.txt", "w") as f:
        json.dump({"actions": actions}, f)
    print(f"  Saved to fixed_level{level_num}.txt")
    
    return actions

if __name__ == "__main__":
    import heapq  # For Dijkstra
    level_num = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    solve_level(level_num)