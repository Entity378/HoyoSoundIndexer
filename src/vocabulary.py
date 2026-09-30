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


# Event families whose slot names the character, matched on lowercase names.
CHARACTER_EVENT_PATTERNS = (
    re.compile(r"^play_vo_char_([a-z0-9]+)_"),
    re.compile(r"^play_sfx_char_(?:foley|skill|impact)_([a-z0-9]+)_"),
)
# The synthetic tag group carrying the character, which the bank tells rather than a game sync.
CHARACTER_GROUP_NAME = "Character"
# An event playing a voice line, whatever verb comes first: play_vo_, vo_, HSR's ev_vo_.
VO_EVENT_PATTERN = re.compile(r"^(?:play_|stop_|ev_)?vo_")
# A voice path spells its category and its speaker as Vo_ folders, the first and the last.
SPEAKER_FOLDER_PREFIX = "vo_"

# Combat VO actions in suffix form and in game order, labelled like the community wiki.
# None means the game has the action but nobody has named it yet.
COMBAT_ACTIONS = (
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
