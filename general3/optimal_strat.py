import json
import sys
import time
import random
import heapq
from collections import defaultdict
from Simulator import Simulator
from copy import deepcopy

class OptimalStrategyFinder:
    def __init__(self, level, const, level_num):
        self.level = level
        self.const = const
        self.level_num = level_num
        self.sim = Simulator(level, const, level_number=level_num)
        
        # Get unlocked actions for this level
        self.unlocked = set()
        for lvl, toks in const["level_unlocks"].items():
            if int(lvl) <= level_num:
                self.unlocked.update(toks)
        
        self.best_score = -1
        self.best_actions = []
        self.results = []
        
        # Cache for path distances
        self.dist_cache = {}
        self._build_distance_cache()
    
    def _build_distance_cache(self):
        """Precompute distances between all locations."""
        locations = list(self.level["towns"].keys()) + list(self.level["nodes"].keys())
        for loc1 in locations:
            for loc2 in locations:
                if loc1 != loc2:
                    self.dist_cache[(loc1, loc2)] = self._estimate_distance(loc1, loc2)
    
    def _estimate_distance(self, loc1, loc2):
        """Estimate distance between locations (simplified)."""
        # For a real implementation, you'd use Dijkstra
        # For now, just look at routes
        for route in self.level["routes"]:
            a, b = route["between"]
            if (a == loc1 and b == loc2) or (a == loc2 and b == loc1):
                return route["weight"]
        return 10  # Default distance
    
    def _get_node_value(self, node_name):
        """Calculate the value of a node."""
        node = self.level["nodes"][node_name]
        resource = node["resource"]
        sell_price = self.const["resources"].get(resource, {}).get("sell_price", 0)
        yield_amt = node["yield"]
        gather_time = node.get("gather-time", 2)
        
        return {
            "resource": resource,
            "sell_price": sell_price,
            "yield": yield_amt,
            "gather_time": gather_time,
            "value_per_tick": (yield_amt * sell_price) / gather_time if sell_price > 0 else 0
        }
    
    def _get_recipe_value(self, recipe_name):
        """Calculate the value of a recipe."""
        recipe = self.const["recipes"].get(recipe_name)
        if not recipe or not recipe.get("sellable", False):
            return None
        
        # Average sell price across towns
        total_price = 0
        count = 0
        for town_data in self.level["towns"].values():
            price = town_data.get("item-rates", {}).get(recipe_name, 0)
            if price > 0:
                total_price += price
                count += 1
        
        if count == 0:
            return None
        
        avg_price = total_price / count
        
        # Calculate input costs
        input_cost = 0
        for res, amt in recipe["inputs"].items():
            buy_price = self.const["resources"].get(res, {}).get("buy_price", 0)
            input_cost += buy_price * amt
        
        return {
            "name": recipe_name,
            "avg_price": avg_price,
            "input_cost": input_cost,
            "profit": avg_price - input_cost,
            "profit_margin": (avg_price - input_cost) / input_cost if input_cost > 0 else 0
        }
    
    def find_best_resources(self):
        """Find the most profitable resources to gather."""
        resources = []
        for node_name, node in self.level["nodes"].items():
            value = self._get_node_value(node_name)
            if value["sell_price"] > 0:
                resources.append({
                    "node": node_name,
                    **value
                })
        
        resources.sort(key=lambda x: x["value_per_tick"], reverse=True)
        return resources
    
    def find_best_recipes(self):
        """Find the most profitable recipes."""
        recipes = []
        for recipe_name in self.const["recipes"]:
            value = self._get_recipe_value(recipe_name)
            if value and value["profit"] > 0:
                recipes.append(value)
        
        recipes.sort(key=lambda x: x["profit_margin"], reverse=True)
        return recipes
    
    def find_best_town_for_crafting(self, recipe_name):
        """Find the best town for crafting a specific recipe."""
        best_town = None
        best_value = 0
        
        for town, town_data in self.level["towns"].items():
            price = town_data.get("item-rates", {}).get(recipe_name, 0)
            has_affinity = "crafting" in town_data.get("affinities", [])
            
            # Score: price + affinity bonus
            score = price + (10 if has_affinity else 0)
            if score > best_value:
                best_value = score
                best_town = town
        
        return best_town
    
    def find_best_town_for_selling(self, item):
        """Find the best town for selling an item."""
        best_town = None
        best_price = 0
        
        for town, town_data in self.level["towns"].items():
            if item in self.const["resources"]:
                # Raw resource - fixed price
                price = self.const["resources"][item].get("sell_price", 0)
            else:
                # Crafted item
                price = town_data.get("item-rates", {}).get(item, 0)
            
            if price > best_price:
                best_price = price
                best_town = town
        
        return best_town, best_price
    
    def generate_level1_strategies(self):
        """Generate strategies for Level 1."""
        strategies = []
        
        # Strategy 1: Best single node
        best_nodes = self.find_best_resources()
        if best_nodes:
            best = best_nodes[0]
            strategies.append({
                "name": f"Best Node: {best['node']} ({best['resource']})",
                "type": "gather_sell",
                "node": best["node"],
                "resource": best["resource"],
                "gather_count": 20
            })
        
        # Strategy 2: Multiple top nodes
        top_nodes = best_nodes[:3]
        if len(top_nodes) > 1:
            strategies.append({
                "name": "Multiple Top Nodes",
                "type": "multi_gather",
                "nodes": [{"name": n["node"], "resource": n["resource"], "gather_count": 8} for n in top_nodes]
            })
        
        # Strategy 3: Buy and sell arbitrage
        buyable = []
        for res, data in self.const["resources"].items():
            buy_price = data.get("buy_price")
            sell_price = data.get("sell_price")
            if buy_price is not None and sell_price is not None and sell_price > buy_price:
                buyable.append((res, buy_price, sell_price))
        
        if buyable:
            strategies.append({
                "name": "Buy/Sell Arbitrage",
                "type": "arbitrage",
                "trades": [(res, 10) for res, _, _ in buyable]
            })
        
        return strategies
    
    def generate_level2_strategies(self):
        """Generate strategies for Level 2."""
        strategies = []
        
        # Crafting strategies
        best_recipes = self.find_best_recipes()
        if best_recipes:
            for recipe in best_recipes[:3]:
                craft_town = self.find_best_town_for_crafting(recipe["name"])
                sell_town, sell_price = self.find_best_town_for_selling(recipe["name"])
                
                strategies.append({
                    "name": f"Craft: {recipe['name']} (profit: {recipe['profit']:.0f})",
                    "type": "craft_loop",
                    "recipe": recipe["name"],
                    "craft_town": craft_town,
                    "sell_town": sell_town,
                    "cycles": 5
                })
        
        # Build upgrade strategies
        production_upgrades = self.const["upgrades"]["production"]
        for upgrade_name, upgrade_data in production_upgrades.items():
            if upgrade_data.get("min_level", 1) <= self.level_num:
                strategies.append({
                    "name": f"Build: {upgrade_name}",
                    "type": "build_upgrade",
                    "upgrade": upgrade_name,
                    "town": list(self.level["towns"].keys())[0]  # Build in first town
                })
        
        return strategies
    
    def generate_level3_strategies(self):
        """Generate strategies for Level 3."""
        strategies = []
        
        # Tool strategies
        if "tools" in self.unlocked:
            strategies.append({
                "name": "Tool Rush (Boots + Pickaxe)",
                "type": "tool_rush"
            })
        
        # Fast route strategies
        fast_routes = [r for r in self.level["routes"] if r.get("toll", 0) > 0]
        if fast_routes:
            for route in fast_routes[:2]:
                a, b = route["between"]
                strategies.append({
                    "name": f"Fast Route: {a}->{b} (toll: {route['toll']})",
                    "type": "fast_route",
                    "from": a,
                    "to": b
                })
        
        return strategies
    
    def generate_level4_strategies(self):
        """Generate strategies for Level 4."""
        strategies = []
        
        # Upkeep strategies
        if "upkeep" in self.unlocked:
            # Find towns with high enteloot
            high_enteloot_towns = []
            for town, town_data in self.level["towns"].items():
                enteloot_rate = town_data["enteloot"]["rate"]
                enteloot_amount = town_data["enteloot"]["amount"]
                value = enteloot_amount / enteloot_rate
                high_enteloot_towns.append((town, value))
            
            high_enteloot_towns.sort(key=lambda x: x[1], reverse=True)
            
            for town, _ in high_enteloot_towns[:3]:
                strategies.append({
                    "name": f"Upkeep at {town}",
                    "type": "upkeep",
                    "town": town,
                    "count": 3
                })
        
        # Civic upgrades
        civic_upgrades = self.const["upgrades"]["civic"]
        for upgrade_name, upgrade_data in civic_upgrades.items():
            if upgrade_data.get("min_level", 1) <= self.level_num:
                strategies.append({
                    "name": f"Civic: {upgrade_name}",
                    "type": "build_civic",
                    "upgrade": upgrade_name
                })
        
        return strategies
    
    def build_actions_from_strategy(self, strategy):
        """Build an action list from a strategy definition."""
        actions = []
        sim = Simulator(self.level, self.const, level_number=self.level_num)
        
        try:
            if strategy["type"] == "gather_sell":
                # Travel to node, gather, return to town, sell
                start = self.level["run"]["starting_town"]
                
                actions.append({"type": "travel", "destination": strategy["node"]})
                
                for _ in range(strategy["gather_count"]):
                    actions.append({"type": "gather"})
                
                actions.append({"type": "travel", "destination": start})
                
                total = strategy["gather_count"] * self.level["nodes"][strategy["node"]]["yield"]
                actions.append({"type": "sell", "item": strategy["resource"], "quantity": total})
            
            elif strategy["type"] == "multi_gather":
                start = self.level["run"]["starting_town"]
                
                for node_info in strategy["nodes"]:
                    actions.append({"type": "travel", "destination": node_info["name"]})
                    for _ in range(node_info["gather_count"]):
                        actions.append({"type": "gather"})
                
                actions.append({"type": "travel", "destination": start})
                
                for node_info in strategy["nodes"]:
                    total = node_info["gather_count"] * self.level["nodes"][node_info["name"]]["yield"]
                    actions.append({"type": "sell", "item": node_info["resource"], "quantity": total})
            
            elif strategy["type"] == "craft_loop":
                craft_town = strategy["craft_town"] or list(self.level["towns"].keys())[0]
                sell_town = strategy["sell_town"] or craft_town
                
                recipe = self.const["recipes"][strategy["recipe"]]
                resource = list(recipe["inputs"].keys())[0]
                need_per_item = recipe["inputs"][resource]
                
                # Find node for resource
                node = None
                for n, data in self.level["nodes"].items():
                    if data["resource"] == resource:
                        node = n
                        break
                
                if not node:
                    return []
                
                node_yield = self.level["nodes"][node]["yield"]
                
                for cycle in range(strategy["cycles"]):
                    if sim.location != node:
                        actions.append({"type": "travel", "destination": node})
                    
                    items_to_craft = 2
                    needed = items_to_craft * need_per_item
                    gathers = -(-needed // node_yield)
                    
                    for _ in range(gathers):
                        actions.append({"type": "gather"})
                    
                    if sim.location != craft_town:
                        actions.append({"type": "travel", "destination": craft_town})
                    
                    actions.append({"type": "craft", "item": strategy["recipe"], "quantity": items_to_craft})
                    
                    if sell_town != craft_town and sim.location != sell_town:
                        actions.append({"type": "travel", "destination": sell_town})
                    
                    actions.append({"type": "sell", "item": strategy["recipe"], "quantity": items_to_craft})
            
            elif strategy["type"] == "build_upgrade":
                actions.append({"type": "travel", "destination": strategy["town"]})
                upgrade_data = self.const["upgrades"]["production"][strategy["upgrade"]]
                components = upgrade_data.get("components", {})
                for comp, amt in components.items():
                    actions.append({"type": "craft", "item": comp, "quantity": amt})
                actions.append({"type": "build", "upgrade": strategy["upgrade"]})
            
            elif strategy["type"] == "tool_rush":
                # Find ore node
                ore_nodes = [n for n, data in self.level["nodes"].items() if data["resource"] == "ore"]
                if ore_nodes:
                    node = ore_nodes[0]
                    
                    # Find crafting town
                    craft_town = list(self.level["towns"].keys())[0]
                    for town, data in self.level["towns"].items():
                        if "crafting" in data.get("affinities", []):
                            craft_town = town
                            break
                    
                    actions.append({"type": "travel", "destination": node})
                    
                    # Gather ore for iron-fittings x2 (need 4 ore)
                    node_yield = self.level["nodes"][node]["yield"]
                    ore_needed = 4
                    gathers = -(-ore_needed // node_yield)
                    for _ in range(gathers):
                        actions.append({"type": "gather"})
                    
                    actions.append({"type": "travel", "destination": craft_town})
                    
                    actions.append({"type": "craft", "item": "iron-fittings", "quantity": 2})
                    actions.append({"type": "craft", "item": "rope", "quantity": 2})
                    actions.append({"type": "craft", "item": "planks", "quantity": 2})
                    actions.append({"type": "craft", "item": "boots", "quantity": 1})
                    actions.append({"type": "craft", "item": "pickaxe", "quantity": 1})
            
            elif strategy["type"] == "fast_route":
                actions.append({"type": "travel", "destination": strategy["from"]})
                actions.append({"type": "travel", "destination": strategy["to"], "fast": True})
            
            elif strategy["type"] == "upkeep":
                actions.append({"type": "travel", "destination": strategy["town"]})
                for _ in range(strategy["count"]):
                    actions.append({"type": "upkeep"})
            
            elif strategy["type"] == "build_civic":
                town = list(self.level["towns"].keys())[0]
                actions.append({"type": "travel", "destination": town})
                upgrade_data = self.const["upgrades"]["civic"][strategy["upgrade"]]
                components = upgrade_data.get("components", {})
                for comp, amt in components.items():
                    actions.append({"type": "craft", "item": comp, "quantity": amt})
                actions.append({"type": "build", "upgrade": strategy["upgrade"]})
            
            elif strategy["type"] == "arbitrage":
                start = self.level["run"]["starting_town"]
                for res, qty in strategy["trades"]:
                    # Buy at a town that sells this resource
                    buy_town = start
                    for town, data in self.level["towns"].items():
                        if res in data["production"]["resources"]:
                            buy_town = town
                            break
                    
                    if sim.location != buy_town:
                        actions.append({"type": "travel", "destination": buy_town})
                    
                    actions.append({"type": "buy", "item": res, "quantity": qty})
                    
                    if sim.location != start:
                        actions.append({"type": "travel", "destination": start})
                    
                    actions.append({"type": "sell", "item": res, "quantity": qty})
        
        except Exception as e:
            print(f"Error building actions for {strategy['name']}: {e}")
            return []
        
        return actions
    
    def evaluate_strategy(self, strategy):
        """Evaluate a strategy by running it."""
        actions = self.build_actions_from_strategy(strategy)
        
        if not actions:
            return None
        
        # Run simulation
        sim = Simulator(self.level, self.const, level_number=self.level_num)
        log = sim.run(actions)
        
        invalid = [e for e in log if not e.valid]
        summary = sim.summary()
        
        return {
            "name": strategy["name"],
            "type": strategy["type"],
            "actions": len(actions),
            "invalid": len(invalid),
            "valid": len(actions) - len(invalid),
            "final_tick": summary["final_tick"],
            "enteloot": summary["final_enteloot"],
            "held_value": summary["held_value"],
            "items_sold": summary["items_sold_count"],
            "multiplier": summary["mult"],
            "upgrades": summary["upgrades_built"],
            "score": summary["estimated_score"],
            "success": len(invalid) == 0,
            "actions_list": actions
        }
    
    def find_optimal_for_level(self):
        """Find the optimal strategy for the current level."""
        print(f"\n{'='*60}")
        print(f"Finding optimal strategy for Level {self.level_num}")
        print(f"{'='*60}")
        print(f"Unlocked actions: {sorted(self.unlocked)}")
        
        strategies = []
        
        # Generate strategies based on level
        if self.level_num == 1:
            strategies.extend(self.generate_level1_strategies())
        elif self.level_num == 2:
            strategies.extend(self.generate_level1_strategies())
            strategies.extend(self.generate_level2_strategies())
        elif self.level_num == 3:
            strategies.extend(self.generate_level1_strategies())
            strategies.extend(self.generate_level2_strategies())
            strategies.extend(self.generate_level3_strategies())
        else:  # Level 4+
            strategies.extend(self.generate_level1_strategies())
            strategies.extend(self.generate_level2_strategies())
            strategies.extend(self.generate_level3_strategies())
            strategies.extend(self.generate_level4_strategies())
        
        # Also add some random strategies for exploration
        random_strategies = self.generate_random_strategies(10)
        strategies.extend(random_strategies)
        
        # Evaluate all strategies
        results = []
        total = len(strategies)
        
        for i, strategy in enumerate(strategies):
            print(f"  [{i+1}/{total}] Testing: {strategy['name'][:50]}...", end=" ", flush=True)
            
            result = self.evaluate_strategy(strategy)
            if result:
                results.append(result)
                status = "✅" if result["success"] else f"❌ ({result['invalid']} invalid)"
                print(f"{status} Score: {result['score']:.0f}")
            else:
                print("❌ Failed to generate actions")
        
        # Sort by score
        results.sort(key=lambda x: x["score"], reverse=True)
        
        return results
    
    def generate_random_strategies(self, count):
        """Generate random strategies for exploration."""
        strategies = []
        
        for i in range(count):
            # Randomly pick a strategy type based on level
            available_types = ["gather_sell", "multi_gather"]
            
            if self.level_num >= 2:
                available_types.append("craft_loop")
                available_types.append("build_upgrade")
            
            if self.level_num >= 3:
                available_types.append("tool_rush")
                available_types.append("fast_route")
            
            if self.level_num >= 4:
                available_types.append("upkeep")
                available_types.append("build_civic")
            
            strategy_type = random.choice(available_types)
            
            if strategy_type == "gather_sell":
                nodes = list(self.level["nodes"].keys())
                if nodes:
                    node = random.choice(nodes)
                    resource = self.level["nodes"][node]["resource"]
                    strategies.append({
                        "name": f"Random Gather {i+1}",
                        "type": "gather_sell",
                        "node": node,
                        "resource": resource,
                        "gather_count": random.randint(5, 15)
                    })
            
            elif strategy_type == "craft_loop":
                recipes = list(self.const["recipes"].keys())
                if recipes:
                    recipe = random.choice(recipes)
                    if self.const["recipes"][recipe].get("sellable", False):
                        strategies.append({
                            "name": f"Random Craft {i+1}",
                            "type": "craft_loop",
                            "recipe": recipe,
                            "craft_town": random.choice(list(self.level["towns"].keys())),
                            "sell_town": random.choice(list(self.level["towns"].keys())),
                            "cycles": random.randint(2, 5)
                        })
            
            elif strategy_type == "build_upgrade":
                upgrades = list(self.const["upgrades"]["production"].keys())
                if upgrades:
                    upgrade = random.choice(upgrades)
                    strategies.append({
                        "name": f"Random Build {i+1}",
                        "type": "build_upgrade",
                        "upgrade": upgrade,
                        "town": random.choice(list(self.level["towns"].keys()))
                    })
            
            elif strategy_type == "tool_rush":
                strategies.append({
                    "name": f"Random Tool Rush {i+1}",
                    "type": "tool_rush"
                })
            
            elif strategy_type == "fast_route":
                fast_routes = [r for r in self.level["routes"] if r.get("toll", 0) > 0]
                if fast_routes:
                    route = random.choice(fast_routes)
                    a, b = route["between"]
                    strategies.append({
                        "name": f"Random Fast Route {i+1}",
                        "type": "fast_route",
                        "from": a,
                        "to": b
                    })
            
            elif strategy_type == "upkeep":
                towns = list(self.level["towns"].keys())
                if towns:
                    strategies.append({
                        "name": f"Random Upkeep {i+1}",
                        "type": "upkeep",
                        "town": random.choice(towns),
                        "count": random.randint(1, 3)
                    })
            
            elif strategy_type == "build_civic":
                civic_upgrades = list(self.const["upgrades"]["civic"].keys())
                if civic_upgrades:
                    upgrade = random.choice(civic_upgrades)
                    strategies.append({
                        "name": f"Random Civic {i+1}",
                        "type": "build_civic",
                        "upgrade": upgrade
                    })
        
        return strategies
    
    def optimize_with_beam_search(self, max_actions=50, beam_width=5):
        """Use beam search to find the optimal action sequence."""
        print(f"\nRunning beam search (max_actions={max_actions}, beam_width={beam_width})...")
        
        start = self.level["run"]["starting_town"]
        total_ticks = self.level["run"]["total_ticks"]
        
        # Define possible actions based on level
        action_templates = []
        
        # Travel to towns and nodes
        for loc in list(self.level["towns"].keys()) + list(self.level["nodes"].keys()):
            if loc != start:
                action_templates.append(("travel", {"type": "travel", "destination": loc}))
                
                # Fast travel if available
                if self.level_num >= 3:
                    for route in self.level["routes"]:
                        if route.get("toll", 0) > 0:
                            a, b = route["between"]
                            if a == loc or b == loc:
                                action_templates.append(("travel_fast", {"type": "travel", "destination": loc, "fast": True}))
        
        # Gather (if at a node)
        for node in self.level["nodes"]:
            action_templates.append(("gather", {"type": "gather", "node": node}))
        
        # Craft (if unlocked)
        if self.level_num >= 2:
            for recipe in self.const["recipes"]:
                action_templates.append(("craft", {"type": "craft", "item": recipe, "quantity": 1}))
            for comp in self.const.get("components", {}):
                action_templates.append(("craft", {"type": "craft", "item": comp, "quantity": 1}))
        
        # Sell
        for res in self.const["resources"]:
            action_templates.append(("sell", {"type": "sell", "item": res, "quantity": 1}))
        for recipe in self.const["recipes"]:
            if self.const["recipes"][recipe].get("sellable", False):
                action_templates.append(("sell", {"type": "sell", "item": recipe, "quantity": 1}))
        
        # Buy
        for res, data in self.const["resources"].items():
            if data.get("buy_price") is not None:
                action_templates.append(("buy", {"type": "buy", "item": res, "quantity": 1}))
        
        # Build (if unlocked)
        if self.level_num >= 2:
            for upgrade in self.const["upgrades"]["production"]:
                action_templates.append(("build", {"type": "build", "upgrade": upgrade}))
            if self.level_num >= 4:
                for upgrade in self.const["upgrades"]["civic"]:
                    action_templates.append(("build", {"type": "build", "upgrade": upgrade}))
        
        # Upkeep (if unlocked)
        if self.level_num >= 4:
            action_templates.append(("upkeep", {"type": "upkeep"}))
        
        # Beam search
        beam = [([], 0, 0)]  # (actions, tick, score)
        
        for step in range(max_actions):
            candidates = []
            
            for actions, tick, score in beam:
                if tick >= total_ticks:
                    candidates.append((actions, tick, score))
                    continue
                
                # Try each action template
                for action_type, template in action_templates:
                    # Quick validation
                    if action_type == "gather" and sim.location not in self.level["nodes"]:
                        continue
                    
                    # Create a new action list
                    new_actions = actions + [template]
                    
                    # Evaluate with a quick simulation
                    try:
                        test_sim = Simulator(self.level, self.const, level_number=self.level_num)
                        # We need to run from the start
                        # This is expensive, so we use a heuristic score
                        test_sim.tick = tick
                        # Copy state - simplified
                        # For beam search, we use the actual simulation
                        test_log = test_sim.run(new_actions)
                        
                        if not test_log:
                            continue
                        
                        last_entry = test_log[-1]
                        new_score = last_entry.enteloot_after if hasattr(last_entry, 'enteloot_after') else 0
                        new_tick = last_entry.tick_after if hasattr(last_entry, 'tick_after') else tick
                        
                        # Add bonus for reaching new ticks
                        new_score += (new_tick / total_ticks) * 1000
                        
                        candidates.append((new_actions, new_tick, new_score))
                    except:
                        continue
            
            if not candidates:
                break
            
            # Keep top beam_width candidates
            candidates.sort(key=lambda x: x[2], reverse=True)
            beam = candidates[:beam_width]
            
            if step % 5 == 0:
                best = beam[0]
                print(f"  Step {step}: Best score {best[2]:.0f} with {len(best[0])} actions")
        
        if beam:
            best = beam[0]
            return best[0]
        
        return []


def find_optimal_for_all_levels():
    """Find the optimal strategy for all levels."""
    results = {}
    
    for level_num in range(1, 5):
        try:
            with open(f"Level{level_num}.json") as f:
                level = json.load(f)
            with open("constants.json") as f:
                const = json.load(f)
            
            print(f"\n{'#'*60}")
            print(f"# OPTIMIZING LEVEL {level_num}")
            print(f"{'#'*60}")
            
            finder = OptimalStrategyFinder(level, const, level_num)
            level_results = finder.find_optimal_for_level()
            
            results[level_num] = {
                "results": level_results,
                "best": level_results[0] if level_results else None
            }
            
            if results[level_num]["best"]:
                best = results[level_num]["best"]
                print(f"\n🏆 LEVEL {level_num} BEST STRATEGY:")
                print(f"   Name: {best['name']}")
                print(f"   Score: {best['score']:.0f}")
                print(f"   Enteloot: {best['enteloot']:.0f}")
                print(f"   Held Value: {best['held_value']:.0f}")
                print(f"   Actions: {best['valid']}/{best['actions']} valid")
                print(f"   Upgrades: {best['upgrades']}")
                
        except Exception as e:
            print(f"Error optimizing Level {level_num}: {e}")
            results[level_num] = {"error": str(e)}
    
    return results


def analyze_best_overall(results):
    """Analyze the best overall strategy across all levels."""
    print(f"\n{'='*80}")
    print("OVERALL BEST STRATEGIES BY LEVEL")
    print(f"{'='*80}")
    
    print(f"{'Level':<8} {'Strategy':<45} {'Score':<12} {'Enteloot':<12} {'Valid/Actions':<12}")
    print("-" * 80)
    
    total_score = 0
    
    for level_num in sorted(results.keys()):
        data = results[level_num]
        if "best" in data and data["best"]:
            best = data["best"]
            print(f"{level_num:<8} {best['name'][:44]:<45} {best['score']:<12.0f} {best['enteloot']:<12.0f} {best['valid']}/{best['actions']}")
            total_score += best["score"]
        elif "error" in data:
            print(f"{level_num:<8} ERROR: {data['error']}")
        else:
            print(f"{level_num:<8} No results")
    
    print("-" * 80)
    print(f"TOTAL SCORE ACROSS ALL LEVELS: {total_score:.0f}")
    
    # Find the single best strategy overall
    all_strategies = []
    for level_num, data in results.items():
        if "results" in data:
            for result in data["results"]:
                all_strategies.append({
                    "level": level_num,
                    **result
                })
    
    if all_strategies:
        all_strategies.sort(key=lambda x: x["score"], reverse=True)
        
        print(f"\n{'='*80}")
        print("TOP 10 STRATEGIES OVERALL")
        print(f"{'='*80}")
        
        for i, strat in enumerate(all_strategies[:10], 1):
            print(f"{i:2}. Level {strat['level']}: {strat['name'][:50]}")
            print(f"   Score: {strat['score']:.0f} | Enteloot: {strat['enteloot']:.0f} | Valid: {strat['valid']}/{strat['actions']}")


if __name__ == "__main__":
    print("AGE OF ENTELAND - OPTIMAL STRATEGY FINDER")
    print("=" * 60)
    
    # Find optimal for all levels
    results = find_optimal_for_all_levels()
    
    # Analyze overall
    analyze_best_overall(results)
    
    # Save results
    with open("optimal_strategies.json", "w") as f:
        # Clean results for JSON
        clean_results = {}
        for level_num, data in results.items():
            clean_results[str(level_num)] = {
                "best": data["best"] if "best" in data else None,
                "error": data.get("error")
            }
        json.dump(clean_results, f, indent=2)
    
    print(f"\nResults saved to optimal_strategies.json")