import enum, time, csv
from re import search

from pyexpat.errors import messages

from agents1.AgentUtils import compute_collected_adjustments, log_info, calculate_wait_time, is_waiting_over
from brains1.ArtificialBrain import ArtificialBrain
from actions1.CustomActions import *
from matrx import utils
from matrx.agents.agent_utils.navigator import Navigator
from matrx.agents.agent_utils.state_tracker import StateTracker
from matrx.actions.object_actions import RemoveObject
from matrx.messages.message import Message
from actions1.CustomActions import CarryObject, Drop
from collections import defaultdict


class Phase(enum.Enum):
    INTRO = 1,
    FIND_NEXT_GOAL = 2,
    PICK_UNSEARCHED_ROOM = 3,
    PLAN_PATH_TO_ROOM = 4,
    FOLLOW_PATH_TO_ROOM = 5,
    PLAN_ROOM_SEARCH_PATH = 6,
    FOLLOW_ROOM_SEARCH_PATH = 7,
    PLAN_PATH_TO_VICTIM = 8,
    FOLLOW_PATH_TO_VICTIM = 9,
    TAKE_VICTIM = 10,
    PLAN_PATH_TO_DROPPOINT = 11,
    FOLLOW_PATH_TO_DROPPOINT = 12,
    DROP_VICTIM = 13,
    WAIT_FOR_HUMAN = 14,
    WAIT_AT_ZONE = 15,
    FIX_ORDER_GRAB = 16,
    FIX_ORDER_DROP = 17,
    REMOVE_OBSTACLE_IF_NEEDED = 18,
    ENTER_ROOM = 19


class InfoEvent(enum.Enum):
    FOUND = 1,
    COLLECT = 2,
    REMOVE = 3,
    NOT_FOUND = 4,
    WAIT_OVER = 5,
    DELIVER = 6,
    FALSE_RESCUE = 7,


class Obstacle(enum.Enum):
    ROCK = 1,
    STONE = 2,
    TREE = 3,


class BaselineAgent(ArtificialBrain):
    def __init__(self, slowdown, condition, name, folder):
        super().__init__(slowdown, condition, name, folder)
        # Initialization of some relevant variables
        self._tick = None
        self._slowdown = slowdown
        self._condition = condition
        self._human_name = name
        self._folder = folder
        self._phase = Phase.INTRO
        self._room_vics = []
        self._explored_rooms = []
        self._searched_rooms = defaultdict(list)
        self._known_victims = []
        self._found_victims = defaultdict(list)
        self._collected_victims = []
        self._known_victim_logs = {}
        self._found_victims_logs = defaultdict(list)
        self._send_messages = []
        self._current_door = None
        self._team_members = []
        self._carrying_together = False
        self._remove = False
        self._goal_vic = None
        self._goal_loc = None
        self._human_loc = None
        self._distance_human = None
        self._distance_drop = None
        self._agent_loc = None
        self._todo = []
        self._answered = False
        self._to_search = []
        self._carrying = False
        self._waiting = False
        self._started_waiting_tick = 0
        self._waiting_time = 0
        self._rescue = None
        self._recent_vic = None
        self._received_messages = {}
        self._moving = False
        self._tasks = ['rescue', 'search']
        self._message_count = 0
        self._confirmed_human_info = defaultdict(list)
        self._trust_belief = defaultdict(dict)
        self._confirmed_info_map_length = 0
        self._task_information = {
            "Search:": {"expected_time_to_complete": 1, "task": "search"},
            "Find:": {"expected_time_to_complete": 1, "task": "rescue"},
            "Remove:": {"expected_time_to_complete": 10, "task": "search"},
            "Collect:": {"expected_time_to_complete": 10, "task": "rescue"},
        }
        self._base_trust_beliefs = {
            "search": {
                "competence": 0.5,
                "willingness": 0.5,
            },
            "rescue": {
                "competence": 0.5,
                "willingness": 0.5,
            }
        }
        self._reserved_names = {'ALWAYS_TRUST', 'NEVER_TRUST', 'RANDOM_TRUST'}

    def initialize(self):
        # Initialization of the state tracker and navigation algorithm
        self._state_tracker = StateTracker(agent_id=self.agent_id)
        self._navigator = Navigator(agent_id=self.agent_id, action_set=self.action_set,
                                    algorithm=Navigator.A_STAR_ALGORITHM)
        for task in self._tasks:
            self._trust_belief[self._human_name][task] = {'competence': 0.5, 'willingness': 0.5}

    def filter_observations(self, state):
        # Filtering of the world state before deciding on an action 
        return state

    def _decay_trust(self, receivedMessages) -> float:
        """
        Calculates the total decay to be applied to the trust, based on the timestamps of the messages.
        It looks at the messages within the last `timeframe_to_look_at` and calculates if there is a
        difference bigger than `max_allowed_gap` between the timestamps.
        If there it, it applied a penalty for each second passed.

        Returns the total decay to be applied.
        """
        max_allowed_gap = 15  # max allowed gap between messages
        timeframe_to_look_at = 60
        decay_rate_per_tick = 0.003  # decay per tick

        total_decay = 0
        previous_tick = max(0, self._tick - timeframe_to_look_at)

        # Compute total decay by going through all messages
        for message_tick in receivedMessages.values():
            if message_tick > self._tick - timeframe_to_look_at:
                time_gap = message_tick - previous_tick  # Compute silence duration
                if time_gap > max_allowed_gap:
                    total_decay += decay_rate_per_tick * time_gap  # Accumulate decay
            previous_tick = message_tick  # Update previous tick

        if self._tick - previous_tick > max_allowed_gap:
            time_gap = self._tick - previous_tick
            total_decay += decay_rate_per_tick * time_gap

        return total_decay

    def decide_on_actions(self, state):
        self._tick = time.perf_counter()
        # Identify team members
        agent_name = state[self.agent_id]['obj_id']
        for member in state['World']['team_members']:
            if member != agent_name and member not in self._team_members:
                self._team_members.append(member)
        # Create a list of received messages from the human team member
        for i, mssg in enumerate(self.received_messages):
            for member in self._team_members:
                if mssg.from_id == member and (mssg.content, i) not in self._received_messages:
                    self._received_messages[(mssg.content, i)] = self._tick

        # Process messages from team members
        self._process_messages(state, self._team_members, self._condition)
        # Initialize and update trust beliefs for team members
        trustBeliefs = self._loadBelief(self._team_members, self._folder)
        self._trustBelief(self._team_members, trustBeliefs, self._folder, self._received_messages)
        self._trust_belief = trustBeliefs

        # Check whether human is close in distance
        if state[{'is_human_agent': True}]:
            self._distance_human = 'close'
        if not state[{'is_human_agent': True}]:
            # Define distance between human and agent based on last known area locations
            if self._agent_loc in [1, 2, 3, 4, 5, 6, 7] and self._human_loc in [8, 9, 10, 11, 12, 13, 14]:
                self._distance_human = 'far'
            if self._agent_loc in [1, 2, 3, 4, 5, 6, 7] and self._human_loc in [1, 2, 3, 4, 5, 6, 7]:
                self._distance_human = 'close'
            if self._agent_loc in [8, 9, 10, 11, 12, 13, 14] and self._human_loc in [1, 2, 3, 4, 5, 6, 7]:
                self._distance_human = 'far'
            if self._agent_loc in [8, 9, 10, 11, 12, 13, 14] and self._human_loc in [8, 9, 10, 11, 12, 13, 14]:
                self._distance_human = 'close'

        # Define distance to drop zone based on last known area location
        if self._agent_loc in [1, 2, 5, 6, 8, 9, 11, 12]:
            self._distance_drop = 'far'
        if self._agent_loc in [3, 4, 7, 10, 13, 14]:
            self._distance_drop = 'close'

        # Check whether victims are currently being carried together by human and agent 
        for info in state.values():
            if 'is_human_agent' in info and self._human_name in info['name'] and len(
                    info['is_carrying']) > 0 and 'critical' in info['is_carrying'][0]['obj_id'] or \
                    'is_human_agent' in info and self._human_name in info['name'] and len(
                info['is_carrying']) > 0 and 'mild' in info['is_carrying'][0][
                'obj_id'] and self._rescue == 'together' and not self._moving:
                # If victim is being carried, add to collected victims memory
                if info['is_carrying'][0]['img_name'][8:-4] not in self._collected_victims:
                    self._collected_victims.append(info['is_carrying'][0]['img_name'][8:-4])
                self._carrying_together = True
                self._waiting = False
            if 'is_human_agent' in info and self._human_name in info['name'] and len(info['is_carrying']) == 0:
                self._carrying_together = False
        # If carrying a victim together, let agent be idle (because joint actions are essentially carried out by the human)
        if self._carrying_together == True:
            return None, {}

        # Send the hidden score message for displaying and logging the score during the task, DO NOT REMOVE THIS
        self._send_message('Our score is ' + str(state['rescuebot']['score']) + '.', 'RescueBot')

        # Ongoing loop until the task is terminated, using different phases for defining the agent's behavior
        while True:
            if Phase.INTRO == self._phase:
                # Send introduction message
                self._send_message('Hello! My name is RescueBot. Together we will collaborate and try to search and rescue the 8 victims on our right as quickly as possible. \
                Each critical victim (critically injured girl/critically injured elderly woman/critically injured man/critically injured dog) adds 6 points to our score, \
                each mild victim (mildly injured boy/mildly injured elderly man/mildly injured woman/mildly injured cat) 3 points. \
                If you are ready to begin our mission, you can simply start moving.', 'RescueBot')
                # Wait until the human starts moving before going to the next phase, otherwise remain idle
                if not state[{'is_human_agent': True}]:
                    self._phase = Phase.FIND_NEXT_GOAL
                else:
                    return None, {}

            if Phase.FIND_NEXT_GOAL == self._phase:
                # Definition of some relevant variables
                self._answered = False
                self._goal_vic = None
                self._goal_loc = None
                self._rescue = None
                self._moving = True
                remaining_zones = []
                remaining_vics = []
                remaining = {}
                # Identification of the location of the drop zones
                zones = self._get_drop_zones(state)
                # Identification of which victims still need to be rescued and on which location they should be dropped
                for info in zones:
                    if str(info['img_name'])[8:-4] not in self._collected_victims:
                        remaining_zones.append(info)
                        remaining_vics.append(str(info['img_name'])[8:-4])
                        remaining[str(info['img_name'])[8:-4]] = info['location']
                if remaining_zones:
                    self._remainingZones = remaining_zones
                    self._remaining = remaining
                # Remain idle if there are no victims left to rescue
                if not remaining_zones:
                    return None, {}

                # Check which victims can be rescued next because human or agent already found them
                for vic in remaining_vics:
                    # Define a previously found victim as target victim because all areas have been searched
                    if vic in self._known_victims and vic in self._todo and len(self._explored_rooms) == 0:
                        self._goal_vic = vic
                        self._goal_loc = remaining[vic]
                        # Move to target victim
                        self._rescue = 'together'
                        self._send_message('Moving to ' + self._known_victim_logs[vic][
                            'room'] + ' to pick up ' + self._goal_vic + '. Please come there as well to help me carry ' + self._goal_vic + ' to the drop zone.',
                                           'RescueBot')
                        # Plan path to victim because the exact location is known (i.e., the agent found this victim)
                        if 'location' in self._known_victim_logs[vic].keys():
                            self._phase = Phase.PLAN_PATH_TO_VICTIM
                            return Idle.__name__, {'duration_in_ticks': 25}
                        # Plan path to area because the exact victim location is not known, only the area (i.e., human found this  victim)
                        if 'location' not in self._known_victim_logs[vic].keys():
                            self._phase = Phase.PLAN_PATH_TO_ROOM
                            return Idle.__name__, {'duration_in_ticks': 25}
                    # Define a previously found victim as target victim
                    if vic in self._known_victims and vic not in self._todo:
                        self._goal_vic = vic
                        self._goal_loc = remaining[vic]
                        # Rescue together when victim is critical or when the victim is mildly injured and the human is competent and willing
                        if 'critical' in vic or 'mild' in vic and trustBeliefs[self._human_name]['rescue'][
                            'competence'] > 0.2 \
                                and trustBeliefs[self._human_name]['rescue']['willingness'] > 0.0:
                            self._rescue = 'together'
                        # Rescue alone if the victim is mildly injured and the human is not trustworthy
                        else:
                            self._rescue = 'alone'
                        # Plan path to victim because the exact location is known (i.e., the agent found this victim)
                        if 'location' in self._known_victim_logs[vic].keys():
                            self._phase = Phase.PLAN_PATH_TO_VICTIM
                            return Idle.__name__, {'duration_in_ticks': 25}
                        # Plan path to area because the exact victim location is not known, only the area (i.e., human found this  victim)
                        if 'location' not in self._known_victim_logs[vic].keys():
                            self._phase = Phase.PLAN_PATH_TO_ROOM
                            return Idle.__name__, {'duration_in_ticks': 25}
                    # If there are no target victims found, visit an unsearched area to search for victims
                    if vic not in self._known_victims or vic in self._known_victims and vic in self._todo and len(
                            self._explored_rooms) > 0:
                        self._phase = Phase.PICK_UNSEARCHED_ROOM

            if Phase.PICK_UNSEARCHED_ROOM == self._phase:
                agent_location = state[self.agent_id]['location']
                # Identify which areas are not explored yet
                unsearched_rooms = [room['room_name'] for room in state.values()
                                    if 'class_inheritance' in room
                                    and 'Door' in room['class_inheritance']
                                    and room['room_name'] not in self._explored_rooms
                                    and room['room_name'] not in self._to_search]
                # If all areas have been searched but the task is not finished, start searching areas again
                if self._remainingZones and len(unsearched_rooms) == 0:
                    self._to_search = []
                    self._explored_rooms = []
                    self._send_messages = []
                    self.received_messages = []
                    self.received_messages_content = []
                    for task in self._tasks:
                        self._base_trust_beliefs[task]['competence'] = trustBeliefs[self._human_name][task][
                            'competence']
                        self._base_trust_beliefs[task]['willingness'] = trustBeliefs[self._human_name][task][
                            'willingness']
                    self._send_message('Going to re-search all areas.', 'RescueBot')
                    self._phase = Phase.FIND_NEXT_GOAL
                # If there are still areas to search, define which one to search next
                else:
                    # Identify the closest door when the agent did not search any areas yet
                    if self._current_door == None:
                        # Find all area entrance locations
                        self._door = \
                            state.get_room_doors(self._getClosestRoom(state, unsearched_rooms, agent_location))[
                                0]
                        self._doormat = \
                            state.get_room(self._getClosestRoom(state, unsearched_rooms, agent_location))[-1]['doormat']
                        # Workaround for one area because of some bug
                        if self._door['room_name'] == 'area 1':
                            self._doormat = (3, 5)
                        # Plan path to area
                        self._phase = Phase.PLAN_PATH_TO_ROOM
                    # Identify the closest door when the agent just searched another area
                    if self._current_door != None:
                        self._door = \
                            state.get_room_doors(self._getClosestRoom(state, unsearched_rooms, self._current_door))[0]
                        self._doormat = \
                            state.get_room(self._getClosestRoom(state, unsearched_rooms, self._current_door))[-1][
                                'doormat']
                        if self._door['room_name'] == 'area 1':
                            self._doormat = (3, 5)
                        self._phase = Phase.PLAN_PATH_TO_ROOM

            if Phase.PLAN_PATH_TO_ROOM == self._phase:
                # Reset the navigator for a new path planning
                self._navigator.reset_full()

                # Check if there is a goal victim, and it has been found, but its location is not known
                if self._goal_vic \
                        and self._goal_vic in self._known_victims \
                        and 'location' not in self._known_victim_logs[self._goal_vic].keys():
                    # Retrieve the victim's room location and related information
                    victim_location = self._known_victim_logs[self._goal_vic]['room']
                    self._door = state.get_room_doors(victim_location)[0]
                    self._doormat = state.get_room(victim_location)[-1]['doormat']

                    # Handle special case for 'area 1'
                    if self._door['room_name'] == 'area 1':
                        self._doormat = (3, 5)

                    # Set the door location based on the doormat
                    doorLoc = self._doormat

                # If the goal victim's location is known, plan the route to the identified area
                else:
                    if self._door['room_name'] == 'area 1':
                        self._doormat = (3, 5)
                    doorLoc = self._doormat

                # Add the door location as a waypoint for navigation
                self._navigator.add_waypoints([doorLoc])
                # Follow the route to the next area to search
                self._phase = Phase.FOLLOW_PATH_TO_ROOM

            if Phase.FOLLOW_PATH_TO_ROOM == self._phase:
                # Check if the previously identified target victim was rescued by the human
                if self._goal_vic and self._goal_vic in self._collected_victims:
                    # Reset current door and switch to finding the next goal
                    self._current_door = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Check if the human found the previously identified target victim in a different room
                if self._goal_vic \
                        and self._goal_vic in self._known_victims \
                        and self._door['room_name'] != self._known_victim_logs[self._goal_vic]['room']:
                    self._current_door = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Check if the human already searched the previously identified area without finding the target victim
                if self._door['room_name'] in self._explored_rooms and self._goal_vic not in self._known_victims:
                    self._current_door = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Move to the next area to search
                else:
                    # Update the state tracker with the current state
                    self._state_tracker.update(state)

                    # Explain why the agent is moving to the specific area, either:
                    # [-] it contains the current target victim
                    # [-] it is the closest un-searched area
                    if self._goal_vic in self._known_victims \
                            and str(self._door['room_name']) == self._known_victim_logs[self._goal_vic]['room'] \
                            and not self._remove:
                        # Use human help if they are capable and willing to rescue or if the victim is critically injured as the RescueBot cannot carry alone
                        if 'critical' in self._goal_vic or trustBeliefs[self._human_name]['rescue'][
                            'competence'] > 0.2 and \
                                trustBeliefs[self._human_name]['rescue']['willingness'] > 0.0:
                            self._send_message('Moving to ' + str(
                                self._door['room_name']) + ' to pick up ' + self._goal_vic + ' together with you.',
                                               'RescueBot')
                        else:
                            self._send_message(
                                'Moving to ' + str(self._door['room_name']) + ' to pick up ' + self._goal_vic + '.',
                                'RescueBot')

                    if self._goal_vic not in self._known_victims and not self._remove or not self._goal_vic and not self._remove:
                        self._send_message(
                            'Moving to ' + str(self._door['room_name']) + ' because it is the closest unsearched area.',
                            'RescueBot')

                    # Set the current door based on the current location
                    self._current_door = self._door['location']

                    # Retrieve move actions to execute
                    action = self._navigator.get_move_action(self._state_tracker)
                    # Check for obstacles blocking the path to the area and handle them if needed
                    if action is not None:
                        # Remove obstacles blocking the path to the area 
                        for info in state.values():
                            if 'class_inheritance' in info and 'ObstacleObject' in info[
                                'class_inheritance'] and 'stone' in info['obj_id'] and info['location'] not in [(9, 4),
                                                                                                                (9, 7),
                                                                                                                (9, 19),
                                                                                                                (21,
                                                                                                                 19)]:
                                self._send_message('Reaching ' + str(self._door['room_name'])
                                                   + ' will take a bit longer because I found stones blocking my path.',
                                                   'RescueBot')
                                return RemoveObject.__name__, {'object_id': info['obj_id']}
                        return action, {}
                    # Identify and remove obstacles if they are blocking the entrance of the area
                    self._phase = Phase.REMOVE_OBSTACLE_IF_NEEDED

            if Phase.REMOVE_OBSTACLE_IF_NEEDED == self._phase:
                objects = []
                agent_location = state[self.agent_id]['location']
                # Identify which obstacle is blocking the entrance
                for info in state.values():
                    if 'class_inheritance' in info and 'ObstacleObject' in info['class_inheritance'] and 'rock' in info[
                        'obj_id']:
                        objects.append(info)
                        # Check whether the wait time is up
                        if self._waiting and is_waiting_over(self._started_waiting_tick, self._tick,
                                                             self._waiting_time):
                            self._answered = True
                            self._waiting = False
                            self._send_message('Waiting is over. Continuing search.', 'RescueBot')
                            self._confirmed_human_info['search'].append(
                                {'event': InfoEvent.WAIT_OVER, 'obstacle': Obstacle.ROCK,
                                 'location': self._door['room_name']})
                            self._phase = Phase.ENTER_ROOM
                            self._remove = False
                            return None, {}
                        # Communicate which obstacle is blocking the entrance
                        if self._answered == False and not self._remove and not self._waiting:
                            self._waiting = True
                            self._started_waiting_tick = self._tick
                            self._waiting_time = calculate_wait_time(self._distance_human,
                                                                     trustBeliefs[self._human_name]['search'], must_be_done_together=True)
                            self._send_message('Found rock blocking ' + str(self._door['room_name']) + '. Please decide whether to "Remove" or "Continue" searching. \n \n \
                                Important features to consider are: \n safe - victims rescued: ' + str(
                                self._collected_victims) + ' \n explore - areas searched: area ' + str(
                                self._explored_rooms).replace('area ', '') + ' \
                                \n clock - removal time: 5 seconds \n afstand - distance between us: ' + self._distance_human + f'\n clock - maximum waiting time: {self._waiting_time} seconds.',
                                               'RescueBot')
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Continue' and not self._remove:
                            self._answered = True
                            self._waiting = False
                            # Add area to the to do list
                            self._to_search.append(self._door['room_name'])
                            self._phase = Phase.FIND_NEXT_GOAL
                        # Wait for the human to help removing the obstacle and remove the obstacle together
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Remove' or self._remove:
                            if not self._remove:
                                self._answered = True
                            # Tell the human to come over and be idle until human arrives
                            if not state[{'is_human_agent': True}]:
                                self._send_message(
                                    'Please come to ' + str(self._door['room_name']) + ' to remove rock.',
                                    'RescueBot')
                                if not self._waiting and self._remove:
                                    self._waiting = True
                                    self._started_waiting_tick = self._tick
                                    self._waiting_time = calculate_wait_time(self._distance_human, trustBeliefs[self._human_name]['search'], must_be_done_together=True)
                                    self._send_message(f"clock - maximum waiting time: {self._waiting_time} seconds.",
                                                       "RescueBot")
                            # Tell the human to remove the obstacle when he/she arrives
                            if state[{'is_human_agent': True}]:
                                self._send_message('Lets remove rock blocking ' + str(self._door['room_name']) + '!',
                                                   'RescueBot')
                                if not self._waiting and self._remove:
                                    self._waiting = True
                                    self._started_waiting_tick = self._tick
                                    self._waiting_time = 10 # player is close, so we only wait 10 seconds
                                    self._send_message(f"clock - maximum waiting time: {self._waiting_time} seconds.",
                                                       "RescueBot")
                        # Remain idle until the human communicates what to do with the identified obstacle
                        return None, {}

                    if 'class_inheritance' in info and 'ObstacleObject' in info['class_inheritance'] and 'tree' in info[
                        'obj_id']:
                        objects.append(info)
                        # check whether the waiting is over
                        if self._waiting and is_waiting_over(self._started_waiting_tick, self._tick,
                                                             self._waiting_time):
                            self._answered = True
                            self._waiting = False
                            self._send_message('Waiting is over. Removing tree alone.', 'RescueBot')
                            self._confirmed_human_info['search'].append(
                                {'event': InfoEvent.WAIT_OVER, 'obstacle': Obstacle.TREE,
                                 'location': self._door['room_name']})
                            self._phase = Phase.ENTER_ROOM
                            self._remove = False
                            return RemoveObject.__name__, {'object_id': info['obj_id']}

                        # Communicate which obstacle is blocking the entrance
                        if self._answered == False and not self._remove and not self._waiting:
                            self._waiting = True
                            self._started_waiting_tick = self._tick
                            self._waiting_time = calculate_wait_time(self._distance_human,
                                                                     trustBeliefs[self._human_name]['search'])
                            self._send_message('Found tree blocking  ' + str(self._door['room_name']) + '. Please decide whether to "Remove" or "Continue" searching. \n \n \
                                Important features to consider are: \n safe - victims rescued: ' + str(
                                self._collected_victims) + '\n explore - areas searched: area ' + str(
                                self._explored_rooms).replace('area ', '') + ' \
                                \n clock - removal time: 10 seconds' + f'\n clock - maximum waiting time: {self._waiting_time} seconds.',
                                               'RescueBot')
                        # Determine the next area to explore if the human tells the agent not to remove the obstacle
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Continue' and not self._remove:
                            self._answered = True
                            self._waiting = False
                            # Add area to the to do list
                            self._to_search.append(self._door['room_name'])
                            self._phase = Phase.FIND_NEXT_GOAL
                        # Remove the obstacle if the human tells the agent to do so
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Remove' or self._remove:
                            # Tell the human to come over and be idle until human arrives
                            if not self._remove:
                                self._answered = True
                                self._waiting = False
                                self._send_message('Removing tree blocking ' + str(self._door['room_name']) + '.',
                                                   'RescueBot')
                            if self._remove:
                                self._send_message('Removing tree blocking ' + str(
                                    self._door['room_name']) + ' because you asked me to.', 'RescueBot')
                            self._phase = Phase.ENTER_ROOM
                            self._remove = False
                            return RemoveObject.__name__, {'object_id': info['obj_id']}
                        # Remain idle until the human communicates what to do with the identified obstacle
                        else:
                            return None, {}

                    if 'class_inheritance' in info and 'ObstacleObject' in info['class_inheritance'] and 'stone' in \
                            info['obj_id']:
                        objects.append(info)
                        # Check if waiting time is over
                        if self._waiting and is_waiting_over(self._started_waiting_tick, self._tick,
                                                             self._waiting_time):
                            self._answered = True
                            self._waiting = False
                            self._send_message('Waiting is over. Removing stone alone.', 'RescueBot')
                            self._confirmed_human_info['search'].append(
                                {'event': InfoEvent.WAIT_OVER, 'obstacle': Obstacle.STONE,
                                 'location': self._door['room_name']})
                            self._phase = Phase.ENTER_ROOM
                            self._remove = False
                            return RemoveObject.__name__, {'object_id': info['obj_id']}

                        # Communicate which obstacle is blocking the entrance
                        if self._answered == False and not self._remove and not self._waiting:
                            self._waiting = True
                            self._started_waiting_tick = self._tick
                            self._waiting_time = calculate_wait_time(self._distance_human,
                                                                     trustBeliefs[self._human_name]['search'])
                            self._send_message('Found stones blocking  ' + str(self._door['room_name']) + '. Please decide whether to "Remove together", "Remove alone", or "Continue" searching. \n \n \
                                Important features to consider are: \n safe - victims rescued: ' + str(
                                self._collected_victims) + ' \n explore - areas searched: area ' + str(
                                self._explored_rooms).replace('area', '') + ' \
                                \n clock - removal time together: 3 seconds \n afstand - distance between us: ' + self._distance_human + '\n clock - removal time alone: 20 seconds' + f'\n clock - maximum waiting time: {self._waiting_time} seconds.',
                                               'RescueBot')
                        # Determine the next area to explore if the human tells the agent not to remove the obstacle          
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Continue' and not self._remove:
                            self._answered = True
                            self._waiting = False
                            # Add area to the to do list
                            self._to_search.append(self._door['room_name'])
                            self._phase = Phase.FIND_NEXT_GOAL
                        # Remove the obstacle alone if the human decides so
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Remove alone' and not self._remove:
                            self._answered = True
                            self._waiting = False
                            self._send_message('Removing stones blocking ' + str(self._door['room_name']) + '.',
                                               'RescueBot')
                            self._phase = Phase.ENTER_ROOM
                            self._remove = False
                            return RemoveObject.__name__, {'object_id': info['obj_id']}

                        # Remove the obstacle together if the human decides so
                        if self.received_messages_content and self.received_messages_content[
                            -1] == 'Remove together' or self._remove:
                            if not self._remove:
                                self._answered = True
                            # Tell the human to come over and be idle until human arrives
                            if not state[{'is_human_agent': True}]:
                                self._send_message(
                                    'Please come to ' + str(self._door['room_name']) + ' to remove stones together.',
                                    'RescueBot')
                                if not self._waiting and self._remove:
                                    self._waiting = True
                                    self._started_waiting_tick = self._tick
                                    self._waiting_time = calculate_wait_time(self._distance_human,
                                                                             trustBeliefs[self._human_name]['search'])
                                    self._send_message(f"clock - maximum waiting time: {self._waiting_time} seconds.",
                                                       "RescueBot")
                            # Tell the human to remove the obstacle when he/she arrives
                            if state[{'is_human_agent': True}]:
                                self._send_message('Lets remove stones blocking ' + str(self._door['room_name']) + '!',
                                                   'RescueBot')
                                if not self._waiting and self._remove:
                                    self._waiting = True
                                    self._started_waiting_tick = self._tick
                                    self._waiting_time = 10 # player is close, so we only wait 10 seconds
                                    self._send_message(f"clock - maximum waiting time: {self._waiting_time} seconds.",
                                                       "RescueBot")

                        return None, {}
                # If no obstacles are blocking the entrance, enter the area
                if len(objects) == 0:
                    self._answered = False
                    self._remove = False
                    self._waiting = False
                    self._phase = Phase.ENTER_ROOM

            if Phase.ENTER_ROOM == self._phase:
                self._answered = False

                # Check if the target victim has been rescued by the human, and switch to finding the next goal
                if self._goal_vic in self._collected_victims:
                    self._current_door = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Check if the target victim is found in a different area, and start moving there
                if self._goal_vic in self._known_victims \
                        and self._door['room_name'] != self._known_victim_logs[self._goal_vic]['room']:
                    self._current_door = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Check if area already searched without finding the target victim, and plan to search another area
                if self._door['room_name'] in self._explored_rooms and self._goal_vic not in self._known_victims:
                    self._current_door = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Enter the area and plan to search it
                else:
                    self._state_tracker.update(state)

                    action = self._navigator.get_move_action(self._state_tracker)
                    # If there is a valid action, return it; otherwise, plan to search the room
                    if action is not None:
                        return action, {}
                    self._phase = Phase.PLAN_ROOM_SEARCH_PATH

            if Phase.PLAN_ROOM_SEARCH_PATH == self._phase:
                # Extract the numeric location from the room name and set it as the agent's location
                self._agent_loc = int(self._door['room_name'].split()[-1])

                # Store the locations of all area tiles in the current room
                room_tiles = [info['location'] for info in state.values()
                              if 'class_inheritance' in info
                              and 'AreaTile' in info['class_inheritance']
                              and 'room_name' in info
                              and info['room_name'] == self._door['room_name']]
                self._roomtiles = room_tiles

                # Make the plan for searching the area
                self._navigator.reset_full()
                self._navigator.add_waypoints(self._efficientSearch(room_tiles))

                # Initialize variables for storing room victims and switch to following the room search path
                self._room_vics = []
                self._phase = Phase.FOLLOW_ROOM_SEARCH_PATH

            if Phase.FOLLOW_ROOM_SEARCH_PATH == self._phase:
                # Search the area
                self._state_tracker.update(state)
                action = self._navigator.get_move_action(self._state_tracker)
                if action != None:
                    # Identify victims present in the area
                    for info in state.values():
                        if 'class_inheritance' in info and 'CollectableBlock' in info['class_inheritance']:
                            vic = str(info['img_name'][8:-4])
                            # Remember which victim the agent found in this area
                            if vic not in self._room_vics:
                                self._room_vics.append(vic)

                            # Identify the exact location of the victim that was found by the human earlier
                            if vic in self._known_victims and 'location' not in self._known_victim_logs[vic].keys():
                                self._recent_vic = vic
                                # Add the exact victim location to the corresponding dictionary
                                self._known_victim_logs[vic] = {'location': info['location'],
                                                                'room': self._door['room_name'],
                                                                'obj_id': info['obj_id']}
                                if vic == self._goal_vic:
                                    # Communicate which victim was found
                                    self._send_message('Found ' + vic + ' in ' + self._door[
                                        'room_name'] + ' because you told me ' + vic + ' was located here.',
                                                       'RescueBot')
                                    # Robot confirmed the human information
                                    self._confirmed_human_info['rescue'].append(
                                        {'event': InfoEvent.FOUND, 'victim': vic, 'location': self._door['room_name']})
                                    # Add the area to the list with searched areas
                                    if self._door['room_name'] not in self._explored_rooms:
                                        self._explored_rooms.append(self._door['room_name'])
                                    # Do not continue searching the rest of the area but start planning to rescue the victim
                                    self._phase = Phase.FIND_NEXT_GOAL

                            # Identify injured victim in the area
                            if 'healthy' not in vic and not self._found_victims[vic]:
                                self._found_victims[vic].append(self._tick)
                                self._found_victims_logs[vic].append({'location': info['location'],
                                                                      'room': self._door['room_name'],
                                                                      'obj_id': info['obj_id'],
                                                                      'tick': self._tick})
                            if 'healthy' not in vic and vic not in self._known_victims:
                                self._recent_vic = vic
                                # Add the victim and the location to the corresponding dictionary
                                self._known_victims.append(vic)
                                self._known_victim_logs[vic] = {'location': info['location'],
                                                                'room': self._door['room_name'],
                                                                'obj_id': info['obj_id']}
                                # Communicate which victim the agent found and ask the human whether to rescue the victim now or at a later stage
                                # Start waiting for an answer
                                if 'mild' in vic and self._answered == False and not self._waiting:
                                    self._waiting = True
                                    self._started_waiting_tick = self._tick
                                    self._waiting_time = calculate_wait_time(self._distance_human,
                                                                             trustBeliefs[self._human_name]['rescue'])
                                    self._send_message('Found ' + vic + ' in ' + self._door['room_name'] + '. Please decide whether to "Rescue together", "Rescue alone", or "Continue" searching. \n \n \
                                        Important features to consider are: \n safe - victims rescued: ' + str(
                                        self._collected_victims) + '\n explore - areas searched: area ' + str(
                                        self._explored_rooms).replace('area ', '') + '\n \
                                        clock - extra time when rescuing alone: 15 seconds \n afstand - distance between us: ' + self._distance_human + f'\n clock - maximum waiting time: {self._waiting_time} seconds.',
                                                       'RescueBot')

                                # Start waiting for an answer
                                if 'critical' in vic and self._answered == False and not self._waiting:
                                    self._waiting = True
                                    self._started_waiting_tick = self._tick
                                    self._waiting_time = calculate_wait_time(self._distance_human,
                                                                             trustBeliefs[self._human_name]['rescue'],
                                                                             must_be_done_together=True)
                                    self._send_message('Found ' + vic + ' in ' + self._door['room_name'] + '. Please decide whether to "Rescue" or "Continue" searching. \n\n \
                                        Important features to consider are: \n explore - areas searched: area ' + str(
                                        self._explored_rooms).replace('area',
                                                                      '') + ' \n safe - victims rescued: ' + str(
                                        self._collected_victims) + '\n \
                                        afstand - distance between us: ' + self._distance_human + f'\n clock - maximum waiting time: {self._waiting_time} seconds.',
                                                       'RescueBot')
                                    # Execute move actions to explore the area
                    return action, {}

                # Communicate that the agent did not find the target victim in the area while the human previously communicated the victim was located here
                if self._goal_vic in self._known_victims and self._goal_vic not in self._room_vics and \
                        self._known_victim_logs[self._goal_vic]['room'] == self._door['room_name']:
                    self._send_message(self._goal_vic + ' not present in ' + str(self._door[
                                                                                     'room_name']) + ' because I searched the whole area without finding ' + self._goal_vic + '.',
                                       'RescueBot')
                    # Robot confirmed the human information
                    self._confirmed_human_info['rescue'].append(
                        {'event': InfoEvent.NOT_FOUND, 'victim': self._goal_vic, 'location': self._door['room_name']})
                    # Remove the victim location from memory
                    self._known_victim_logs.pop(self._goal_vic, None)
                    self._known_victims.remove(self._goal_vic)
                    self._room_vics = []
                    # Reset received messages (bug fix)
                    self.received_messages = []
                    self.received_messages_content = []
                    self._phase = Phase.FIND_NEXT_GOAL
                    return None, {}
                # Add the area to the list of searched areas
                if self._door['room_name'] not in self._explored_rooms:
                    self._explored_rooms.append(self._door['room_name'])
                if not self._searched_rooms[self._door['room_name']]:
                    self._searched_rooms[self._door['room_name']].append({'type': 'Robot', 'tick': self._tick})

                # Check if the robot requested a Rescue type message and emit a false rescue if not
                if self.received_messages_content and self.received_messages_content[-1] in {'Rescue',
                                                                                             'Rescue together',
                                                                                             'Rescue alone'} and not self._recent_vic:
                    self._confirmed_human_info['rescue'].append(
                        {'event': InfoEvent.FALSE_RESCUE, 'victim': None, 'location': self._door['room_name']})
                    self._phase = Phase.FIND_NEXT_GOAL
                    return None, {}

                # Make a plan to rescue a found critically injured victim if the human decides so
                if self.received_messages_content and self.received_messages_content[
                    -1] == 'Rescue' and self._recent_vic and 'critical' in self._recent_vic:
                    self._rescue = 'together'
                    self._answered = True
                    self._waiting = False
                    # Tell the human to come over and help carry the critically injured victim
                    if not state[{'is_human_agent': True}]:
                        self._send_message('Please come to ' + str(self._door['room_name']) + ' to carry ' + str(
                            self._recent_vic) + ' together.', 'RescueBot')
                    # Tell the human to carry the critically injured victim together
                    if state[{'is_human_agent': True}]:
                        self._send_message('Lets carry ' + str(
                            self._recent_vic) + ' together! Please wait until I moved on top of ' + str(
                            self._recent_vic) + '.', 'RescueBot')
                    self._goal_vic = self._recent_vic
                    self._recent_vic = None
                    self._phase = Phase.PLAN_PATH_TO_VICTIM
                # Make a plan to rescue a found mildly injured victim together if the human decides so
                if self.received_messages_content and self.received_messages_content[
                    -1] == 'Rescue together' and self._recent_vic and 'mild' in self._recent_vic:
                    self._rescue = 'together'
                    self._answered = True
                    self._waiting = False
                    # Tell the human to come over and help carry the mildly injured victim
                    if not state[{'is_human_agent': True}]:
                        self._send_message('Please come to ' + str(self._door['room_name']) + ' to carry ' + str(
                            self._recent_vic) + ' together.', 'RescueBot')
                    # Tell the human to carry the mildly injured victim together
                    if state[{'is_human_agent': True}]:
                        self._send_message('Lets carry ' + str(
                            self._recent_vic) + ' together! Please wait until I moved on top of ' + str(
                            self._recent_vic) + '.', 'RescueBot')
                    self._goal_vic = self._recent_vic
                    self._recent_vic = None
                    self._phase = Phase.PLAN_PATH_TO_VICTIM
                # Make a plan to rescue the mildly injured victim alone if the human decides so, and communicate this to the human
                if self.received_messages_content and self.received_messages_content[
                    -1] == 'Rescue alone' and self._recent_vic and 'mild' in self._recent_vic:
                    self._send_message('Picking up ' + self._recent_vic + ' in ' + self._door['room_name'] + '.',
                                       'RescueBot')
                    self._rescue = 'alone'
                    self._answered = True
                    self._waiting = False
                    self._goal_vic = self._recent_vic
                    self._goal_loc = self._remaining[self._goal_vic]
                    self._recent_vic = None
                    self._phase = Phase.PLAN_PATH_TO_VICTIM

                # Continue searching other areas if the human decides so
                if self.received_messages_content and self.received_messages_content[-1] == 'Continue':
                    self._answered = True
                    self._waiting = False
                    self._todo.append(self._recent_vic)
                    self._recent_vic = None
                    self._phase = Phase.FIND_NEXT_GOAL

                # Check whether the waiting time expired
                if not self._carrying_together and self._waiting and is_waiting_over(self._started_waiting_tick, self._tick, self._waiting_time):
                    self._waiting = False
                    self._answered = True
                    self._confirmed_human_info['rescue'].append(
                        {'event': InfoEvent.WAIT_OVER, 'victim': self._recent_vic, 'location': self._door['room_name']})
                    if 'mild' in self._recent_vic:
                        self._send_message(f'Waiting is over. Rescuing {self._recent_vic} alone.', 'RescueBot')
                        self._rescue = 'alone'
                        self._answered = True
                        self._waiting = False
                        self._goal_vic = self._recent_vic
                        self._goal_loc = self._remaining[self._goal_vic]
                        self._recent_vic = None
                        self._phase = Phase.PLAN_PATH_TO_VICTIM
                    elif 'critical' in self._recent_vic:
                        self._send_message('Waiting is over. Continuing search.', 'RescueBot')
                        self._todo.append(self._recent_vic)
                        self._recent_vic = None
                        self._phase = Phase.FIND_NEXT_GOAL
                        return None, {}

                # Remain idle until the human communicates to the agent what to do with the found victim
                if self.received_messages_content and self._waiting and self.received_messages_content[
                    -1] != 'Rescue' and self.received_messages_content[-1] != 'Continue':
                    return None, {}

                # Find the next area to search when the agent is not waiting for an answer from the human or occupied with rescuing a victim
                if not self._waiting and not self._rescue:
                    self._recent_vic = None
                    self._phase = Phase.FIND_NEXT_GOAL
                return Idle.__name__, {'duration_in_ticks': 25}

            if Phase.PLAN_PATH_TO_VICTIM == self._phase:
                # Plan the path to a found victim using its location
                self._navigator.reset_full()
                self._navigator.add_waypoints([self._known_victim_logs[self._goal_vic]['location']])
                # Follow the path to the found victim
                self._phase = Phase.FOLLOW_PATH_TO_VICTIM

            if Phase.FOLLOW_PATH_TO_VICTIM == self._phase:
                # Start searching for other victims if the human already rescued the target victim
                if self._goal_vic and self._goal_vic in self._collected_victims:
                    self._phase = Phase.FIND_NEXT_GOAL

                # Move towards the location of the found victim
                else:
                    self._state_tracker.update(state)

                    action = self._navigator.get_move_action(self._state_tracker)
                    # If there is a valid action, return it; otherwise, switch to taking the victim
                    if action is not None:
                        return action, {}
                    self._phase = Phase.TAKE_VICTIM

            if Phase.TAKE_VICTIM == self._phase:
                # Store all area tiles in a list
                room_tiles = [info['location'] for info in state.values()
                              if 'class_inheritance' in info
                              and 'AreaTile' in info['class_inheritance']
                              and 'room_name' in info
                              and info['room_name'] == self._known_victim_logs[self._goal_vic]['room']]
                self._roomtiles = room_tiles
                objects = []
                # When the victim has to be carried by human and agent together, check whether human has arrived at the victim's location
                for info in state.values():
                    # When the victim has to be carried by human and agent together, check whether human has arrived at the victim's location
                    if 'class_inheritance' in info and 'CollectableBlock' in info['class_inheritance'] and 'critical' in \
                            info['obj_id'] and info['location'] in self._roomtiles or \
                            'class_inheritance' in info and 'CollectableBlock' in info[
                        'class_inheritance'] and 'mild' in info['obj_id'] and info[
                        'location'] in self._roomtiles and self._rescue == 'together' or \
                            self._goal_vic in self._known_victims and self._goal_vic in self._todo and len(
                        self._explored_rooms) == 0 and 'class_inheritance' in info and 'CollectableBlock' in info[
                        'class_inheritance'] and 'critical' in info['obj_id'] and info['location'] in self._roomtiles or \
                            self._goal_vic in self._known_victims and self._goal_vic in self._todo and len(
                        self._explored_rooms) == 0 and 'class_inheritance' in info and 'CollectableBlock' in info[
                        'class_inheritance'] and 'mild' in info['obj_id'] and info['location'] in self._roomtiles:
                        objects.append(info)
                        # Remain idle when the human has not arrived at the location or until the waiting time expires
                        if not self._carrying_together and self._waiting and is_waiting_over(self._started_waiting_tick, self._tick, self._waiting_time):
                            self._waiting = False
                            self._answered = True
                            victim_name = info['obj_id'].split('_in_')[0].replace('_', ' ')
                            self._confirmed_human_info['rescue'].append(
                                {'event': InfoEvent.WAIT_OVER, 'victim': victim_name,
                                 'location': self._door['room_name']})

                            if 'mild' in victim_name:
                                self._rescue = 'alone'
                                self._send_message(f'Waiting is over. Rescuing {victim_name} alone.', 'RescueBot')
                                self._confirmed_human_info['rescue'].append(
                                    {'event': InfoEvent.WAIT_OVER, 'victim': self._goal_vic,
                                     'location': self._door['room_name']})
                                self._goal_loc = self._remaining[self._goal_vic]
                                return None, {}
                            else:
                                self._send_message('Waiting is over. Continuing search.', 'RescueBot')
                                self._confirmed_human_info['rescue'].append(
                                    {'event': InfoEvent.WAIT_OVER, 'victim': self._goal_vic,
                                     'location': self._door['room_name']})
                                self._phase = Phase.FIND_NEXT_GOAL
                                return None, {}

                        # Start waiting
                        if not self._human_name in info['name'] and not self._waiting:
                            self._waiting = True
                            self._started_waiting_tick = self._tick
                            self._waiting_time = calculate_wait_time(self._distance_human,
                                                                     trustBeliefs[self._human_name]['rescue'],
                                                                     must_be_done_together=('critical' in self._goal_vic))
                            self._moving = False
                            self._send_message(f"clock - maximum waiting time: {self._waiting_time}", "RescueBot")
                            return None, {}
                        return None, {}
                # Add the victim to the list of rescued victims when it has been picked up
                if len(objects) == 0 and 'critical' in self._goal_vic or len(
                        objects) == 0 and 'mild' in self._goal_vic and self._rescue == 'together':
                    self._waiting = False
                    if self._goal_vic not in self._collected_victims:
                        self._collected_victims.append(self._goal_vic)
                    self._carrying_together = True
                    self._waiting = False
                    # Add event for collecting victim together
                    self._confirmed_human_info['rescue'].append(
                        {'event': InfoEvent.COLLECT, 'victim': self._goal_vic, 'location': self._door['room_name']})
                    # Determine the next victim to rescue or search
                    self._phase = Phase.FIND_NEXT_GOAL
                # When rescuing mildly injured victims alone, pick the victim up and plan the path to the drop zone
                if 'mild' in self._goal_vic and self._rescue == 'alone':
                    self._phase = Phase.PLAN_PATH_TO_DROPPOINT
                    if self._goal_vic not in self._collected_victims:
                        self._collected_victims.append(self._goal_vic)
                    self._carrying = True
                    return CarryObject.__name__, {'object_id': self._known_victim_logs[self._goal_vic]['obj_id'],
                                                  'human_name': self._human_name}

            if Phase.PLAN_PATH_TO_DROPPOINT == self._phase:
                self._navigator.reset_full()
                # Plan the path to the drop zone
                self._navigator.add_waypoints([self._goal_loc])
                # Follow the path to the drop zone
                self._phase = Phase.FOLLOW_PATH_TO_DROPPOINT

            if Phase.FOLLOW_PATH_TO_DROPPOINT == self._phase:
                # Communicate that the agent is transporting a mildly injured victim alone to the drop zone
                if 'mild' in self._goal_vic and self._rescue == 'alone':
                    self._send_message('Transporting ' + self._goal_vic + ' to the drop zone.', 'RescueBot')
                self._state_tracker.update(state)
                # Follow the path to the drop zone
                action = self._navigator.get_move_action(self._state_tracker)
                if action is not None:
                    return action, {}
                # Drop the victim at the drop zone
                self._phase = Phase.DROP_VICTIM

            if Phase.DROP_VICTIM == self._phase:
                # Communicate that the agent delivered a mildly injured victim alone to the drop zone
                if 'mild' in self._goal_vic and self._rescue == 'alone':
                    self._send_message('Delivered ' + self._goal_vic + ' at the drop zone.', 'RescueBot')
                # Add event for dropping victim together
                if self._rescue == 'together':
                    self._confirmed_human_info['rescue'].append(
                        {'event': InfoEvent.DELIVER, 'victim': self._goal_vic, 'location': self._door['room_name']})
                # Identify the next target victim to rescue

                self._phase = Phase.FIND_NEXT_GOAL
                self._rescue = None
                self._current_door = None
                self._tick = state['World']['nr_ticks']
                self._carrying = False
                # Drop the victim on the correct location on the drop zone
                return Drop.__name__, {'human_name': self._human_name}

    def _get_drop_zones(self, state):
        """
        @return list of drop zones (their full dict), in order (the first one is the
        place that requires the first drop)
        """
        places = state[{'is_goal_block': True}]
        places.sort(key=lambda info: info['location'][1])
        zones = []
        for place in places:
            if place['drop_zone_nr'] == 0:
                zones.append(place)
        return zones

    def _process_messages(self, state, teamMembers, condition):
        """
        process incoming messages received from the team members
        """
        receivedMessages = {}
        # Create a dictionary with a list of received messages from each team member
        for member in teamMembers:
            receivedMessages[member] = []
        for i, mssg in enumerate(self.received_messages):
            for member in teamMembers:
                if mssg.from_id == member:
                    receivedMessages[member].append((mssg.content, i))
        # Check the content of the received messages
        for mssgs in receivedMessages.values():
            for msg, i in mssgs:
                # If a received message involves team members searching areas, add these areas to the memory of areas that have been explored
                if msg.startswith("Search:"):
                    area = 'area ' + msg.split()[-1]
                    if area not in self._explored_rooms:
                        self._explored_rooms.append(area)
                    if {'type': 'Human', 'tick': self._received_messages[(msg, i)]} not in self._searched_rooms[area]:
                        self._searched_rooms[area].append({'type': 'Human', 'tick': self._received_messages[(msg, i)]})
                # If a received message involves team members finding victims, add these victims and their locations to memory
                if msg.startswith("Found:"):
                    # Identify which victim and area it concerns
                    if len(msg.split()) == 6:
                        foundVic = ' '.join(msg.split()[1:4])
                    else:
                        foundVic = ' '.join(msg.split()[1:5])
                    loc = 'area ' + msg.split()[-1]
                    # Add the area to the memory of searched areas
                    if loc not in self._explored_rooms:
                        self._explored_rooms.append(loc)

                    if self._received_messages[(msg, i)] not in self._found_victims[foundVic]:
                        self._found_victims[foundVic].append(self._received_messages[(msg, i)])
                        self._found_victims_logs[foundVic].append(
                            {'room': loc, 'tick': self._received_messages[(msg, i)]})

                    # Add the victim and its location to memory
                    if foundVic not in self._known_victims:
                        self._known_victims.append(foundVic)
                        self._known_victim_logs[foundVic] = {'room': loc}
                    if foundVic in self._known_victims and self._known_victim_logs[foundVic]['room'] != loc:
                        self._known_victim_logs[foundVic] = {'room': loc}
                    # Decide to help the human carry a found victim when the human's condition is 'weak'
                    if self._get_condition() == 'weak':
                        self._rescue = 'together'
                    # Add the found victim to the to do list when the human's condition is not 'weak'
                    if 'mild' in foundVic and self._get_condition() != 'weak':
                        self._todo.append(foundVic)
                # If a received message involves team members rescuing victims, add these victims and their locations to memory
                if msg.startswith('Collect:'):
                    # Identify which victim and area it concerns
                    if len(msg.split()) == 6:
                        collectVic = ' '.join(msg.split()[1:4])
                    else:
                        collectVic = ' '.join(msg.split()[1:5])
                    loc = 'area ' + msg.split()[-1]
                    # Add the area to the memory of searched areas
                    if loc not in self._explored_rooms:
                        self._explored_rooms.append(loc)
                    # Add the victim and location to the memory of found victims
                    if collectVic not in self._known_victims:
                        self._known_victims.append(collectVic)
                        self._known_victim_logs[collectVic] = {'room': loc}
                    if collectVic in self._known_victims and self._known_victim_logs[collectVic]['room'] != loc:
                        self._known_victim_logs[collectVic] = {'room': loc}
                    # Add the victim to the memory of rescued victims when the human's condition is not weak
                    if self._get_condition() != 'weak' and collectVic not in self._collected_victims:
                        self._collected_victims.append(collectVic)
                    # Decide to help the human carry the victim together when the human's condition is weak or if the human's condition is normal and they are close
                    if self._get_condition() == 'weak' or self._get_condition() == 'normal' and self._human_loc == 'close':
                        self._rescue = 'together'
                # If a received message involves team members asking for help with removing obstacles, add their location to memory and come over
                if msg.startswith('Remove:'):
                    # Come over immediately when the agent is not carrying a victim
                    if not self._carrying:
                        # Identify at which location the human needs help
                        area = 'area ' + msg.split()[-1]
                        self._door = state.get_room_doors(area)[0]
                        self._doormat = state.get_room(area)[-1]['doormat']
                        if area in self._explored_rooms:
                            self._explored_rooms.remove(area)
                        # Clear received messages (bug fix)
                        self.received_messages = []
                        self.received_messages_content = []
                        self._moving = True
                        self._remove = True
                        if self._waiting and self._recent_vic:
                            self._todo.append(self._recent_vic)
                        self._waiting = False
                        # Let the human know that the agent is coming over to help
                        self._send_message(
                            'Moving to ' + str(self._door['room_name']) + ' to help you remove an obstacle.',
                            'RescueBot')
                        # Plan the path to the relevant area
                        self._phase = Phase.PLAN_PATH_TO_ROOM
                    # Come over to help after dropping a victim that is currently being carried by the agent
                    else:
                        area = 'area ' + msg.split()[-1]
                        self._send_message('Will come to ' + area + ' after dropping ' + self._goal_vic + '.',
                                           'RescueBot')
            # Store the current location of the human in memory
            if mssgs and mssgs[-1][0].split()[-1] in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12',
                                                      '13',
                                                      '14']:
                self._human_loc = int(mssgs[-1][0].split()[-1])

    def _get_condition(self):
        """
        Gets the human condition based on the average competence in search and rescue.
        """
        average_competence = (self._trust_belief[self._human_name]['search']['competence'] +
                              self._trust_belief[self._human_name]['rescue']['competence']) / 2
        if average_competence < -0.2:
            return 'weak'
        elif average_competence < 0.2:
            return 'normal'
        return 'strong'

    def _loadBelief(self, members, folder):
        """
        Loads trust belief values if agent already collaborated with human before, otherwise trust belief values are initialized using default values.
        """
        # Create a dictionary with trust values for all team members
        trustBeliefs = defaultdict(dict)
        # Set a default starting trust value
        trustfile_header = []
        trustfile_contents = []
        # Check if agent already collaborated with this human before, if yes: load the corresponding trust values, if no: initialize using default trust values
        with open(folder + '/beliefs/allTrustBeliefs.csv') as csvfile:
            reader = csv.reader(csvfile, delimiter=';', quotechar="'")
            for row in reader:
                if not trustfile_header:
                    trustfile_header = row
                    continue
                # Retrieve trust values
                if row and row[0] == self._human_name:
                    name = row[0]
                    task = row[1]
                    competence = float(row[2])
                    willingness = float(row[3])
                    trustBeliefs[name][task] = {'competence': competence, 'willingness': willingness}
        # Initialize with default values if not initialized yet
        for task in self._tasks:
            if not trustBeliefs[self._human_name] or not trustBeliefs[self._human_name].get(task):
                competence = self._base_trust_beliefs[task]['competence']
                willingness = self._base_trust_beliefs[task]['willingness']
                trustBeliefs[self._human_name][task] = {'competence': competence, 'willingness': willingness}
        return trustBeliefs

    def _trustBelief(self, members, trustBeliefs, folder, receivedMessages):
        """
        Creates a dictionary with trust belief scores for each team member.
        Does not change the beliefs for ALWAYS_TRUST, NEVER_TRUST and RANDOM_TRUST.
        """
        # Do not change baseline trust
        if self._human_name in self._reserved_names:
            return

        # Save current trust belief values so we can later use and retrieve them to add to a csv file with all the logged trust belief values
        # Update the trust value based on for example the received messages
        previous_message, previous_message_tick = None, 0

        for i, ((message, pos), message_tick) in enumerate(receivedMessages.items()):
            if 'Collect' in message:
                task = 'rescue'
                message_tokens = message.split(" in ")
                victim_location = "area " + message_tokens[1]
                victim_name = message_tokens[0].replace("Collect: ", "")
                if not self._found_victims[victim_name] or (
                        self._found_victims[victim_name] and self._found_victims[victim_name][0] >= message_tick):
                    log_info(self._message_count == i, "Victim collected but not found")
                    competence_adj, willingness_adj = compute_collected_adjustments(victim_name, -0.1)
                    self._change_belief(competence_adj, willingness_adj, task, trustBeliefs)
                elif self._found_victims[victim_name][0] < message_tick:
                    found_location = None
                    for log in self._found_victims_logs[victim_name]:
                        if log['tick'] < message_tick:
                            found_location = log['room']

                    if found_location != victim_location:
                        log_info(self._message_count == i,
                                 "Victim collected but found in another location. Rescue ability and willingness decrease")
                        competence_adj, willingness_adj = compute_collected_adjustments(victim_name, -0.05)
                        self._change_belief(competence_adj, willingness_adj, task, trustBeliefs)
                    else:
                        log_info(self._message_count == i,
                                 "Victim collected and found in right location. Rescue ability and willingness increase")
                        competence_adj, willingness_adj = compute_collected_adjustments(victim_name, 0.1)
                        self._change_belief(competence_adj, willingness_adj, task, trustBeliefs)

                if victim_location not in self._searched_rooms or not self._searched_rooms[victim_location]:
                    log_info(self._message_count == i,
                             "Victim location not found in searched rooms. Skipping trust adjustment to prevent errors.")
                else:
                    if self._searched_rooms[victim_location][0]['tick'] >= message_tick:
                        log_info(self._message_count == i,
                                 "Victim collected but location was not searched. Search and rescue ability and willingness decrease")
                        self._change_belief(-0.12, -0.12, 'search', trustBeliefs)
                        self._change_belief(-0.12, -0.12, 'rescue', trustBeliefs)


            elif 'Search' in message:
                task = 'search'
                message_tokens = message.split(" ")
                search_location = "area " + message_tokens[1]

                if search_location not in self._searched_rooms or (
                        search_location in self._searched_rooms and self._searched_rooms[search_location][0][
                    'tick'] >= message_tick):
                    log_info(self._message_count == i,
                             "Location searched for the first time. Search ability and willingness increase")
                    self._change_belief(0.05, 0.08, task, trustBeliefs)
                else:
                    search_type = None
                    for event in self._searched_rooms[search_location]:
                        if event['tick'] == message_tick:
                            search_type = event['type']
                            break

                    log_info(self._message_count == i,
                             "Location was searched before. Search ability and willingness decrease")
                    competence_adj, willingness_adj = (-0.1, -0.1) if search_type == 'Human' else (
                        -0.15, -0.15)
                    self._change_belief(competence_adj, willingness_adj, task, trustBeliefs)

            elif 'Found' in message:
                task = 'rescue'
                message_tokens = message.split(" in ")
                location = "area " + message_tokens[-1]
                victim_name = message_tokens[0].replace("Found: ", "")
                if (victim_name not in self._found_victims) or (
                        victim_name in self._found_victims and message_tick <= self._found_victims[victim_name][0]):
                    log_info(self._message_count == i,
                             f"Found {victim_name} in {location}. Rescue willingness increases")
                    self._change_belief(0.0, 0.05, task, trustBeliefs)
                else:
                    same_victim_reported_twice_at_different_location = False
                    for found_log in self._found_victims_logs[victim_name]:
                        if found_log['tick'] >= message_tick:
                            break
                        if found_log['room'] != location:
                            same_victim_reported_twice_at_different_location = True
                            break
                    if same_victim_reported_twice_at_different_location:
                        log_info(self._message_count == i,
                                 f"{victim_name} reported twice at different locations. Rescue ability and willingness decrease")
                        self._change_belief(-0.12, -0.12, task, trustBeliefs)
                    else:
                        log_info(self._message_count == i,
                                 f"{victim_name} reported twice at the same location. Rescue ability and willingness decrease, but with a small amount")
                        self._change_belief(-0.05, -0.05, task, trustBeliefs)

                if not self._searched_rooms[location] or (self._searched_rooms[location][0]['tick'] >= message_tick):
                    log_info(self._message_count == i,
                             f"Found a victim in unsearched room {location}. Rescue and search ability and willingness decrease")
                    self._change_belief(-0.12, -0.12, 'search', trustBeliefs)
                    self._change_belief(-0.12, -0.12, 'rescue', trustBeliefs)

            elif 'Remove' in message:
                task = 'search'
                if message == "Remove alone":
                    log_info(self._message_count == i,
                             f"Remove alone. Nothing happens, as the robot will do it itself")
                elif message == "Remove together":
                    log_info(self._message_count == i,
                             f"Player wants to help removing the obstacle. Search ability and willingness")
                    self._change_belief(0.05, 0.15, task, trustBeliefs)
                elif message == "Remove":
                    log_info(self._message_count == i,
                             f"Player wants to remove the tree. Search willingness slightly increases")
                    self._change_belief(0.0, 0.05, task, trustBeliefs)
                else:
                    location = "area" + message.replace("Remove: at", "")
                    log_info(self._message_count == i,
                             f"Search willingness increases for wanting help to remove")
                    self._change_belief(0.0, 0.1, task, trustBeliefs)
                    if not self._searched_rooms[location] or (
                            self._searched_rooms[location][0]['tick'] >= message_tick):
                        log_info(self._message_count == i,
                                 f"Search ability and willingness decrease for asking for remove help in unsearched room {location}")
                        self._change_belief(-0.12, -0.12, 'search', trustBeliefs)

            if 'Rescue' in message:
                task = 'rescue'
                if message == "Rescue alone":
                    log_info(self._message_count == i,
                             f"Rescue alone. Nothing happens, as the robot will do it itself")
                elif message == "Rescue together" or message == "Rescue":
                    log_info(self._message_count == i,
                             f"Player wants to help rescuing the victim. Rescue ability and willingness")
                    self._change_belief(0.12, 0.12, task, trustBeliefs)

            elif 'Continue' in message:
                for task in self._tasks:
                    self._change_belief(0.0, -0.1, task, trustBeliefs)

            if previous_message:
                previous_message_info = self._task_information.get(message.split(" ")[0])
                if previous_message_info and message_tick - previous_message_tick < previous_message_info[
                    'expected_time_to_complete']:
                    log_info(self._message_count == i,
                             f"The previous task was likely not finished. Ability and willingness for {previous_message_info['task']} decrease")
                    self._change_belief(-0.1, -0.2, previous_message_info['task'], trustBeliefs)

            previous_message = message
            previous_message_tick = message_tick

        # Penalize the human for not providing information to the robot in a long time.
        total_decay = self._decay_trust(receivedMessages)
        self.apply_trust_decay(total_decay, -0.25, trustBeliefs)

        # Save to CSV
        with open(folder + '/beliefs/currentTrustBelief.csv', mode='w') as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            csv_writer.writerow(['name', 'task', 'competence', 'willingness'])
            for task in self._tasks:
                csv_writer.writerow(
                    [self._human_name, task, trustBeliefs[self._human_name][task]['competence'],
                     trustBeliefs[self._human_name][task]['willingness']])

        self._message_count = len(receivedMessages)

        self.update_trust_from_confirmed_info(trustBeliefs)

        return trustBeliefs

    def update_trust_from_confirmed_info(self, trustBeliefs):
        # This function is used to update the trust scores based on the confirmed human information that the RescueBot has
        # received. This information is stored in the self._confirmed_human_info dictionary.
        # Take each case and adapt slightly the trust scores based on that
        for i, (task, events) in enumerate(self._confirmed_human_info.items()):
            for event in events:
                # Case 1: The human correctly reported a victim location so the human competence improves and the
                # willingness is slightly adjusted because of the human first having to search before actually finding
                if event['event'] == InfoEvent.FOUND:
                    log_info(self._confirmed_info_map_length <= i,
                             f"Trust Update: Human correctly reported victim location - {event['victim']} in {event['location']}")
                    self._change_belief(0.1, 0.05, 'rescue', trustBeliefs)

                # Case 2: The human incorrectly reported a victim location  so their competence decreases
                elif event['event'] == InfoEvent.NOT_FOUND:
                    log_info(self._confirmed_info_map_length <= i,
                             f"Trust Update: Human incorrectly reported victim location - {event['victim']} not found in {event['location']}")

                    self._change_belief(-0.15, 0, 'rescue', trustBeliefs)  # Reduce competence

                # Case 3: The human didn't respond in time so the willingness should be lower
                elif event['event'] == InfoEvent.WAIT_OVER:
                    if event.get('victim'):
                        log_info(self._confirmed_info_map_length <= i,
                                 f"Trust Update: Human did not respond in time for victim - {event['victim']} in {event['location']}")

                        self._change_belief(0, -0.1, 'rescue', trustBeliefs)  # Reduce willingness
                    elif event.get('obstacle'):
                        log_info(self._confirmed_info_map_length <= i,
                                 f"Trust Update: Human did not respond in time for obstacle - {event['obstacle']} in {event['location']}")

                        self._change_belief(0, -0.1, 'search', trustBeliefs)

                # Case 4: The human reported a false rescue so the competence decreases and the willingness is
                # slightly decreased
                elif event['event'] == InfoEvent.FALSE_RESCUE:
                    log_info(self._confirmed_info_map_length <= i,
                             f"Trust Update: Human falsely reported a rescue - No victim found in {event['location']}")
                    self._change_belief(-0.2, -0.1, 'rescue', trustBeliefs)  # Strong penalty for misinformation

                # Case 5: The human successfully delivered a victim to safety so the competence increases and
                # the willingness is slightly increased
                elif event['event'] == InfoEvent.DELIVER:
                    log_info(self._confirmed_info_map_length <= i,
                             f"Trust Update: Human successfully delivered victim - {event['victim']} to safety")
                    self._change_belief(0.2, 0.1, 'rescue', trustBeliefs)

                # Case 6: The human helped remove an obstacle so the competence increases and the willingness is
                # also increased
                elif event['event'] == InfoEvent.REMOVE:
                    log_info(self._confirmed_info_map_length <= i,
                             f"Trust Update: Human helped remove an obstacle - {event['obstacle']} at {event['location']}")
                    self._change_belief(0.1, 0.1, 'search', trustBeliefs)

                # Case 7: The human successfully collected a victim so the competence increases and the willingness
                # is also increased slightly less
                elif event['event'] == InfoEvent.COLLECT:
                    log_info(self._confirmed_info_map_length <= i,
                             f"Trust Update: Human successfully collected victim - {event['victim']} in {event['location']}")
                    self._change_belief(0.15, 0.1, 'rescue', trustBeliefs)

        self._confirmed_info_map_length = len(self._confirmed_human_info.values())

    def apply_trust_decay(self, total_decay, min_val, trustBeliefs):
        for task in self._tasks:
            new_willingness = trustBeliefs[self._human_name][task]['willingness']
            new_competence = trustBeliefs[self._human_name][task]['competence']
            if new_willingness > min_val:
                new_willingness = np.clip(trustBeliefs[self._human_name][task]['willingness'] - total_decay, min_val,
                                          1.0)
            if new_competence > min_val:
                new_competence = np.clip(trustBeliefs[self._human_name][task]['competence'] - total_decay, min_val, 1.0)
            trustBeliefs[self._human_name][task] = {'competence': new_competence, 'willingness': new_willingness}

    def _change_belief(self, competence_adjustment: float, willingness_adjustment: float, task: str,
                       trust_beliefs: dict, min_val=-1.0, max_val=1.0):
        new_competence = np.clip(trust_beliefs[self._human_name][task]['competence'] + competence_adjustment, min_val,
                                 max_val)
        new_willingness = np.clip(trust_beliefs[self._human_name][task]['willingness'] + willingness_adjustment,
                                  min_val,
                                  max_val)
        trust_beliefs[self._human_name][task] = {'competence': new_competence, 'willingness': new_willingness}

    def _send_message(self, mssg, sender):
        """
        send messages from agent to other team members
        """
        msg = Message(content=mssg, from_id=sender)
        if msg.content not in self.received_messages_content and 'Our score is' not in msg.content:
            self.send_message(msg)
            self._send_messages.append(msg.content)
        # Sending the hidden score message (DO NOT REMOVE)
        if 'Our score is' in msg.content:
            self.send_message(msg)

    def _getClosestRoom(self, state, objs, currentDoor):
        """
        calculate which area is closest to the agent's location
        """
        agent_location = state[self.agent_id]['location']
        locs = {}
        for obj in objs:
            locs[obj] = state.get_room_doors(obj)[0]['location']
        dists = {}
        for room, loc in locs.items():
            if currentDoor != None:
                dists[room] = utils.get_distance(currentDoor, loc)
            if currentDoor == None:
                dists[room] = utils.get_distance(agent_location, loc)

        return min(dists, key=dists.get)

    def _efficientSearch(self, tiles):
        """
        efficiently transverse areas instead of moving over every single area tile
        """
        x = []
        y = []
        for i in tiles:
            if i[0] not in x:
                x.append(i[0])
            if i[1] not in y:
                y.append(i[1])
        locs = []
        for i in range(len(x)):
            if i % 2 == 0:
                locs.append((x[i], min(y)))
            else:
                locs.append((x[i], max(y)))
        return locs
