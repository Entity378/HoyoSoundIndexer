# Usage: python HoyoSoundIndexer.py --scan <folder> --names <txt>, with the options below.

import argparse
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

from src.config import APP_NAME, load_config
from src.harvest import harvest_folder
from src.names_io import export_json, export_txt, read_names_file
from src.online.data import load_online_data
from src.pipeline import resolve_all_matches
from src.scan import scan_folder
from src.vocabulary import DEFAULT_HARVEST_PREFIXES
from src.voice.from_harvest import HarvestedVoice, import_vo_sources

# With output redirected to a file, one progress line every this many seconds.
_LOG_PROGRESS_SECONDS = 5
_PROGRESS_LINE_WIDTH = 90


def build_parser():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--scan", help="folder to scan (CLI mode)")
    parser.add_argument("--names", help="txt file with names")
    parser.add_argument("--sample", type=int, default=10, help="how many matches to print")
    parser.add_argument("--export-json", help="write matched events (with ids/wems/sources) to this json")
    parser.add_argument("--export-txt", help="write the clean matched event-name list to this txt")
    parser.add_argument("--harvest", help="folder to raw-scan for candidate event names (e.g. the game Blocks folder)")
    parser.add_argument("--harvest-prefixes", default=DEFAULT_HARVEST_PREFIXES,
                        help=f"comma-separated name prefixes (default: {DEFAULT_HARVEST_PREFIXES})")
    parser.add_argument("--harvest-out", help="also write the raw candidate list to this txt")
    parser.add_argument("--vo-out", help="write harvested voice prefixes/names to this json")
    parser.add_argument("--vo-in", help="reuse voice prefixes/names from this json")
    parser.add_argument("--vo-import", help="folder/file of game-config json to pull voice paths (*.wem) from")
    parser.add_argument("--vo-online", choices=["GI", "SR", "ZZZ"],
                        help="download & use online voice-name data (Dimbreath repos) for this game")
    parser.add_argument("--vo-online-refresh", action="store_true",
                        help="force re-download of the online voice data (ignore the local cache)")
    parser.add_argument("--harvest-game", choices=["ZZZ", "GI", "SR"],
                        help="decrypt .blk asset bundles for this game (Blocks folder) instead of raw scanning")
    parser.add_argument("--no-crack", action="store_true",
                        help="skip the deep name cracking (use a saved export as --names instead)")
    return parser


def wants_cli(args):
    return bool(args.scan or args.harvest or args.vo_online)


# A phase is a message with its numbers left out, so a growing count does not restart the timer.
class CliProgress:
    def __init__(self):
        self.interactive = sys.stdout.isatty()
        self.phase = None
        self.phase_start = time.time()
        self.last_logged = 0.0

    def __call__(self, done, total, message):
        now = time.time()
        phase = re.sub(r"\d+", "", message)
        if phase != self.phase:
            self.phase = phase
            self.phase_start = now
        percent = (done / total * 100) if total else 0
        elapsed = now - self.phase_start
        eta = (elapsed / done * (total - done)) if done else 0
        line = f"{message} {done}/{total} ({percent:.1f}%) {elapsed:.0f}s elapsed, ETA {eta:.0f}s"
        if self.interactive:
            sys.stdout.write("\r" + line.ljust(_PROGRESS_LINE_WIDTH))
            sys.stdout.flush()
        elif now - self.last_logged >= _LOG_PROGRESS_SECONDS or done >= total:
            self.last_logged = now
            print(line, flush=True)


def run_cli(args):
    progress = CliProgress()
    names, export, online, harvested_voice = _gather_inputs(args, progress)
    if not args.scan:
        return
    names = list(dict.fromkeys(names))
    index = scan_folder(args.scan, progress=progress)
    print()
    for key, value in sorted(index.stats.items()):
        print(f"  {key}: {value}")
    linked = sum(1 for event_id in index.event_actions if index.wems_for_event(event_id))
    print(f"  events with >=1 resolved wem: {linked}/{len(index.event_actions)}")

    matches, unmatched, counts = resolve_all_matches(
        index, names, args.scan, export=export, online=online, harvested_voice=harvested_voice,
        progress=progress, crack=not args.no_crack)
    _print_counts(index, counts)
    if matches or names:
        _print_matches(index, names, matches, unmatched)
        _write_exports(args, index, matches, unmatched)
        _print_sample(index, matches, args.sample)


# The harvested voice data comes from the last option giving any: --vo-import, --vo-in, --harvest.
# --vo-online adds names, labels and roster alongside, and its voice paths win when it has them.
def _gather_inputs(args, progress):
    names = []
    export = None
    online = None
    harvested_voice = None
    if args.names:
        file_names, export = read_names_file(args.names)
        names.extend(file_names)
    if args.vo_import:
        imported = import_vo_sources(args.vo_import, progress=progress)
        harvested_voice = HarvestedVoice([], imported)
        print(f"  voice paths imported: {len(imported)}")
    if args.vo_in:
        harvested_voice = HarvestedVoice.load(args.vo_in)
        print(f"  voice data loaded: {len(harvested_voice.prefixes)} prefixes, "
              f"{len(harvested_voice.sources)} voice names")
    if args.vo_online:
        data = load_online_data(args.vo_online, config=load_config(), force=args.vo_online_refresh,
                                progress=progress)
        online = data
        names.extend(data.names)
        print(f"  online data [{args.vo_online}]: {len(data.voice_paths)} voice, {len(data.names)} names, "
              f"{len(data.id_names)} id-labels, {len(data.state_candidates)} cfg candidates "
              f"(updated {data.updated})")
    if args.harvest:
        started = time.time()
        result = harvest_folder(args.harvest, args.harvest_prefixes.split(","), progress=progress,
                                game=args.harvest_game)
        harvested_voice = result.voice
        print()
        print(f"  harvested: {len(result.names)} names, {len(result.voice.prefixes)} voice prefixes, "
              f"{len(result.voice.sources)} voice names in {time.time() - started:.0f}s")
        if args.harvest_out:
            Path(args.harvest_out).write_text("\n".join(result.names) + "\n", encoding="utf-8")
            print(f"  candidates written -> {args.harvest_out}")
        if args.vo_out:
            result.voice.save(args.vo_out)
            print(f"  voice data written -> {args.vo_out}")
        names.extend(result.names)
    return names, export, online, harvested_voice


def _print_counts(index, counts):
    if counts.get("local_state_candidates"):
        print(f"\n  local state candidates: {counts['local_state_candidates']} (ZZZ data blocks)")
    if "voices" in counts:
        externals = len(index.external_locations)
        percent = counts["voices"] / max(1, externals) * 100
        print(f"  voice names recovered: {counts['voices']} / {externals} externals ({percent:.1f}%)")
    if "labels" in counts:
        print(f"  audio labels applied: {counts['labels']} (MusicSegment & co.)")
    if counts.get("music_branches"):
        print(f"  music branches labeled: {counts['music_branches']}")
    if counts.get("characters"):
        print(f"  wems tagged with a character: {counts['characters']} "
              f"({len(set(index.wem_characters.values()))} characters, from their banks)")


def _print_matches(index, names, matches, unmatched):
    by_kind = defaultdict(int)
    with_wems = 0
    located = 0
    for m in matches:
        by_kind[m.kind] += 1
        if m.wem_ids:
            with_wems += 1
            if any(index.wem_locations.get(w) or index.external_locations.get(w) for w in m.wem_ids):
                located += 1
    print(f"\n  names: {len(names)}  unmatched: {len(unmatched)}")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind} matches: {count}")
    print(f"  matches with wems: {with_wems} (located: {located})")


def _write_exports(args, index, matches, unmatched):
    if args.export_txt:
        count = export_txt(matches, args.export_txt)
        print(f"  exported {count} event names -> {args.export_txt}")
    if args.export_json:
        count = export_json(matches, unmatched, index, args.scan, _names_source(args), args.export_json)
        print(f"  exported {count} events -> {args.export_json}")


def _names_source(args):
    if args.names:
        return args.names
    if args.harvest:
        return f"harvest:{args.harvest}"
    if args.vo_online:
        return f"online:{args.vo_online}"
    return ""


def _print_sample(index, matches, count):
    for m in matches[:count]:
        places = []
        for wem_id in m.wem_ids[:4]:
            locations = index.locations_of(wem_id)
            places.append(f"{wem_id}@{locations[0].label()}" if locations else f"{wem_id}@?")
        print(f"    [{m.kind}] {m.name} -> {len(m.wem_ids)} wem  {places}")
