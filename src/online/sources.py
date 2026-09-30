# The repos vanish now and then (DMCA), so config.json can override a source under "voice_sources".
ONLINE_SOURCES = {
    "GI": {
        "label": "Genshin - Dimbreath/animegamedata2",
        "project": "Dimbreath/animegamedata2",
        "ref": "main",
        "voice_subtree": "BinOutput/Voice",
        "audio_subtree": "BinOutput/Audio",
        "avatar_cfg": ["ExcelBinOutput/AvatarExcelConfigData.json"],
        # The avatar names resolve only in the medium text map, not in TextMapEN.
        "avatar_textmap": ["TextMap/TextMap_MediumEN.json"],
    },
    "ZZZ": {
        "label": "Zenless - dimbreath/ZenlessData (music titles)",
        "host": "https://git.mero.moe",
        "project": "dimbreath/ZenlessData",
        "ref": "master",
        "music_cfg": "FileCfg/MusicPlayerConfigTemplateTb.json",
        # The only table where both spellings of a character meet, like zhenzhen and Ye Shunguang.
        "avatar_cfg": ["FileCfg/AvatarBaseTemplateTb.json"],
        "textmap": "TextMap/TextMap_ENTemplateTb.json",
        "textmap_overwrite": "TextMap/TextMap_ENOverwriteTemplateTb.json",
        # These carry readable Play_ events and the state names of the BGM switches.
        "event_cfg_files": ["FileCfg/AudioEventTemplateTb.json",
                            "FileCfg/CustomSoundEventTemplateTb.json",
                            "FileCfg/DefaultSoundEventTemplateTb.json",
                            "FileCfg/SceneSoundConfigTemplateTb.json",
                            "FileCfg/BigSceneBGMTemplateTb.json",
                            "FileCfg/SmithyMusicConfigTemplateTb.json",
                            "FileCfg/MainCityBGMConfigTemplateTb.json"],
        # Every other table is scanned raw for state and switch name candidates.
        "cfg_scan_dir": "FileCfg",
    },
    "SR": {
        "label": "Star Rail - Dimbreath/turnbasedgamedata",
        "project": "Dimbreath/turnbasedgamedata",
        "ref": "main",
        "voice_files": ["ExcelOutput/VoiceConfig.json"],
        # Only small high-yield tables: Level and LevelOutput run over 1 GB.
        "event_files": ["ExcelOutput/VoiceAtlas.json", "ExcelOutput/ChimeraTalk.json",
                        "Config/AudioConfig.json"],
        "event_subtrees": ["Config/ConfigAnimEvents", "Config/ConfigFreeStyle", "Config/Props"],
        "event_prefixes": "Ev,Play,Stop,Set",
        # The voice tag of a row is the speaker the voice files spell (mar7th, sam).
        "avatar_cfg": ["ExcelOutput/AvatarConfig.json", "ExcelOutput/AvatarConfigLD.json"],
        "avatar_textmap": ["TextMap/TextMapEN.json"],
    },
}


def source_for(game, config=None):
    override = ((config or {}).get("voice_sources") or {}).get(game)
    return override or ONLINE_SOURCES.get(game)


def source_short_name(game):
    project = (ONLINE_SOURCES.get(game) or {}).get("project", "")
    return project.split("/")[-1] if project else "—"
