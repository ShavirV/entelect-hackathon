import json
import sys
import time
import random
from collections import defaultdict
from Simulator import Simulator
import itertools

class StrategyTester:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.results = []
        
    def run_strategy(self, name, action_generator, max_ticks=None):
        """Run a strategy and return its performance metrics."""
        sim = Simulator(self.level, self.const, level_number=self.level_num)
        
        # Generate actions
        actions = action_generator(self.level, self.const, sim)
        
        # Run simulation
        start_time = time.time()
        log = sim.run(actions)
        elapsed = time.time() - start_time
        
        # Analyze results
        invalid = [e for e in log if not e.valid]
        summary = sim.summary()
        
        result = {
            "name": name,
            "actions": len(actions),
            "invalid_actions": len(invalid),
            "valid_actions": len(actions) - len(invalid),
            "final_tick": summary["final_tick"],
            "final_enteloot": summary["final_enteloot"],
            "held_value": summary["held_value"],
            "items_sold": summary["items_sold_count"],
            "multiplier": summary["mult"],
            "upgrades_built": summary["upgrades_built"],
            "estimated_score": summary["estimated_score"],
            "time_seconds": elapsed,
            "success": len(invalid) == 0
        }
        
        self.results.append(result)
        return result
    
    def print_results(self, sort_by="estimated_score"):
        """Print all results sorted by a specific metric."""
        print("\n" + "=" * 100)
        print(f"STRATEGY TEST RESULTS (sorted by {sort_by})")
        print("=" * 100)
        
        # Sort results
        sorted_results = sorted(self.results, key=lambda x: x.get(sort_by, 0), reverse=True)
        
        # Print header
        print(f"{'Rank':<5} {'Strategy':<30} {'Score':<12} {'Enteloot':<12} {'Held':<10} {'Ticks':<8} {'Invalid':<8} {'Success':<8}")
        print("-" * 100)
        
        for i, result in enumerate(sorted_results, 1):
            status = "✅" if result["success"] else "❌"
            print(f"{i:<5} {result['name'][:29]:<30} {result['estimated_score']:<12.0f} {result['final_enteloot']:<12.2f} {result['held_value']:<10.2f} {result['final_tick']:<8} {result['invalid_actions']:<8} {status:<8}")
        
        print("-" * 100)
        print(f"\nTotal strategies tested: {len(self.results)}")
        
        # Best strategy summary
        best = sorted_results[0]
        print(f"\n🏆 BEST STRATEGY: {best['name']}")
        print(f"   Score: {best['estimated_score']:.0f}")
        print(f"   Enteloot: {best['final_enteloot']:.0f}")
        print(f"   Held Value: {best['held_value']:.0f}")
        print(f"   Upgrades Built: {best['upgrades_built']}")
        print(f"   Items Sold: {best['items_sold']}")
        print(f"   Invalid Actions: {best['invalid_actions']}")

# ============================================================
# STRATEGY GENERATORS - Each returns a list of actions
# ============================================================

def strategy_basic_gather_sell(level, const, sim):
    """Simple strategy: gather wheat, sell it."""
    actions = []
    
    # Find wheat node nearest to start
    start = level["run"]["starting_town"]
    wheat_nodes = [n for n, data in level["nodes"].items() if data["resource"] == "wheat"]
    
    if not wheat_nodes:
        return []
    
    # Use the first wheat node
    target_node = wheat_nodes[0]
    
    # Travel to node
    actions.append({"type": "travel", "destination": target_node})
    
    # Gather as much as possible
    max_gathers = 10
    for _ in range(max_gathers):
        actions.append({"type": "gather"})
    
    # Find town with best wheat sell price
    sell_town = start
    best_price = 0
    for town, data in level["towns"].items():
        if "wheat" in const["resources"]:
            price = const["resources"]["wheat"].get("sell_price", 0)
            if price > best_price:
                best_price = price
                sell_town = town
    
    # Travel to town
    if sell_town != sim.location:
        actions.append({"type": "travel", "destination": sell_town})
    
    # Sell wheat
    actions.append({"type": "sell", "item": "wheat", "quantity": max_gathers * 5})  # 5 wheat per gather
    
    return actions

def strategy_craft_single_recipe(level, const, sim, recipe_name="bread"):
    """Focus on crafting a single recipe repeatedly."""
    actions = []
    
    recipe = const["recipes"].get(recipe_name)
    if not recipe:
        return []
    
    # Find the resource needed
    resources_needed = list(recipe["inputs"].keys())
    if not resources_needed:
        return []
    
    resource = resources_needed[0]
    
    # Find node for this resource
    nodes = [n for n, data in level["nodes"].items() if data["resource"] == resource]
    if not nodes:
        return []
    
    target_node = nodes[0]
    
    # Find town with crafting affinity for this recipe
    craft_town = sim.location
    for town, data in level["towns"].items():
        if "crafting" in data.get("affinities", []):
            craft_town = town
            break
    
    # Find town with best sell price for this recipe
    sell_town = craft_town
    best_price = 0
    for town, data in level["towns"].items():
        price = data.get("item-rates", {}).get(recipe_name, 0)
        if price > best_price:
            best_price = price
            sell_town = town
    
    # Generate actions for a few cycles
    num_cycles = 5
    need_per_item = recipe["inputs"][resource]
    node_yield = level["nodes"][target_node]["yield"]
    gather_time = level["nodes"][target_node].get("gather-time", 2)
    
    for cycle in range(num_cycles):
        # Travel to node if not there
        if sim.location != target_node:
            actions.append({"type": "travel", "destination": target_node})
        
        # Gather enough for 2 items
        items_to_craft = 2
        needed = items_to_craft * need_per_item
        gathers_needed = -(-needed // node_yield)  # Ceiling division
        
        for _ in range(gathers_needed):
            actions.append({"type": "gather"})
        
        # Travel to craft town if needed
        if sim.location != craft_town:
            actions.append({"type": "travel", "destination": craft_town})
        
        # Craft items
        actions.append({"type": "craft", "item": recipe_name, "quantity": items_to_craft})
        
        # Travel to sell town if different
        if sell_town != craft_town and sim.location != sell_town:
            actions.append({"type": "travel", "destination": sell_town})
        
        # Sell items
        actions.append({"type": "sell", "item": recipe_name, "quantity": items_to_craft})
    
    return actions

def strategy_aggressive_crafting(level, const, sim):
    """Aggressive strategy: craft and sell the most profitable item repeatedly."""
    actions = []
    
    # Find the most profitable recipe
    best_recipe = None
    best_margin = 0
    
    for recipe_name, recipe_data in const["recipes"].items():
        if not recipe_data.get("sellable", False):
            continue
        
        # Calculate average sell price across towns
        total_price = 0
        count = 0
        for town, data in level["towns"].items():
            price = data.get("item-rates", {}).get(recipe_name, 0)
            if price > 0:
                total_price += price
                count += 1
        
        if count == 0:
            continue
        
        avg_price = total_price / count
        
        # Calculate cost of inputs
        input_cost = 0
        for res, amt in recipe_data["inputs"].items():
            input_cost += const["resources"][res]["buy_price"] * amt
        
        margin = avg_price - input_cost
        
        if margin > best_margin:
            best_margin = margin
            best_recipe = recipe_name
    
    if not best_recipe:
        return strategy_basic_gather_sell(level, const, sim)
    
    # Use the same approach as strategy_craft_single_recipe but with the best recipe
    return strategy_craft_single_recipe(level, const, sim, best_recipe)

def strategy_build_upgrades(level, const, sim):
    """Focus on building production upgrades early."""
    actions = []
    
    # Find production upgrades we can build
    production_upgrades = const["upgrades"]["production"]
    
    for town, town_data in level["towns"].items():
        # Travel to town
        if sim.location != town:
            actions.append({"type": "travel", "destination": town})
        
        # Try to build each production upgrade
        for upgrade_name, upgrade_data in production_upgrades.items():
            # Check if already built
            if upgrade_name in town_data.get("upgrades", []):
                continue
            
            # Check level requirement
            if upgrade_data.get("min_level", 1) > sim.level:
                continue
            
            # Check if we have components (simplified - just craft them)
            components = upgrade_data.get("components", {})
            for comp_name, comp_amt in components.items():
                # Craft components
                actions.append({"type": "craft", "item": comp_name, "quantity": comp_amt})
            
            # Build the upgrade
            actions.append({"type": "build", "upgrade": upgrade_name})
    
    return actions

def strategy_fast_routes_only(level, const, sim):
    """Strategy that uses fast routes where available."""
    actions = []
    
    # Find a fast route to use
    fast_routes = [r for r in level["routes"] if r.get("toll", 0) > 0]
    
    if not fast_routes:
        return strategy_basic_gather_sell(level, const, sim)
    
    # Use the first fast route
    route = fast_routes[0]
    a, b = route["between"]
    
    # Start at the first town
    start = level["run"]["starting_town"]
    
    # Travel to one endpoint
    if start != a:
        actions.append({"type": "travel", "destination": a})
    
    # Use fast route
    actions.append({"type": "travel", "destination": b, "fast": True})
    
    # Then do some gathering/crafting
    actions.extend(strategy_basic_gather_sell(level, const, sim))
    
    return actions

def strategy_tool_rush(level, const, sim):
    """Rush to craft tools as early as possible."""
    actions = []
    
    # Find ore nodes
    ore_nodes = [n for n, data in level["nodes"].items() if data["resource"] == "ore"]
    if not ore_nodes:
        return strategy_basic_gather_sell(level, const, sim)
    
    target_node = ore_nodes[0]
    
    # Find a town with crafting affinity
    craft_town = sim.location
    for town, data in level["towns"].items():
        if "crafting" in data.get("affinities", []):
            craft_town = town
            break
    
    # Travel to ore node
    if sim.location != target_node:
        actions.append({"type": "travel", "destination": target_node})
    
    # Gather ore (need 4 ore for 2 iron-fittings)
    ore_needed = 4
    node_yield = level["nodes"][target_node]["yield"]
    gathers_needed = -(-ore_needed // node_yield)
    
    for _ in range(gathers_needed):
        actions.append({"type": "gather"})
    
    # Travel to town to craft
    if sim.location != craft_town:
        actions.append({"type": "travel", "destination": craft_town})
    
    # Craft iron-fittings, rope, planks
    actions.append({"type": "craft", "item": "iron-fittings", "quantity": 2})
    
    # Need rope and planks for tools
    actions.append({"type": "craft", "item": "rope", "quantity": 2})
    actions.append({"type": "craft", "item": "planks", "quantity": 2})
    
    # Craft tools
    actions.append({"type": "craft", "item": "boots", "quantity": 1})
    actions.append({"type": "craft", "item": "pickaxe", "quantity": 1})
    
    return actions

def strategy_balanced(level, const, sim):
    """Balanced strategy: gather, craft, build upgrades."""
    actions = []
    
    # Phase 1: Get tools if available
    if sim.level >= 3:
        ore_nodes = [n for n, data in level["nodes"].items() if data["resource"] == "ore"]
        if ore_nodes:
            # Rush tools first
            actions.extend(strategy_tool_rush(level, const, sim))
    
    # Phase 2: Find best recipe and grind it
    best_recipe = None
    best_score = 0
    
    for recipe_name, recipe_data in const["recipes"].items():
        if not recipe_data.get("sellable", False):
            continue
        
        # Simple heuristic: average sell price / input cost
        total_price = 0
        count = 0
        for town_data in level["towns"].values():
            price = town_data.get("item-rates", {}).get(recipe_name, 0)
            if price > 0:
                total_price += price
                count += 1
        
        if count == 0:
            continue
        
        avg_price = total_price / count
        input_cost = sum(const["resources"][res]["buy_price"] * amt 
                       for res, amt in recipe_data["inputs"].items())
        
        score = avg_price / input_cost if input_cost > 0 else 0
        if score > best_score:
            best_score = score
            best_recipe = recipe_name
    
    # Grind the best recipe
    if best_recipe:
        actions.extend(strategy_craft_single_recipe(level, const, sim, best_recipe))
    
    # Phase 3: Build upgrades if profitable
    actions.extend(strategy_build_upgrades(level, const, sim))
    
    return actions

# ============================================================
# TESTING FUNCTIONS
# ============================================================

def run_all_strategies(level, const, level_num):
    """Run all strategies and compare results."""
    tester = StrategyTester(level, const, level_num)
    
    strategies = [
        ("Basic Gather/Sell", strategy_basic_gather_sell),
        ("Bread Crafting", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"bread")),
        ("Fish-n-Chips Crafting", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"fish-n-chips")),
        ("Stew Crafting", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"stew")),
        ("Wooden Crafts", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"wooden-crafts")),
        ("Furniture", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"furniture")),
        ("Stone Works", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"stone-works")),
        ("Roof Tiles", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"roof-tiles")),
        ("Wool Garments", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"wool-garments")),
        ("Pottery", lambda l,c,s: strategy_craft_single_recipe(l,c,s,"pottery")),
        ("Aggressive Crafting", strategy_aggressive_crafting),
        ("Build Upgrades", strategy_build_upgrades),
        ("Fast Routes", strategy_fast_routes_only),
        ("Tool Rush", strategy_tool_rush),
        ("Balanced Strategy", strategy_balanced),
    ]
    
    print(f"\nTesting {len(strategies)} strategies...")
    print("-" * 60)
    
    for name, strategy_func in strategies:
        print(f"Running: {name}...", end=" ", flush=True)
        try:
            result = tester.run_strategy(name, strategy_func)
            status = "✅" if result["success"] else "❌"
            print(f"{status} Score: {result['estimated_score']:.0f} ({result['actions']} actions, {result['invalid_actions']} invalid)")
        except Exception as e:
            print(f"❌ ERROR: {str(e)[:50]}")
    
    # Print results
    tester.print_results("estimated_score")
    
    return tester.results

def run_random_strategies(level, const, level_num, num_random=20):
    """Run random strategies to explore the action space."""
    tester = StrategyTester(level, const, level_num)
    
    # Define possible actions
    towns = list(level["towns"].keys())
    nodes = list(level["nodes"].keys())
    recipes = list(const["recipes"].keys())
    resources = list(const["resources"].keys())
    
    def random_action_generator(level, const, sim):
        actions = []
        
        # Generate random number of actions
        num_actions = random.randint(5, 30)
        
        for _ in range(num_actions):
            action_type = random.choice(["travel", "gather", "craft", "sell", "build"])
            
            if action_type == "travel":
                # Pick a random location
                location = random.choice(towns + nodes)
                # 20% chance of fast travel if available
                fast = random.random() < 0.2 and sim.level >= 3
                action = {"type": "travel", "destination": location}
                if fast:
                    action["fast"] = True
                actions.append(action)
            
            elif action_type == "gather":
                # If at a node, gather
                if sim.location in nodes:
                    actions.append({"type": "gather"})
                else:
                    # Pick a random node and travel there first
                    node = random.choice(nodes)
                    actions.append({"type": "travel", "destination": node})
                    actions.append({"type": "gather"})
            
            elif action_type == "craft":
                # Pick a random recipe or component
                craftable = list(recipes) + list(const.get("components", {}).keys())
                if craftable:
                    item = random.choice(craftable)
                    qty = random.randint(1, 3)
                    actions.append({"type": "craft", "item": item, "quantity": qty})
            
            elif action_type == "sell":
                # Sell something we might have
                item = random.choice(resources + list(recipes))
                qty = random.randint(1, 5)
                actions.append({"type": "sell", "item": item, "quantity": qty})
            
            elif action_type == "build":
                # Build a random upgrade
                all_upgrades = (list(const.get("upgrades", {}).get("production", {}).keys()) + 
                              list(const.get("upgrades", {}).get("civic", {}).keys()))
                if all_upgrades:
                    upgrade = random.choice(all_upgrades)
                    actions.append({"type": "build", "upgrade": upgrade})
        
        return actions
    
    print(f"\nTesting {num_random} random strategies...")
    print("-" * 60)
    
    for i in range(num_random):
        name = f"Random-{i+1}"
        print(f"Running: {name}...", end=" ", flush=True)
        try:
            result = tester.run_strategy(name, random_action_generator)
            status = "✅" if result["success"] else "❌"
            print(f"{status} Score: {result['estimated_score']:.0f} ({result['actions']} actions, {result['invalid_actions']} invalid)")
        except Exception as e:
            print(f"❌ ERROR: {str(e)[:50]}")
    
    return tester.results

def test_fixed_strategies(level, const, level_num):
    """Test specific fixed strategies to find the best approach."""
    tester = StrategyTester(level, const, level_num)
    
    # Modified strategies with specific parameters
    def strategy_craft_with_affinity(level, const, sim, recipe_name):
        """Craft using towns with crafting affinity only."""
        actions = []
        
        recipe = const["recipes"].get(recipe_name)
        if not recipe:
            return []
        
        # Find crafting affinity towns
        affinity_towns = [t for t, data in level["towns"].items() 
                        if "crafting" in data.get("affinities", [])]
        
        if not affinity_towns:
            return strategy_craft_single_recipe(level, const, sim, recipe_name)
        
        craft_town = affinity_towns[0]
        
        # Find best sell town
        sell_town = craft_town
        best_price = 0
        for town, data in level["towns"].items():
            price = data.get("item-rates", {}).get(recipe_name, 0)
            if price > best_price:
                best_price = price
                sell_town = town
        
        # Find resource node
        resource = list(recipe["inputs"].keys())[0]
        nodes = [n for n, data in level["nodes"].items() if data["resource"] == resource]
        if not nodes:
            return []
        
        target_node = nodes[0]
        
        # Generate actions
        num_cycles = 3
        need_per_item = recipe["inputs"][resource]
        node_yield = level["nodes"][target_node]["yield"]
        
        for cycle in range(num_cycles):
            if sim.location != target_node:
                actions.append({"type": "travel", "destination": target_node})
            
            items_to_craft = 2
            needed = items_to_craft * need_per_item
            gathers_needed = -(-needed // node_yield)
            
            for _ in range(gathers_needed):
                actions.append({"type": "gather"})
            
            if sim.location != craft_town:
                actions.append({"type": "travel", "destination": craft_town})
            
            actions.append({"type": "craft", "item": recipe_name, "quantity": items_to_craft})
            
            if sell_town != craft_town and sim.location != sell_town:
                actions.append({"type": "travel", "destination": sell_town})
            
            actions.append({"type": "sell", "item": recipe_name, "quantity": items_to_craft})
        
        return actions
    
    # Test each recipe with affinity towns
    for recipe_name in const["recipes"].keys():
        if const["recipes"][recipe_name].get("sellable", False):
            name = f"Affinity-{recipe_name}"
            tester.run_strategy(
                name, 
                lambda l,c,s, r=recipe_name: strategy_craft_with_affinity(l,c,s,r)
            )
    
    return tester.results

# ============================================================
# MAIN EXECUTION
# ============================================================

if __name__ == "__main__":
    lvl_num = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    
    print(f"Loading Level {lvl_num}...")
    with open(f"Level{lvl_num}.json") as f:
        level = json.load(f)
    with open("constants.json") as f:
        const = json.load(f)
    
    print(f"Loaded: {len(level['towns'])} towns, {len(level['nodes'])} nodes")
    print(f"Total ticks: {level['run']['total_ticks']}")
    print(f"Starting town: {level['run']['starting_town']}")
    print(f"Starting enteloot: {level['run']['starting_enteloot']}")
    
    # Run all strategies
    all_results = []
    
    # 1. Run predefined strategies
    results = run_all_strategies(level, const, lvl_num)
    all_results.extend(results)
    
    # 2. Test fixed affinity strategies
    affinity_results = test_fixed_strategies(level, const, lvl_num)
    all_results.extend(affinity_results)
    
    # 3. Run some random strategies (fewer for large levels)
    if lvl_num <= 2:
        random_results = run_random_strategies(level, const, lvl_num, 30)
        all_results.extend(random_results)
    else:
        random_results = run_random_strategies(level, const, lvl_num, 10)
        all_results.extend(random_results)
    
    # Final summary
    print("\n" + "=" * 100)
    print("FINAL SUMMARY - ALL STRATEGIES")
    print("=" * 100)
    
    # Sort all results by score
    sorted_all = sorted(all_results, key=lambda x: x["estimated_score"], reverse=True)
    
    print(f"{'Rank':<5} {'Strategy':<35} {'Score':<15} {'Enteloot':<12} {'Upgrades':<10} {'Valid/Actions':<12}")
    print("-" * 100)
    
    for i, result in enumerate(sorted_all[:20], 1):
        status = "✅" if result["success"] else "❌"
        valid_ratio = f"{result['valid_actions']}/{result['actions']}"
        print(f"{i:<5} {result['name'][:34]:<35} {result['estimated_score']:<15.0f} {result['final_enteloot']:<12.0f} {result['upgrades_built']:<10} {valid_ratio:<12} {status}")
    
    # Save results to file
    with open(f"strategy_results_level{lvl_num}.json", "w") as f:
        json.dump(sorted_all, f, indent=2)
    print(f"\nResults saved to strategy_results_level{lvl_num}.json")
    
    # Best strategy recommendation
    best = sorted_all[0]
    print("\n" + "=" * 100)
    print(f"🏆 RECOMMENDED STRATEGY: {best['name']}")
    print("=" * 100)
    print(f"Estimated Score: {best['estimated_score']:.0f}")
    print(f"Final Enteloot: {best['final_enteloot']:.0f}")
    print(f"Held Value: {best['held_value']:.0f}")
    print(f"Upgrades Built: {best['upgrades_built']}")
    print(f"Items Sold: {best['items_sold']}")
    print(f"Invalid Actions: {best['invalid_actions']} / {best['actions']}")
    print(f"Final Tick: {best['final_tick']}")
    print(f"Time: {best['time_seconds']:.2f}s")
    
    # Second best as alternative
    if len(sorted_all) > 1:
        second = sorted_all[1]
        print(f"\n🥈 Alternative Strategy: {second['name']}")
        print(f"   Score: {second['estimated_score']:.0f}")