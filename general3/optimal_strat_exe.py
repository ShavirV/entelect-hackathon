import json
import sys
from Simulator import Simulator

class OptimalStrategyExecutor:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.sim = Simulator(level, const, level_number=level_num)
        self.actions = []
        
        # Get unlocked actions
        self.unlocked = set()
        for lvl, toks in const["level_unlocks"].items():
            if int(lvl) <= level_num:
                self.unlocked.update(toks)
    
    def do(self, action):
        """Execute a single action with validation."""
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
        return True
    
    def travel_to(self, destination, fast=False):
        """Travel to a destination."""
        if self.sim.location == destination:
            return True
        return self.do({"type": "travel", "destination": destination, "fast": fast})
    
    def gather(self, count=1):
        """Gather multiple times."""
        for _ in range(count):
            if not self.do({"type": "gather"}):
                return False
        return True
    
    def craft(self, item, quantity):
        """Craft an item."""
        return self.do({"type": "craft", "item": item, "quantity": quantity})
    
    def sell(self, item, quantity):
        """Sell an item."""
        return self.do({"type": "sell", "item": item, "quantity": quantity})
    
    def buy(self, item, quantity):
        """Buy an item."""
        return self.do({"type": "buy", "item": item, "quantity": quantity})
    
    def build(self, upgrade):
        """Build an upgrade."""
        return self.do({"type": "build", "upgrade": upgrade})
    
    def upkeep(self):
        """Perform upkeep."""
        return self.do({"type": "upkeep"})
    
    def get_nearest_node(self, resource):
        """Find nearest node for a resource."""
        current = self.sim.location
        best_node = None
        best_dist = float('inf')
        
        # Build adjacency for pathfinding
        adj = {}
        for route in self.level["routes"]:
            a, b = route["between"]
            w = route["weight"]
            if a not in adj:
                adj[a] = {}
            if b not in adj:
                adj[b] = {}
            adj[a][b] = w
            adj[b][a] = w
        
        # Simple BFS
        for node_name, node_data in self.level["nodes"].items():
            if node_data["resource"] != resource:
                continue
            # Simple distance (just use direct route if exists)
            if node_name in adj.get(current, {}):
                dist = adj[current][node_name]
            else:
                dist = 999  # Far
            if dist < best_dist:
                best_dist = dist
                best_node = node_name
        
        return best_node
    
    def get_best_sell_town(self, item):
        """Find the town with the best sell price for an item."""
        best_town = None
        best_price = 0
        
        for town, town_data in self.level["towns"].items():
            if item in self.const["resources"]:
                price = self.const["resources"][item].get("sell_price", 0)
            else:
                price = town_data.get("item-rates", {}).get(item, 0)
            
            if price > best_price:
                best_price = price
                best_town = town
        
        return best_town, best_price
    
    def get_crafting_town(self):
        """Find a town with crafting affinity."""
        for town, data in self.level["towns"].items():
            if "crafting" in data.get("affinities", []):
                return town
        return list(self.level["towns"].keys())[0]

# ============================================================
# LEVEL-SPECIFIC OPTIMAL STRATEGIES
# ============================================================

def execute_level1_optimal(executor):
    """Execute the optimal Level 1 strategy: Multi-node gathering."""
    print("Executing Level 1 Optimal Strategy: Multi-Node Gathering")
    
    # Strategy: Gather from N1 (sheep), N3 (fish), N7 (stone)
    # Based on the optimal results
    
    # Node 1: N1 - Sheep (yield 8, sell 5)
    if executor.travel_to("N1"):
        executor.gather(8)  # 8 gathers * 8 yield = 64 sheep
    
    # Node 2: N3 - Fish (yield 7, sell 4)
    if executor.travel_to("N3"):
        executor.gather(8)  # 8 gathers * 7 yield = 56 fish
    
    # Node 3: N7 - Stone (yield 8, sell 3)
    if executor.travel_to("N7"):
        executor.gather(8)  # 8 gathers * 8 yield = 64 stone
    
    # Return to Demacia to sell
    executor.travel_to("Demacia")
    executor.sell("sheep", 64)
    executor.sell("fish", 56)
    executor.sell("stone", 64)
    
    return executor.actions

def execute_level2_optimal(executor):
    """Execute the optimal Level 2 strategy: Stew crafting."""
    print("Executing Level 2 Optimal Strategy: Stew Crafting")
    
    # Need: 1 sheep + 1 fish + 1 wheat per stew
    # Best node for sheep: N14 (yield 4, sell 5)
    # Best node for fish: N1 (yield 4, sell 4) or N3 (yield 5)
    # Best node for wheat: N5 (yield 6, sell 2) or N6 (yield 4)
    
    # Find crafting town with affinity
    craft_town = executor.get_crafting_town()
    print(f"Using crafting town: {craft_town}")
    
    # Find sell town for stew
    sell_town, sell_price = executor.get_best_sell_town("stew")
    print(f"Best sell town for stew: {sell_town} (price: {sell_price})")
    
    # Do 5 cycles of crafting
    cycles = 5
    items_per_cycle = 2
    
    for cycle in range(cycles):
        print(f"  Cycle {cycle+1}/{cycles}")
        
        # Gather sheep from N14
        executor.travel_to("N14")
        executor.gather(2)  # 2 gathers * 4 yield = 8 sheep (enough for 2 stews)
        
        # Gather fish from N1
        executor.travel_to("N1")
        executor.gather(2)  # 2 gathers * 4 yield = 8 fish (enough for 2 stews)
        
        # Gather wheat from N5
        executor.travel_to("N5")
        executor.gather(1)  # 1 gather * 6 yield = 6 wheat (enough for 2 stews)
        
        # Travel to crafting town
        executor.travel_to(craft_town)
        
        # Craft stew
        executor.craft("stew", items_per_cycle)
        
        # Travel to sell town and sell
        if craft_town != sell_town:
            executor.travel_to(sell_town)
        executor.sell("stew", items_per_cycle)
    
    return executor.actions

def execute_level3_optimal(executor):
    """Execute the optimal Level 3 strategy: Furniture crafting with tools."""
    print("Executing Level 3 Optimal Strategy: Furniture Crafting")
    
    # Furniture needs: 3 wood + 1 sheep
    # Best node for wood: N10 (yield 8)
    # Best node for sheep: N11 (yield 4) or N3 (yield 8)
    
    # First, rush tools if possible
    if "tools" in executor.unlocked:
        print("  Phase 1: Tool Rush")
        ore_node = executor.get_nearest_node("ore")
        if ore_node:
            executor.travel_to(ore_node)
            executor.gather(2)  # 2 gathers * yield = enough for iron-fittings
            
            craft_town = executor.get_crafting_town()
            executor.travel_to(craft_town)
            
            # Craft iron-fittings, rope, planks, then tools
            executor.craft("iron-fittings", 2)
            executor.craft("rope", 2)
            executor.craft("planks", 2)
            executor.craft("boots", 1)
            executor.craft("pickaxe", 1)
    
    # Now craft furniture
    craft_town = executor.get_crafting_town()
    sell_town, sell_price = executor.get_best_sell_town("furniture")
    
    print(f"  Phase 2: Furniture Crafting")
    print(f"  Craft town: {craft_town}, Sell town: {sell_town} (price: {sell_price})")
    
    # Do 5 cycles
    cycles = 5
    items_per_cycle = 2
    
    for cycle in range(cycles):
        print(f"    Cycle {cycle+1}/{cycles}")
        
        # Gather wood from N10
        executor.travel_to("N10")
        executor.gather(1)  # 1 gather * 8 yield = 8 wood (enough for 2 furniture)
        
        # Gather sheep from N11 (4 yield) or N3 (8 yield)
        # Use N3 for better yield
        executor.travel_to("N3")
        executor.gather(1)  # 1 gather * 8 yield = 8 sheep (enough for 2 furniture)
        
        # Travel to crafting town
        executor.travel_to(craft_town)
        
        # Craft furniture
        executor.craft("furniture", items_per_cycle)
        
        # Sell
        if craft_town != sell_town:
            executor.travel_to(sell_town)
        executor.sell("furniture", items_per_cycle)
    
    return executor.actions

def execute_level4_optimal(executor):
    """Execute the optimal Level 4 strategy: Furniture crafting with upgrades."""
    print("Executing Level 4 Optimal Strategy: Furniture Crafting with Upgrades")
    
    # Same as Level 3 but with more cycles and upgrades
    # First, rush tools
    if "tools" in executor.unlocked:
        print("  Phase 1: Tool Rush")
        ore_node = executor.get_nearest_node("ore")
        if ore_node:
            executor.travel_to(ore_node)
            executor.gather(2)
            
            craft_town = executor.get_crafting_town()
            executor.travel_to(craft_town)
            
            executor.craft("iron-fittings", 2)
            executor.craft("rope", 2)
            executor.craft("planks", 2)
            executor.craft("boots", 1)
            executor.craft("pickaxe", 1)
    
    # Build production upgrades if possible
    if "build" in executor.unlocked:
        print("  Phase 2: Building Production Upgrades")
        # Build woodlands and farmhouse for better production
        # But only if we can afford them and they're worth it
        
        # For Level 4, build in a town with good resources
        # Build woodlands in a town that produces wood
        # Build farmhouse in a town that produces sheep
        
        # Check if we can build
        try:
            executor.travel_to("Freljord")  # Has wood production
            # Need components for woodlands: 2 fencing + 2 rope
            executor.craft("fencing", 2)
            executor.craft("rope", 2)
            executor.build("woodlands")
        except:
            print("  Could not build woodlands upgrade")
        
        try:
            executor.travel_to("Targon")  # Has sheep production
            # Need components for farmhouse: 3 planks + 2 thatch
            executor.craft("planks", 3)
            executor.craft("thatch", 2)
            executor.build("farmhouse")
        except:
            print("  Could not build farmhouse upgrade")
    
    # Now craft furniture
    craft_town = executor.get_crafting_town()
    sell_town, sell_price = executor.get_best_sell_town("furniture")
    
    print(f"  Phase 3: Furniture Crafting")
    
    # Do more cycles
    cycles = 10
    items_per_cycle = 2
    
    for cycle in range(cycles):
        # Gather wood from N12 (or best wood node)
        # Find best wood node for this level
        wood_node = executor.get_nearest_node("wood")
        if wood_node:
            executor.travel_to(wood_node)
            executor.gather(1)
        
        # Gather sheep from best sheep node
        sheep_node = executor.get_nearest_node("sheep")
        if sheep_node:
            executor.travel_to(sheep_node)
            executor.gather(1)
        
        # Craft and sell
        executor.travel_to(craft_town)
        executor.craft("furniture", items_per_cycle)
        
        if craft_town != sell_town:
            executor.travel_to(sell_town)
        executor.sell("furniture", items_per_cycle)
    
    # Upkeep if unlocked
    if "upkeep" in executor.unlocked:
        print("  Phase 4: Upkeep")
        # Find best town for upkeep (high enteloot)
        best_enteloot = 0
        best_town = None
        for town, town_data in executor.level["towns"].items():
            rate = town_data["enteloot"]["rate"]
            amount = town_data["enteloot"]["amount"]
            value = amount / rate
            if value > best_enteloot:
                best_enteloot = value
                best_town = town
        
        if best_town:
            executor.travel_to(best_town)
            executor.upkeep()
    
    return executor.actions

# ============================================================
# MAIN EXECUTION
# ============================================================

def solve_with_optimal_strategy(level_num):
    """Solve a level using the optimal strategy found."""
    
    with open(f"Level{level_num}.json") as f:
        level = json.load(f)
    with open("constants.json") as f:
        const = json.load(f)
    
    print(f"\n{'='*60}")
    print(f"Solving Level {level_num} with Optimal Strategy")
    print(f"{'='*60}")
    
    executor = OptimalStrategyExecutor(level, const, level_num)
    
    # Execute the appropriate strategy
    if level_num == 1:
        actions = execute_level1_optimal(executor)
    elif level_num == 2:
        actions = execute_level2_optimal(executor)
    elif level_num == 3:
        actions = execute_level3_optimal(executor)
    elif level_num == 4:
        actions = execute_level4_optimal(executor)
    else:
        print(f"Unknown level: {level_num}")
        return []
    
    # Run the full simulation to verify
    sim = Simulator(level, const, level_number=level_num)
    log = sim.run(actions)
    
    invalid = [e for e in log if not e.valid]
    print(f"\nResults:")
    print(f"  Total actions: {len(actions)}")
    print(f"  Invalid actions: {len(invalid)}")
    print(f"  Final tick: {sim.tick}")
    
    summary = sim.summary()
    print(f"  Final Enteloot: {summary['final_enteloot']:.0f}")
    print(f"  Held Value: {summary['held_value']:.0f}")
    print(f"  Upgrades Built: {summary['upgrades_built']}")
    print(f"  Items Sold: {summary['items_sold_count']}")
    print(f"  Estimated Score: {summary['estimated_score']:.0f}")
    
    return actions

def solve_all_levels():
    """Solve all levels with optimal strategies."""
    all_actions = {}
    total_score = 0
    
    for level_num in range(1, 5):
        actions = solve_with_optimal_strategy(level_num)
        all_actions[level_num] = actions
        
        # Save actions to file
        with open(f"optimal_level{level_num}.txt", "w") as f:
            json.dump({"actions": actions}, f)
        print(f"  Saved actions to optimal_level{level_num}.txt")
    
    return all_actions

if __name__ == "__main__":
    if len(sys.argv) > 1:
        level_num = int(sys.argv[1])
        actions = solve_with_optimal_strategy(level_num)
        with open(f"optimal_level{level_num}.txt", "w") as f:
            json.dump({"actions": actions}, f)
    else:
        solve_all_levels()
    
    print("\n" + "="*60)
    print("All levels solved with optimal strategies!")
    print("="*60)