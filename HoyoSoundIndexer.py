# Opens the GUI, or runs the CLI when given --scan, --harvest or --vo-online.

import multiprocessing

from src.cli import build_parser, run_cli, wants_cli


def main():
    args = build_parser().parse_args()
    if wants_cli(args):
        run_cli(args)
        return
    # Imported here so the CLI and the harvest workers never load Qt.
    from src.gui.app import run_gui
    run_gui()


if __name__ == "__main__":
    # The harvest's worker processes re-run this file in the frozen exe.
    multiprocessing.freeze_support()
    main()
