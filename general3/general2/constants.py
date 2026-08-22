# constants.py
import json
import os

# Load the constants from JSON
with open(os.path.join(os.path.dirname(__file__), 'constants.json')) as f:
    _data = json.load(f)

# Export all the constants
CONSTANTS = _data['constants']
RESOURCES = _data['resources']
RECIPES = _data['recipes']
COMPONENTS = _data['components']
UPGRADES = _data['upgrades']
TOOLS = _data['tools']
LEVEL_UNLOCKS = _data['level_unlocks']

# Derived constants
PRODUCTION_UPGRADES = UPGRADES.get('production', {})
CIVIC_UPGRADES = UPGRADES.get('civic', {})
ALL_UPGRADES = {**PRODUCTION_UPGRADES, **CIVIC_UPGRADES}
CRAFTABLES = {**RECIPES, **COMPONENTS}

# Helper functions
def sell_price(resource):
    """Get sell price for a resource"""
    if resource in RESOURCES:
        return RESOURCES[resource]['sell_price']
    return None

def buy_price(resource):
    """Get buy price for a resource"""
    if resource in RESOURCES:
        return RESOURCES[resource]['buy_price']
    return None

def features_for_level(level):
    """Get features available at a given level"""
    level = str(level)
    features = {
        'travel': False,
        'buy': False,
        'sell': False,
        'gather': False,
        'crafting': False,
        'building': False,
        'recipes': False,
        'components': False,
        'production_upgrades': False,
        'civic_upgrades': False,
        'fast_routes': False,
        'mines': False,
        'ore': False,
        'iron_fittings': False,
        'tools': False,
        'police_station': False,
        'upkeep': False,
    }
    
    if level in LEVEL_UNLOCKS:
        for feature in LEVEL_UNLOCKS[level]:
            if feature == 'fast_routes':
                features['fast_routes'] = True
            elif feature == 'mine_nodes':
                features['mines'] = True
            elif feature == 'ore':
                features['ore'] = True
            elif feature == 'iron-fittings':
                features['iron_fittings'] = True
            elif feature == 'tools':
                features['tools'] = True
            elif feature == 'police-station':
                features['police_station'] = True
            elif feature == 'upkeep':
                features['upkeep'] = True
            elif feature == 'craft':
                features['crafting'] = True
            elif feature == 'build':
                features['building'] = True
            elif feature == 'recipes':
                features['recipes'] = True
            elif feature == 'construction_components':
                features['components'] = True
            elif feature == 'production_upgrades':
                features['production_upgrades'] = True
            elif feature == 'civic_upgrades':
                features['civic_upgrades'] = True
    
    # Level 1 always has basic features
    if level == '1':
        features['travel'] = True
        features['buy'] = True
        features['sell'] = True
        features['gather'] = True
    # Level 2+ has all level 1 features plus more
    elif int(level) >= 2:
        features['travel'] = True
        features['buy'] = True
        features['sell'] = True
        features['gather'] = True
    
    return features