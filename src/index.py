from collections import defaultdict

from src.wwise.hirc import ACTION_SET_STATE, ACTION_SET_SWITCH

_MAX_PARENT_CHAIN = 64


class ScanIndex:
    def __init__(self):
        self.wem_locations = defaultdict(list)
        self.external_locations = defaultdict(list)
        self.bank_ids = set()

        self.object_ids = set()
        self.event_actions = {}
        self.event_banks = defaultdict(set)
        self.actions = {}
        self.action_syncs = {}
        self.dialogue_children = defaultdict(set)
        self.node_sources = defaultdict(set)
        self.parents = defaultdict(set)
        # Containers only: a per-character subtree hangs off one, and its bank names the character.
        self.object_banks = defaultdict(set)

        # Id -> kind of every state, switch, their groups, triggers and game parameters.
        self.sync_ids = {}
        self.named_objects = {}
        self.switch_containers = {}
        self.music_trees = {}
        self.dialogue_trees = {}

        # Filled by build_wems_below and build_sync_links once the hierarchy is complete.
        self.wems_below = {}
        self.sync_wems = {}
        # Wem id -> the (group, value) pairs of the selectors above it.
        self.wem_tags = {}
        # Event id -> the (group, value) pairs its actions set, with value 0 for triggers.
        self.event_tags = {}
        self.group_values = {}

        # Filled by label_characters once the names are resolved.
        self.wem_characters = {}
        self.character_banks = {}
        # Every spelling of a character met by the resolve -> the name shown for it.
        self.character_names = {}
        # The words naming an avatar -> its shown name, in event names, in voice paths and as a voice file's speaker.
        # Voice paths say sunna where the events say summer, and Xianyun's NPC lines say liuyun.
        self.avatar_codenames = {}
        self.avatar_voice_names = {}
        self.avatar_file_speakers = {}
        # Wem id -> milliseconds, filled by read_durations for the GUI.
        self.wem_durations = {}

        self.stats = defaultdict(int)

    # Credits a sound's wems to every node of its first-parent chain and to the other parents of those.
    # Two parents are rare, and one extra level reaches both subtrees.
    def build_wems_below(self):
        below = defaultdict(set)
        for node, sources in self.node_sources.items():
            chain = {node}
            current = node
            for _ in range(_MAX_PARENT_CHAIN):
                parents = self.parents.get(current)
                if not parents:
                    break
                current = next(iter(parents))
                if current in chain or current == 0:
                    break
                chain.add(current)
            extra = set()
            for member in list(chain):
                for parent in self.parents.get(member, ()):
                    if parent and parent not in chain:
                        extra.add(parent)
            chain |= extra
            for member in chain:
                below[member] |= sources
        self.wems_below = dict(below)

    def wems_under(self, node):
        return set(self.wems_below.get(node, ())) | set(self.node_sources.get(node, ()))

    # What the event's actions target, plus what its dialogue tree selects.
    def wems_for_event(self, event_id):
        wems = set()
        targets = set()
        for action_id in self.event_actions.get(event_id, ()):
            action = self.actions.get(action_id)
            if action and action.target:
                targets.add(action.target)
        targets |= self.dialogue_children.get(event_id, set())
        for target in targets:
            wems |= self.wems_below.get(target, set())
            wems |= self.node_sources.get(target, set())
        return wems

    def wems_for_object(self, oid):
        if oid in self.event_actions or oid in self.dialogue_children:
            return sorted(self.wems_for_event(oid))
        if oid in self.object_ids:
            return sorted(set(self.wems_below.get(oid, set())) | set(self.node_sources.get(oid, set())))
        return []

    # Biggest first: a bank often holds only the 0.1 s prefetch stub of a streamed wem.
    def locations_of(self, wem_id):
        locations = self.wem_locations.get(wem_id, []) + self.external_locations.get(wem_id, [])
        return sorted(locations, key=lambda location: location.size, reverse=True)

    # Links every selector's group and value ids to the wems they choose, which become the wems' tags.
    # A SetState or SetSwitch action tags its event instead, since the event holds no audio.
    def build_sync_links(self):
        sync_wems = defaultdict(set)
        wem_tags = defaultdict(set)
        group_values = defaultdict(set)

        def link(group_id, value_id, wems):
            if not group_id:
                return
            if value_id:
                group_values[group_id].add(value_id)
            if not wems:
                return
            sync_wems[group_id] |= wems
            if value_id:
                sync_wems[value_id] |= wems
                for wem_id in wems:
                    wem_tags[wem_id].add((group_id, value_id))

        for container in self.switch_containers.values():
            for value_id, nodes in container.assignments.items():
                wems = set()
                for node in nodes:
                    wems |= self.wems_under(node)
                link(container.group_id, value_id, wems)
        for trees in (self.music_trees, self.dialogue_trees):
            for tree in trees.values():
                for keys, target in tree.leaves:
                    wems = self.wems_under(target) if target else set()
                    for group_id, key in zip(tree.groups, keys):
                        link(group_id, key, wems)

        event_tags = defaultdict(set)
        for event_id, action_ids in self.event_actions.items():
            for action_id in action_ids:
                touched = self.action_syncs.get(action_id)
                action = self.actions.get(action_id)
                if not touched or not action:
                    continue
                if action.type in (ACTION_SET_STATE, ACTION_SET_SWITCH) and len(touched) == 2:
                    event_tags[event_id].add((touched[0], touched[1]))
                    group_values[touched[0]].add(touched[1])
                else:
                    event_tags[event_id].update((sync_id, 0) for sync_id in touched)
        self.sync_wems = dict(sync_wems)
        self.wem_tags = dict(wem_tags)
        self.event_tags = dict(event_tags)
        self.group_values = dict(group_values)
