from floorplan.floorplan_generator import FloorplanGenerator
from layout.layout_generator import LayoutGenerator
import credentials

import json

# Read floorplan_description.json as a dict
with open("floorplan_description.json", "r") as f:
    floorplan_description = json.load(f)

for floorplan in floorplan_description["floorplans"]:
    prompt = floorplan["description"]
    floorplanGenerator = FloorplanGenerator(prompt)
    graph_pkg = floorplanGenerator.generate_floorplan_from_description(description=prompt)
    floorplan['graph_str'] = graph_pkg[0]
    floorplan['graph_raw'] = graph_pkg[1]

    # save the updated floorplan_description.json
    with open('floorplan_description.json', 'w') as f:
        json.dump(floorplan_description, f, indent=4)
        print(f"finished generating floorplan for id {floorplan['id']}")
    # sleep from random time between 1 and 5 seconds
    import time
    import random
    # not int but float
    time.sleep(random.uniform(1, 5))