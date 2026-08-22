import json
from Simulator import Simulator
from collections import defaultdict

class Level2Optimizer:
    """Optimized Level 2 strategy for Age of Enteland"""
    
    def __init__(self, level_json, constants_json):
        self.level = level_json
        self.constants = constants_json
        self.towns = level_json["towns"]
        self.nodes = level_json.get("nodes", {})
        self.routes = level_json.get("routes", [])
        self.total_ticks = level_json["run"]["total_ticks"]
        self.starting_town = level_json["run"]["starting_town"]
        self.starting_enteloot = level_json["run"]["starting_enteloot"]
        
        # Constants
        self.resources = constants_json["resources"]
        self.recipes = constants_json["recipes"]
        self.components = constants_json["components"]
        self.production_upgrades = constants_json["upgrades"]["production"]
        self.civic_upgrades = constants_json["upgrades"]["civic"]
        self.const = constants_json["constants"]
        
        # Build adjacency
        self.adj = defaultdict(list)
        for route in self.routes:
            a, b = route["between"]
            weight = route["weight"]
            toll = route.get("toll", 0)
            self.adj[a].append((b, weight, toll))
            self.adj[b].append((a, weight, toll))
        
        # Resource nodes
        self.resource_nodes = defaultdict(list)
        for node_name, node_data in self.nodes.items():
            resource = node_data["resource"]
            yield_amt = node_data["yield"]
            gather_time = node_data.get("gather-time", 2)
            self.resource_nodes[resource].append((node_name, yield_amt, gather_time))
        
        # Find best nodes
        self.best_node = {}
        for resource, nodes in self.resource_nodes.items():
            self.best_node[resource] = max(nodes, key=lambda x: x[1])
        
        # Crafting affinity towns
        self.affinity_towns = []
        for town_name, town_data in self.towns.items():
            if "crafting" in town_data.get("affinities", []):
                self.affinity_towns.append(town_name)
        
        # Best selling towns
        self.best_sellers = {}
        for town_name, town_data in self.towns.items():
            for item, rate in town_data.get("item-rates", {}).items():
                if item not in self.best_sellers or rate > self.best_sellers[item][1]:
                    self.best_sellers[item] = (town_name, rate)
    
    def find_path(self, start, end):
        """Find shortest path"""
        from collections import deque
        
        if start == end:
            return [start]
        
        visited = {start}
        queue = deque([(start, [start])])
        
        while queue:
            node, path = queue.popleft()
            for neighbor, weight, toll in self.adj.get(node, []):
                if neighbor not in visited:
                    if neighbor == end:
                        return path + [neighbor]
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
        return None
    
    def analyze_recipe_efficiency(self):
        """Calculate value per gather for each recipe"""
        efficiency = {}
        
        for recipe_name, recipe_data in self.recipes.items():
            if not recipe_data.get("sellable", True):
                continue
                
            inputs = recipe_data["inputs"]
            total_gathers = 0
            
            for resource, amount in inputs.items():
                if resource in self.best_node:
                    node_name, yield_amt, gather_time = self.best_node[resource]
                    total_gathers += amount / yield_amt
            
            best_price = self.best_sellers.get(recipe_name, (None, 0))[1]
            
            if best_price > 0 and total_gathers > 0:
                efficiency[recipe_name] = {
                    "value_per_gather": best_price / total_gathers,
                    "best_price": best_price,
                    "best_town": self.best_sellers[recipe_name][0],
                    "total_gathers": total_gathers,
                    "craft_time": recipe_data["craft_time"]
                }
        
        return efficiency
    
    def generate_actions(self):
        """Generate optimized action sequence"""
        actions = []
        current_town = self.starting_town
        
        def travel_to(destination):
            nonlocal current_town
            if destination != current_town:
                path = self.find_path(current_town, destination)
                if path:
                    actions.append({"type": "travel", "destination": destination})
                    current_town = destination
        
        def build_upgrade(upgrade, town=None):
            if town is None:
                town = self.starting_town
            travel_to(town)
            actions.append({"type": "build", "upgrade": upgrade})
        
        print(f"\n{'='*60}")
        print("LEVEL 2 OPTIMIZED STRATEGY")
        print(f"{'='*60}")
        
        # Find best recipe
        recipe_efficiency = self.analyze_recipe_efficiency()
        
        print("\n📊 RECIPE EFFICIENCY (Value per gather):")
        for recipe, data in sorted(recipe_efficiency.items(), 
                                   key=lambda x: x[1]["value_per_gather"], 
                                   reverse=True)[:5]:
            print(f"  {recipe}: {data['value_per_gather']:.1f} (sell: {data['best_price']} at {data['best_town']})")
        
        best_recipe = max(recipe_efficiency.items(), 
                         key=lambda x: x[1]["value_per_gather"])
        recipe_name, recipe_data = best_recipe
        
        print(f"\n✅ Best recipe: {recipe_name}")
        print(f"   Value per gather: {recipe_data['value_per_gather']:.1f}")
        print(f"   Sell at: {recipe_data['best_town']} for {recipe_data['best_price']}")
        
        craft_town = self.affinity_towns[0] if self.affinity_towns else self.starting_town
        print(f"   Craft at: {craft_town} (crafting affinity)")
        
        # ============================================================
        # PHASE 1: Early Crafting (Ticks 0-1000)
        # ============================================================
        print("\n📌 PHASE 1: Early Crafting (0-1000 ticks)")
        
        # Gather wheat at N1 (6 wheat/gather, 2 ticks)
        # Target: 150 bread = 450 wheat = 75 gathers
        wheat_gathers = 75
        print(f"   Gathering {wheat_gathers} wheat at N1...")
        
        travel_to("N1")
        for _ in range(wheat_gathers):
            actions.append({"type": "gather"})
        
        # Craft bread at Demacia (1 tick with affinity)
        bread_count = wheat_gathers * 6 // 3  # 75 * 6 / 3 = 150
        print(f"   Crafting {bread_count} bread at {craft_town}...")
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "bread", "quantity": bread_count})
        
        # Sell at best town
        best_town = recipe_data["best_town"]
        print(f"   Selling {bread_count} bread at {best_town}...")
        travel_to(best_town)
        actions.append({"type": "sell", "item": "bread", "quantity": bread_count})
        
        # ============================================================
        # PHASE 2: Production Upgrades (Ticks 1000-2500)
        # ============================================================
        print("\n📌 PHASE 2: Building Production Upgrades")
        
        # Fertilised-fields at Demacia (doubles wheat)
        # Components: 2 fencing + 2 thatch, 500 Enteloot
        print("   Building Fertilised-fields at Demacia...")
        
        # Thatch: 2 wheat each, need 2 thatch = 4 wheat
        travel_to("N1")
        actions.append({"type": "gather"})  # 6 wheat
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "thatch", "quantity": 2})
        
        # Fencing: 2 wood + 1 rope each, need 2 fencing
        # Rope: 2 sheep each, need 2 rope = 4 sheep
        travel_to("N6")
        actions.append({"type": "gather"})  # 3 sheep
        actions.append({"type": "gather"})  # 3 sheep (6 total)
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "rope", "quantity": 2})
        
        # Wood for fencing: 2 wood each, need 2 fencing = 4 wood
        travel_to("Noxus")
        travel_to("N2")
        actions.append({"type": "gather"})  # 5 wood
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "fencing", "quantity": 2})
        
        # Build Fertilised-fields
        build_upgrade("fertilised-fields", self.starting_town)
        print("   ✓ Fertilised-fields built!")
        
        # Farmhouse at Demacia (doubles sheep)
        # Components: 3 planks + 2 thatch, 500 Enteloot
        print("   Building Farmhouse at Demacia...")
        
        # Planks: 2 wood each, need 3 planks = 6 wood
        travel_to("Noxus")
        travel_to("N2")
        actions.append({"type": "gather"})  # 5 wood
        actions.append({"type": "gather"})  # 10 wood
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "planks", "quantity": 3})
        
        # Thatch: 2 wheat each, need 2 thatch = 4 wheat
        travel_to("N1")
        actions.append({"type": "gather"})  # 6 wheat
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "thatch", "quantity": 2})
        
        # Build Farmhouse
        build_upgrade("farmhouse", self.starting_town)
        print("   ✓ Farmhouse built!")
        
        # ============================================================
        # PHASE 3: Civic Upgrades (Ticks 2500-4000)
        # ============================================================
        print("\n📌 PHASE 3: Building Civic Upgrades")
        
        # Rec-center at Demacia (requires 1 production upgrade)
        # Components: 4 planks + 3 bricks + 1 rope, 1200 Enteloot
        print("   Building Rec-center at Demacia...")
        
        # Planks: 4 planks = 8 wood
        travel_to("Noxus")
        travel_to("N2")
        actions.append({"type": "gather"})  # 5 wood
        actions.append({"type": "gather"})  # 10 wood
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "planks", "quantity": 4})
        
        # Bricks: 3 bricks = 6 clay + 3 mortar = 9 clay + 3 stone
        travel_to("Ionia")
        travel_to("N4")
        actions.append({"type": "gather"})  # 4 clay
        actions.append({"type": "gather"})  # 8 clay
        actions.append({"type": "gather"})  # 12 clay
        
        travel_to("Noxus")
        travel_to("N3")
        actions.append({"type": "gather"})  # 5 stone
        travel_to(craft_town)
        
        actions.append({"type": "craft", "item": "mortar", "quantity": 3})
        actions.append({"type": "craft", "item": "bricks", "quantity": 3})
        
        # Rope: 1 rope = 2 sheep
        travel_to("N6")
        actions.append({"type": "gather"})  # 3 sheep
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "rope", "quantity": 1})
        
        # Build Rec-center
        build_upgrade("rec-center", self.starting_town)
        print("   ✓ Rec-center built!")
        
        # School at Demacia (requires Rec-center)
        # Components: 6 bricks + 3 planks + 2 kiln-glass, 2000 Enteloot
        print("   Building School at Demacia...")
        
        # Planks: 3 planks = 6 wood
        travel_to("Noxus")
        travel_to("N2")
        actions.append({"type": "gather"})  # 5 wood
        actions.append({"type": "gather"})  # 10 wood
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "planks", "quantity": 3})
        
        # Bricks: 6 bricks = 12 clay + 6 mortar = 18 clay + 6 stone
        travel_to("Ionia")
        travel_to("N4")
        for _ in range(5):
            actions.append({"type": "gather"})  # 20 clay
        
        travel_to("Noxus")
        travel_to("N3")
        for _ in range(2):
            actions.append({"type": "gather"})  # 10 stone
        travel_to(craft_town)
        
        actions.append({"type": "craft", "item": "mortar", "quantity": 6})
        actions.append({"type": "craft", "item": "bricks", "quantity": 6})
        
        # Kiln-glass: 2 clay + 2 wood each, need 2 kiln-glass = 4 clay + 4 wood
        travel_to("Ionia")
        travel_to("N4")
        actions.append({"type": "gather"})  # 4 clay
        travel_to("Noxus")
        travel_to("N2")
        actions.append({"type": "gather"})  # 5 wood
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "kiln-glass", "quantity": 2})
        
        # Build School
        build_upgrade("school", self.starting_town)
        print("   ✓ School built!")
        
        # ============================================================
        # PHASE 4: Final Crafting Push (Ticks 4000-5000)
        # ============================================================
        print("\n📌 PHASE 4: Final Crafting Push (4000-5000 ticks)")
        
        # Gather wheat at N1
        final_gathers = 150
        print(f"   Gathering {final_gathers} wheat at N1...")
        travel_to("N1")
        for _ in range(final_gathers):
            actions.append({"type": "gather"})
        
        # Craft bread at Demacia
        final_bread = final_gathers * 6 // 3
        print(f"   Crafting {final_bread} bread at {craft_town}...")
        travel_to(craft_town)
        actions.append({"type": "craft", "item": "bread", "quantity": final_bread})
        
        # Sell at best town
        print(f"   Selling {final_bread} bread at {best_town}...")
        travel_to(best_town)
        actions.append({"type": "sell", "item": "bread", "quantity": final_bread})
        
        print(f"\n📊 Generated {len(actions)} actions")
        return actions

# Main execution
def main():
    print("=== AGE OF ENTELAND - LEVEL 2 OPTIMIZER ===\n")
    
    # Load level data
    try:
        with open("level2.json") as f:
            level = json.load(f)
        print("✓ Loaded level2.json")
    except FileNotFoundError:
        print("❌ Error: level2.json not found!")
        return
    
    try:
        with open("constants.json") as f:
            constants = json.load(f)
        print("✓ Loaded constants.json")
    except FileNotFoundError:
        print("❌ Error: constants.json not found!")
        return
    
    # Create optimizer
    optimizer = Level2Optimizer(level, constants)
    
    # Generate actions
    actions = optimizer.generate_actions()
    
    # Write to output.txt
    output = {"actions": actions}
    with open("output.txt", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n✓ Written {len(actions)} actions to output.txt")
    
    # Run simulation
    print("\n🔄 Running simulation...")
    sim = Simulator(level, constants, level_number=2)
    log = sim.run(actions)
    
    # Print summary
    print()
    print("=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    for k, v in sim.summary().items():
        print(f"{k}: {v}")

if __name__ == "__main__":
    main()