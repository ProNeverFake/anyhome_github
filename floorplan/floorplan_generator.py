import os
import regex
import numpy as np
from matplotlib.colors import LinearSegmentedColormap
import openai
import json
from termcolor import colored
import matplotlib.pyplot as plt
import torch
from torchvision.utils import save_image

from floorplan.houseganpp.models import Generator
from floorplan.houseganpp.utils import _init_input, draw_masks, draw_graph
import floorplan.utils as utils


class FloorplanGenerator:
    def __init__(self, description, output_dir="floorplan/output", houseganpp_weight="floorplan/houseganpp/checkpoints/pretrained.pth"):
        self.description = description
        self.output_dir = output_dir
        self.houseganpp_weight = houseganpp_weight

    def generate_house_mesh(self, edit=False):
        """  
        Generate a complete 3D house mesh from the text description.  
        
        This method orchestrates the entire floorplan generation pipeline, converting  
        a natural language description into a structured 3D house representation with  
        walls, floors, and room boundaries.  
        
        Args:  
            edit (bool, optional): If True, enables interactive language-guided editing  
                mode where users can iteratively refine the floorplan through natural  
                language descriptions. Defaults to False.  
        
        Returns:  
            tuple: A 6-tuple containing:  
                - house_v (list): 3D mesh vertices representing walls and floors  
                - house_f (list): Face indices for the 3D mesh triangulation  
                - border_map_no_doors (numpy.ndarray): 2D integer map of the floorplan  
                where -2=walls, -10=outside, positive integers=room indices  
                - room_name_dict (dict): Mapping from original room names to standardized  
                room types used internally  
                - boxes (dict): Bounding box coordinates for each room, used for furniture  
                placement  
                - centers (dict): Center point coordinates for each room  
        
        Process:  
            1. Generates bubble diagram from text description using GPT-4  
            2. Creates floorplan layout masks using HouseGAN++  
            3. Post-processes masks to ensure validity and coherence  
            4. Extracts wall segments and generates 3D mesh geometry  
            5. Optionally allows iterative editing through natural language  
        
        Raises:  
            Exception: If GPT-4 API calls fail or HouseGAN++ model loading fails  
            
        Example:  
            >>> generator = FloorplanGenerator("A 2-bedroom apartment with kitchen")  
            >>> house_v, house_f, border_map, rooms, boxes, centers = generator.generate_house_mesh()  
            >>> # For interactive editing:  
            >>> house_data = generator.generate_house_mesh(edit=True)  
        """
        print(colored("Generating house mesh...", "grey"))
        # Generate graph from descriptoin
        nodes, edges, room_name_dict, room_list, floorplan_str = self.generate_bubble_diagram(self.description)
        # Generate floorplan
        border_map_no_doors, boxes, centers = self.generate_floorplan(nodes, edges, room_name_dict, room_list)
        # Handle editing
        while edit:
            edit_description = input(colored("Enter the description of the changes you want to make to the floorplan (Enter q to stop editing): ", "green"))
            if edit_description == "q" or edit_description == "":
                break
            nodes, edges, room_name_dict, room_list, floorplan_str = self.generate_bubble_diagram(self.description, is_edit=True, edit_description=edit_description, edit_fp=floorplan_str)
            border_map_no_doors, boxes, centers = self.generate_floorplan(nodes, edges, room_name_dict, room_list)
        # Generate house mesh
        segments = utils.find_segments(border_map_no_doors)
        house_v, house_f = utils.write_to_obj(segments, border_map_no_doors)
        print(colored("House Mesh Generated Successfully!", "magenta"))

        return house_v, house_f, border_map_no_doors, room_name_dict, boxes, centers

    def generate_floorplan(self, nodes, edges, room_name_dict, room_list):
        # Control whether the generated map is valid
        status = False
        while not status:
            # Generate masks for house layout 
            # * here house++
            masks = self.generate_layout_masks(nodes, edges)
            # Check whether the resulted floor plan is valid and generate a single integer map for the house layout
            img_size = 256
            status, result_map, result_map_no_doors, result_masks = utils.check_post_processing(nodes, edges, masks, list(range(len(room_name_dict), len(room_list))), img_size)

        # Change the color map into a border map
        border_map_no_doors, start_coors = utils.generate_border_map_no_doors(result_map_no_doors, result_masks, list(range(len(room_name_dict), len(room_list))), nodes, edges)
        # Define the custom colors
        custom_colors = [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), *plt.cm.tab20b(np.linspace(0, 1, 19))]
        # Decompose each room into regular rectangles and obtain the centers
        boxes, centers = utils.get_room_boundaries(border_map_no_doors, len(room_list)-1, start_coors)
        utils.visualize_map_with_centers(border_map_no_doors, boxes, centers)

        return border_map_no_doors, boxes, centers

    def generate_floorplan_from_description(self, description, is_edit = False, edit_description=None, edit_fp=None):
        # Generate a graph from description using GPT-4
        context_msg = """
        Task: You are a talented Architectural Planner tasked with envisioning the floorplan for a house described as {}. Your need to generate four things as described below:

        Requirements:
        1. Complete Room List: Compile a comprehensive list of all rooms within the house. The list should adhere as close as possible with the house description given. If a room type appears multiple times, append an index to differentiate them. Without exceeding the house description, aim for a diverse assortment of rooms, covering a wide range of functionalities. You can generate any room type for the complete room list. For example, if the house features two bedrooms and one dining room, list them as: "[bedroom1, bedroom2, dining room1]".
        2. Modified Room List: Adapt the previously listed rooms into a standardized set of room types based on their functionality and the house's style. Use only the following room types: kitchen, storage, bathroom, study_room, balcony, living_room, bedroom, entrance, dining_room, and unknown. Transform unique rooms to the closest match from the predefined list, appending an index if necessary. If a room does not match any predefined type, label it as "unknown".
        3. Room Connections: Map out the connectivity between rooms, detailing which rooms are directly accessible from each other. Present this information as a list of tuples, indicating room pairs that share a connection. For instance, if the dining room connects to both bedroom1 and bedroom2, but there is no direct connection between the two bedrooms, list the connections as: "[[dining_room1, bedroom1], [dining_room1, bedroom2]]". The room names should be from the complete room list that you've generated at requirement 1.
        4. Front Door Locations: Identify the rooms that house the main entrances to the dwelling. Specify each room that contains a front door, considering it as the primary access point to the house.

        Output: Provide the information in a valid JSON structure with no spaces. Don't include anything beside the requested data represented in the following format:
        {{
            "complete_room_list": [...],
            ”modified_room_list": [...],
            ”connection": [...],
            ”front_door": [...]
        }}
        """

        edit_context_msg = """
        Context: You are a talented Architectural Planner serving a customer. You are given a floorplan for a house as represented by the data below. The data was generated as the four requirements below:

        Requirements:
        1. Complete Room List: Compile a comprehensive list of all rooms within the house. The list should adhere as close as possible with the house description given. If a room type appears multiple times, append an index to differentiate them. Without exceeding the house description, aim for a diverse assortment of rooms, covering a wide range of functionalities. You can generate any room type for the complete room list. For example, if the house features two bedrooms and one dining room, list them as: "[bedroom1, bedroom2, dining room1]".
        2. Modified Room List: Adapt the previously listed rooms into a standardized set of room types based on their functionality and the house's style. Use only the following room types: kitchen, storage, bathroom, study_room, balcony, living_room, bedroom, entrance, dining_room, and unknown. Transform unique rooms to the closest match from the predefined list, appending an index if necessary. If a room does not match any predefined type, label it as "unknown".
        3. Room Connections: Map out the connectivity between rooms, detailing which rooms are directly accessible from each other. Present this information as a list of tuples, indicating room pairs that share a connection. For instance, if the dining room connects to both bedroom1 and bedroom2, but there is no direct connection between the two bedrooms, list the connections as: "[[dining_room1, bedroom1], [dining_room1, bedroom2]]". The room names should be consistent with the names in the complete room list that you've generated.
        4. Front Door Locations: Identify the rooms that house the main entrances to the dwelling. Specify each room that contains a front door, considering it as the primary access point to the house. The room name should be consistent with the names in the complete room list that you've generated.

        The description of the House:
        {}

        Generated Data of the Floorplan of the House:
        {}

        Now, the customer wants to edit the floorplan. They have provided the following description of the changes they want to make:
        {}

        Task: Could you please update the floorplan based on the customer's request and provide the updated data strictly following the same requirements above?

        Output: Provide the information in a valid JSON structure with no spaces. Don't include anything beside the requested data represented in the following format:
        {{
            "complete_room_list": [...],
            ”modified_room_list": [...],
            ”connection": [...],
            ”front_door": [...]
        }}
        """

        client = openai.OpenAI(base_url="http://localhost:8001/v1", api_key="your_api_key_here")  # Replace with your OpenAI API key
        raw_response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "user", "content": context_msg.format(description) if not is_edit else edit_context_msg.format(description, edit_fp, edit_description)},
            ],
            temperature=0.7,
            max_tokens=2048
        )

        response_str = raw_response.choices[0].message.content
        raw_response = response_str.replace("\n", "").replace(" ", "")
        return [response_str, raw_response]

    def generate_bubble_diagram(
        self, description, is_edit=False, edit_description=None, edit_fp=None
    ):
        """Generate a bubble diagram representation of a house floorplan based on the given description.

        Args:
            description (str): Description of the house to generate the floorplan for.
            is_edit (bool, optional): Whether this is an edit to an existing floorplan. Defaults to False.
            edit_description (str, optional): Description of changes to make to the existing floorplan. Defaults to None.
            edit_fp (str, optional): Existing floorplan data to modify. Defaults to None.

        Returns:
            tuple: A tuple containing the following:
                - graph_nodes (torch.FloatTensor): Node features of the generated graph.
                - graph_edges (torch.LongTensor): Edge indices of the generated graph.
                - room_name_dict (dict): Mapping of room names to their standardized types.
                - room_list (list): List of room types in the generated floorplan.
                - response_str (str): The raw response string from the model.
        """

        # Generate a graph from description using GPT-4
        context_msg = """
        Task: You are a talented Architectural Planner tasked with envisioning the floorplan for a house described as {}. Your need to generate four things as described below:

        Requirements:
        1. Complete Room List: Compile a comprehensive list of all rooms within the house. The list should adhere as close as possible with the house description given. If a room type appears multiple times, append an index to differentiate them. Without exceeding the house description, aim for a diverse assortment of rooms, covering a wide range of functionalities. You can generate any room type for the complete room list. For example, if the house features two bedrooms and one dining room, list them as: "[bedroom1, bedroom2, dining room1]".
        2. Modified Room List: Adapt the previously listed rooms into a standardized set of room types based on their functionality and the house's style. Use only the following room types: kitchen, storage, bathroom, study_room, balcony, living_room, bedroom, entrance, dining_room, and unknown. Transform unique rooms to the closest match from the predefined list, appending an index if necessary. If a room does not match any predefined type, label it as "unknown".
        3. Room Connections: Map out the connectivity between rooms, detailing which rooms are directly accessible from each other. Present this information as a list of tuples, indicating room pairs that share a connection. For instance, if the dining room connects to both bedroom1 and bedroom2, but there is no direct connection between the two bedrooms, list the connections as: "[[dining_room1, bedroom1], [dining_room1, bedroom2]]". The room names should be from the complete room list that you've generated at requirement 1.
        4. Front Door Locations: Identify the rooms that house the main entrances to the dwelling. Specify each room that contains a front door, considering it as the primary access point to the house.

        Output: Provide the information in a valid JSON structure with no spaces. Don't include anything beside the requested data represented in the following format:
        {{
            "complete_room_list": [...],
            ”modified_room_list": [...],
            ”connection": [...],
            ”front_door": [...]
        }}
        """

        edit_context_msg = """
        Context: You are a talented Architectural Planner serving a customer. You are given a floorplan for a house as represented by the data below. The data was generated as the four requirements below:

        Requirements:
        1. Complete Room List: Compile a comprehensive list of all rooms within the house. The list should adhere as close as possible with the house description given. If a room type appears multiple times, append an index to differentiate them. Without exceeding the house description, aim for a diverse assortment of rooms, covering a wide range of functionalities. You can generate any room type for the complete room list. For example, if the house features two bedrooms and one dining room, list them as: "[bedroom1, bedroom2, dining room1]".
        2. Modified Room List: Adapt the previously listed rooms into a standardized set of room types based on their functionality and the house's style. Use only the following room types: kitchen, storage, bathroom, study_room, balcony, living_room, bedroom, entrance, dining_room, and unknown. Transform unique rooms to the closest match from the predefined list, appending an index if necessary. If a room does not match any predefined type, label it as "unknown".
        3. Room Connections: Map out the connectivity between rooms, detailing which rooms are directly accessible from each other. Present this information as a list of tuples, indicating room pairs that share a connection. For instance, if the dining room connects to both bedroom1 and bedroom2, but there is no direct connection between the two bedrooms, list the connections as: "[[dining_room1, bedroom1], [dining_room1, bedroom2]]". The room names should be consistent with the names in the complete room list that you've generated.
        4. Front Door Locations: Identify the rooms that house the main entrances to the dwelling. Specify each room that contains a front door, considering it as the primary access point to the house. The room name should be consistent with the names in the complete room list that you've generated.

        The description of the House:
        {}

        Generated Data of the Floorplan of the House:
        {}

        Now, the customer wants to edit the floorplan. They have provided the following description of the changes they want to make:
        {}

        Task: Could you please update the floorplan based on the customer's request and provide the updated data strictly following the same requirements above?

        Output: Provide the information in a valid JSON structure with no spaces. Don't include anything beside the requested data represented in the following format:
        {{
            "complete_room_list": [...],
            ”modified_room_list": [...],
            ”connection": [...],
            ”front_door": [...]
        }}
        """

        client = openai.OpenAI(base_url="http://localhost:8001/v1", api_key="your_api_key_here")  # Replace with your OpenAI API key
        raw_response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "user", "content": context_msg.format(description) if not is_edit else edit_context_msg.format(description, edit_fp, edit_description)},
            ],
            temperature=0.7,
            max_tokens=2048
        )

        response_str = raw_response.choices[0].message.content
        raw_response = response_str.replace("\n", "").replace(" ", "")

        # save the response str and the raw response in a file for debug
        with open('debug_output.txt', 'w') as f:
            f.write(f"Raw Response: {raw_response} ")
            f.write(f"Processed Response: {response_str}")

        # import ipdb; ipdb.set_trace()

        pattern = r'\{(?:[^{}]|(?R))*\}'  # regex to discard text paragraphs before or after the JSON object
        response = json.loads(regex.search(pattern, raw_response).group())

        print(colored("House Floorplan Graph", "yellow"))
        print('\n'.join([f'{colored(k, "blue")}: {v}' for k, v in response.items()]))

        complete_room_list = response["complete_room_list"]
        room_list = response["modified_room_list"]
        room_name_dict = dict(zip(complete_room_list, room_list))
        real_nodes_len = len(room_list)

        connection = response["connection"]
        original_connection = connection.copy()
        # change to the original room names to the HouseGAN++ room names
        connection = [[room_name_dict[pair[0]], room_name_dict[pair[1]]] for pair in connection]
        raw_connection = connection.copy()
        front_door = response["front_door"]
        front_door = [room_name_dict[door] for door in front_door]

        # add interior doors nodes to the room_list
        interior_door_list = ["interior_door" + str(i) for i in range(1, len(connection) + 1)]
        room_list.extend(interior_door_list)
        front_door_list = ["front_door" + str(i) for i in range(1, len(front_door) + 1)]
        room_list.extend(front_door_list)
        connection = [(pair[0], "interior_door" + str(i), pair[1]) for i, pair in enumerate(connection, 1)]

        # the list of room types and their corresponding indices
        type_list = {"living_room": 1, "kitchen": 2, "bedroom": 3, "bathroom": 4, "balcony": 5, "entrance": 6,
                    "dining_room": 7, "study_room": 8, "storage": 10, "front_door": 15, "unknown": 16, "interior_door": 17}

        # turn the room types into indices
        room_list_indices = [type_list[room[:-1]] if room[:-1] in type_list else type_list["unknown"] for room in room_list]
        # add the index to each room for better formatting the edges of the bubble graph
        room_dict = {room: i for i, room in enumerate(room_list)}
        connection = [(room_dict[pair[0]], room_dict[pair[1]], room_dict[pair[2]]) for pair in connection]

        # generate the edges as triples
        triples = []
        for i in range(len(room_list)):
            for j in range(len(room_list)):
                if j > i:
                    triples.append((i, -1, j))

        for i in range(real_nodes_len):
            for j in range(real_nodes_len):
                if j > i:
                    is_adjacent = any([True for e_map in connection if (i in e_map) and (j in e_map)])
                    # print(i, j, is_adjacent)
                    if is_adjacent:
                        edge = [pair[1] for pair in connection if (pair[0] == i and pair[2] == j) or
                                (pair[0] == j and pair[2] == i)]
                        triples.remove((i, -1, edge[0]))
                        triples.remove((j, -1, edge[0]))
                        triples.remove((i, -1, j))
                        triples.append((i, 1, edge[0]))
                        triples.append((j, 1, edge[0]))
                        triples.append((i, 1, j))

        # add the front door edges
        for i, room in enumerate(front_door, 1):
            triples.remove((room_dict[room], -1, room_dict["front_door" + str(i)]))
            triples.append((room_dict[room], 1, room_dict["front_door" + str(i)]))

        organized_triples = sorted(triples, key=lambda x: (x[0], x[2]))

        graph_nodes = utils.one_hot_embedding(room_list_indices)[:, 1:]
        graph_nodes = torch.FloatTensor(graph_nodes)
        graph_edges = torch.LongTensor(organized_triples)

        return graph_nodes, graph_edges, room_name_dict, room_list, str(response)

    def generate_layout_masks(self, nodes, edges):
        # Create output dir
        os.makedirs(self.output_dir, exist_ok=True)
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        # Initialize generator and discriminator
        model = Generator()
        model.load_state_dict(torch.load(self.houseganpp_weight, map_location=torch.device(device)), strict=True)
        model = model.eval()

        # Initialize variables
        if torch.cuda.is_available():
            model.cuda()

        # Draw real graph
        real_nodes = np.where(nodes.detach().cpu()==1)[-1]
        graph = [nodes, edges]
        true_graph_obj, graph_im = draw_graph([real_nodes, edges.detach().cpu().numpy()])
        graph_im.save('./{}/input_graph.png'.format(self.output_dir)) # save graph

        # Add room types incrementally
        _types = sorted(list(set(real_nodes)))
        selected_types = [_types[:k+1] for k in range(10)]
        os.makedirs('./{}/'.format(self.output_dir), exist_ok=True)
        _round = 0

        # Initialize layout
        state = {'masks': None, 'fixed_nodes': []}
        masks = self._infer(graph, model, state)
        im0 = draw_masks(masks.copy(), real_nodes)
        im0 = torch.tensor(np.array(im0).transpose((2, 0, 1)))/255.0
        save_image(im0, './{}/init_fp.png'.format(self.output_dir), nrow=1, normalize=False) # visualize init image

        # Generate per room type
        for _iter, _types in enumerate(selected_types):
            _fixed_nodes = np.concatenate([np.where(real_nodes == _t)[0] for _t in _types]) \
                if len(_types) > 0 else np.array([])
            state = {'masks': masks, 'fixed_nodes': _fixed_nodes}
            masks = self._infer(graph, model, state)

        # Save final floorplans
        imk = draw_masks(masks.copy(), real_nodes)
        imk = torch.tensor(np.array(imk).transpose((2, 0, 1))) / 255.0
        save_image(imk, './{}/final_fp.png'.format(self.output_dir), nrow=1, normalize=False)

        return masks

    @staticmethod
    def _infer(graph, model, prev_state=None):
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        # configure input to the network
        z, given_masks_in, given_nodes, given_edges = _init_input(graph, prev_state)
        # run inference model
        with torch.no_grad():
            masks = model(z.to(device), given_masks_in.to(device), given_nodes.to(device), given_edges.to(device))
            masks = masks.detach().cpu().numpy()
        return masks
