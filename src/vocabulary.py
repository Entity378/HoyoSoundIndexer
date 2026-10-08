# The words the indexer reasons with: name grammar, recovered names, characters, prefixes, voice paths.
# Algorithms and tuning numbers stay next to the code that uses them.

import re

# Verbs stripped from an event name before its tokens are recombined.
NAME_VERBS = ("play_", "set_state_", "set_", "stop_", "mute_")
# Tokens too generic to seed a candidate.
NAME_SKIP_TOKENS = ("play", "stop", "mute", "pause", "resume", "set")
# A name ending in one half suggests a sibling ending in the other.
NAME_PAIRS = (("start", "end"), ("open", "close"), ("on", "off"), ("in", "out"),
              ("good", "bad"), ("yes", "no"), ("enter", "exit"), ("first", "last"))
# Tails glued to every known sync stem: words the games append, then 00-30 and 0-10.
SYNC_TAIL_WORDS = ("yes", "no", "on", "off", "true", "false", "a", "b", "c", "start", "end",
                   "in", "out", "normal", "none", "good", "bad", "perfect")
SYNC_TAILS = (SYNC_TAIL_WORDS
              + tuple(f"{n:02d}" for n in range(31))
              + tuple(str(n) for n in range(11)))
# Glued in front of every candidate, since names like BGM_Combat exist only as code-side concatenations.
TAG_PREFIXES = ("State_", "StateGroup_", "BGM_", "Music_", "Switch_", "SwitchGroup_")
# The boilerplate around a group's subject, matched lowercase: SwitchGroup_AvatarSwitchAidAttackType.
GROUP_WRAPPERS = ("switchgroup_", "stategroup_", "switch_", "state_")
GROUP_DOMAINS = ("avatarswitch", "avatar", "monster", "char", "vo")
# States that only pad a music branch label.
FILLER_STATE_NAMES = ("none", "false", "true", "state_false", "state_true")

# Reverse engineered by hand: the {} slot is refilled with every filler of the family.
KNOWN_EVENT_TEMPLATES = ("play_vo_char_{}_charconfirm",)
# Sync names the game code composes at runtime, recovered by hand.
# state_battle_speed is HSR's combat speed toggle, whose Double_Speed branch holds sped-up battle VO.
KNOWN_SYNC_NAMES = ("vo_charconfirm", "vo_charconfirm_yes", "vo_charconfirm_no",
                    "shot1", "shot2", "state_crazytime", "farfromboss_state", "qte_state",
                    "state_battle_speed", "normal_speed", "double_speed", "none")


# ZZZ event families with a character slot, matched on lowercase names: play_vo_char_<agent>_<action>.
CHARACTER_SLOT_PATTERNS = (
    re.compile(r"^play_vo_char_([a-z0-9]+)_"),
    re.compile(r"^play_sfx_char_(?:foley|skill|impact)_([a-z0-9]+)_"),
)
# The synthetic tag group carrying the character, which the bank tells rather than a game sync.
CHARACTER_GROUP_NAME = "Character"
# A voice path spells its category and its speaker as Vo_ folders, the first and the last.
SPEAKER_FOLDER_PREFIX = "vo_"
# An event playing a voice line has vo among its words: play_vo_, HSR's ev_archive_vo_, GI's Play_Beyd_vo_.
VO_EVENT_PATTERN = re.compile(r"(?:^|_)vo(?:_|$)")
# Words an event name opens with before its family, as in Play_Sfx_ and HSR's Ev_vo_.
EVENT_VERB_WORDS = ("play", "ev", "stop", "set", "mute", "pause", "resume")

# Combat voice actions in game order, labelled like each game's wiki; None means nobody has named one yet.
# ZZZ keys are the suffix of the character slot, play_vo_char_<agent>_<key>.
ZZZ_VO_ACTIONS = (
    ("attacklight", "Basic Attack"),
    ("attackcharge", "Basic Attack (Charged)"),
    ("attackenhance", "Basic Attack (Enhanced)"),
    ("evade", "Dodge"),
    ("evadeback", "Dodge Back"),
    ("evadefront", "Dodge Forward"),
    ("evadesuccess", "Dodge (Success)"),
    ("reward", "Perfect Dodge"),
    ("attackrush", "Dash Attack"),
    ("attackcounter", "Dodge Counter"),
    ("battleswitch", "Assist"),
    ("battleswitch_bossstun", "Assist (Boss Stunned)"),
    ("battleswitch_reward", "Perfect Assist"),
    ("battleswitch_offplay", None),
    ("battleswitch_final", None),
    ("battleswitch_stun_hb", None),
    ("battleswitch_stun_lb", None),
    ("parry", "Defensive Assist"),
    ("followattack", "Assist Follow-Up"),
    ("attackbranch_a", "Special Attack"),
    ("attackbranch_a_charge", "Special Attack (Charged)"),
    ("attackbranch_b", "EX Special Attack"),
    ("attackbranch_b_charge", "EX Special Attack (Charged)"),
    ("attackbranch_c", None),
    ("overload", "Special State"),
    ("support", "Aftershock Attack"),
    ("qtestart", "Chain Attack Selection"),
    ("qte", "Chain Attack"),
    ("exqte", "Ultimate"),
    ("coattack_reward_qte", None),
    ("coattack_reward_branch_b", None),
    ("idle", "Idle"),
    ("encounterenemy", "Encounter Normal Enemy"),
    ("encounterelite", "Encounter Elite Enemy"),
    ("encounterboss", "Encounter Boss Enemy"),
    ("chargehint", "Charge Hint"),
    ("oshint", "Off-Screen Hint"),
    ("stunrecovery_first", "Enemy Stun Recovery"),
    ("stunrecovery_follow", "Enemy Stun Recovery (Subsequent)"),
    ("hit_l", "Light Hit Received"),
    ("hit_h", "Heavy Hit Received"),
    ("hit_blow", "Staggered"),
    ("death", "Defeated"),
    ("taunt", None),
    ("charselect", "Character Select"),
    ("charconfirm", "Character Confirm"),
)
# HSR voice events are a head, the action, then the avatar, as in ev_vo_avatar_turn_begin_kafka.
# The archive head is the profile copy of the same line, with its own wems.
HSR_VO_HEADS = ("ev_vo_avatar_", "ev_archive_vo_avatar_")
HSR_VO_ACTIONS = (
    ("advantage", "Battle Begins: Weakness Break"),
    ("high_threat", "Battle Begins: Danger Alert"),
    ("turn_begin", "Turn Begins"),
    ("waiting", "Turn Idling"),
    ("atk_cast", "Basic ATK"),
    ("skill_cast", "Skill"),
    ("hit_light", "Hit by Light Attack"),
    ("hit_heavy", "Hit by Heavy Attack"),
    ("ultra_skill_select", "Ultimate: Activate"),
    ("ultra_skill_cast", "Ultimate: Unleash"),
    ("passive_skill", "Talent"),
    ("die", "Downed"),
    ("revive", "Return to Battle"),
    ("healing", "Health Recovery"),
    ("atk_maze", "Overworld Basic ATK"),
    ("skill_maze", "Technique"),
    ("battle_victory_maze", "Battle Won"),
    ("open_chest_maze", "Treasure Opening"),
    ("open_preciouschest_maze", "Precious Treasure Opening"),
    ("solve_puzzle_maze", "Successful Puzzle-Solving"),
    ("lookat_threat_maze", "Enemy Target Found"),
    ("town_teleport_maze", "Returning to Town"),
    ("idleshow_maze", "Character Idles"),
    ("addtoteam", "Added to Team"),
    ("growth_eidolon_unlock", "Eidolon Activation"),
    ("growth_ascension_unlock", "Character Ascension"),
    ("growth_maxlevel_unlock", "Max Level Reached"),
    ("growth_trace_unlock", "Trace Activation"),
)
# GI keys are the middle of the VO_gameplay file names, vo_<speaker>_<key>_<take>.
GI_VO_ACTIONS = (
    ("battle_attacklight", "Light Attack"),
    ("battle_attackmid", "Mid Attack"),
    ("battle_attackheavy", "Heavy Attack"),
    ("battle_attack_attackplunging", "Plunging Attack"),
    ("battle_skill1", "Elemental Skill"),
    ("battle_skill2", None),
    ("battle_skill3", "Elemental Burst"),
    ("explore_sprint_start", "Sprint Start"),
    ("explore_sprint_end", "Sprint End"),
    ("explore_fly_start", "Deploying Wind Glider"),
    ("explore_fly_end", "Disengaging Wind Glider"),
    ("chest_open", "Opening Treasure Chest"),
    ("life_less30", "Low HP"),
    ("life_less30_teammate", "Ally at Low HP"),
    ("life_die", "Fallen"),
    ("battle_hit_l", "Light Hit Taken"),
    ("battle_hit_h", "Heavy Hit Taken"),
    ("teamjoin", "Joining Party"),
    ("explore_idle", "Character Idles"),
    ("standbyshow", "Character Idles (Standby)"),
    ("explore_climb", "Climbing"),
    ("explore_climb_breath", "Climbing Breath"),
    ("explore_jump", "Jumping"),
    ("explore_superjump", "Superjump"),
)

# Comma separated, as the CLI takes them.
DEFAULT_HARVEST_PREFIXES = "play,vo,stop,state,sfx,pause,resume,mute,trigger"
# HSR names its events Ev_ instead of Play_.
HARVEST_PREFIXES_BY_GAME = {"SR": "ev,play,vo,stop,state,sfx,music,amb,mute,trigger"}

# Voice-file categories: the first token of a file name, or the first folder of its path.
VO_CATEGORIES = (
    "Accompany", "Activity", "Breath", "Bubble", "Chat", "ChatPlus", "Chessboard", "Cinema",
    "Clue", "Comic", "Gal", "Galgame", "Level", "Maincity", "Maincity_NPC", "Maincity_ShopNpc",
    "Message", "Ongoing", "OngoingAntique", "OngoingCinema", "OngoingLevel", "OngoingMainCity",
    "Tips", "TL", "VoiceOnly", "NPC", "MainCity", "Timeline", "GalGame",
)
# Start of a file name -> its category folder, the longer starts tried before the shorter ones.
VO_LEAF_CATEGORIES = (
    ("galgame_", "vo_galgame"), ("comic_", "vo_comic"), ("chatplus_", "vo_chatplus"),
    ("timeline_", "vo_tl"), ("vo_cs", "vo_tl"), ("bubble", "vo_bubble"),
    ("vo_npc_", "vo_bubble"), ("cinema", "vo_cinema"), ("ongoingcinema_", "vo_cinema"),
    ("ongoinglevel_", "vo_level"), ("level_", "vo_level"), ("chessboard_", "vo_chessboard"),
    ("accompany_", "vo_accompany"), ("tips_", "vo_tips"), ("ongoing", "vo_ongoing"),
)
# Never fixed per game: calibration on the data keeps only the heads that hit.
VO_LANGUAGE_CANDIDATES = (
    "English(EN)", "English(US)", "English", "En",
    "Japanese(JP)", "Japanese", "Jp",
    "Chinese(PRC)", "Chinese", "Cn",
    "Korean(KR)", "Korean", "Kr",
)
# Protagonist lines carry _m or _f suffixes.
VO_SUFFIXES = ("", "_m", "_f")
# Language folders for the online hash rule, where extra values do no harm.
VO_ONLINE_LANGS = {
    "GI": ["English(US)", "Japanese", "Chinese", "Korean"],
    "SR": ["English", "Chinese", "Japanese", "Korean"],
    "ZZZ": ["English(EN)", "Japanese(JP)", "Chinese", "Korean"],
}
