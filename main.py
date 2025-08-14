"""Entry point for the Cross‑Make CAN Message Translator.

This script ties together the various modules of the translator.  It
parses command‑line arguments to determine which CAN interfaces to use
for the source and target, loads a translation table from a JSON file,
instantiates the appropriate interface backends, constructs a
``CanTranslator`` and launches the GUI.

The default behaviour is deliberately conservative: if no translation
table is provided, the translator will run with an empty table and
therefore will not translate any messages.  Unknown messages will be
logged in the GUI and optionally passed to a fuzzing strategy if
configured via the ``--fuzz`` flag.  Users are expected to provide
their own translation rules via JSON; see the README for details.

Usage Example:

    python3 main.py --source can0 --target can1 --table my_table.json \
        --backend python-can --fuzz random

Command‑line options:

* ``--source``: required, name of the source interface (e.g. ``can0``).
* ``--target``: required, name of the target interface (e.g. ``can1``).
* ``--backend``: optional, force the backend for both interfaces
  (``socketcan``, ``python-can`` or ``mock``).  If omitted, the script
  chooses python‑can if installed, otherwise socketcan if available,
  otherwise mock.
* ``--table``: optional, path to a JSON file containing translation
  definitions.  Without this, no translations occur.
* ``--fuzz``: optional, name of the fuzzing strategy.  ``none`` (default)
  disables fuzzing; ``random`` enables the ``RandomByteFuzzer``.
* ``--bitrate``: optional integer specifying CAN bitrate when using
  socketcan or python‑can backends.  Note: changing bitrates requires
  appropriate permissions and may not be supported for all drivers.

Because this script launches a GUI, it must be run on a system with
graphics support.  On headless systems you may still use the core
translator classes without the GUI.
"""

import argparse
import sys

from .can_interface import get_interface
from .translation_table import TranslationTable
from .translator_core import CanTranslator
from .fuzzing import NullFuzzer, RandomByteFuzzer
from .gui import CanTranslatorGUI


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross‑Make CAN Message Translator")
    parser.add_argument('--source', required=True, help='Name of the source CAN interface (e.g. can0)')
    parser.add_argument('--target', required=True, help='Name of the target CAN interface (e.g. can1)')
    parser.add_argument('--backend', choices=['socketcan', 'python-can', 'mock'], help='Force a specific CAN backend')
    parser.add_argument('--table', help='Path to translation table JSON file')
    parser.add_argument('--fuzz', choices=['none', 'random'], default='none', help='Fuzzing strategy for unknown messages')
    parser.add_argument('--bitrate', type=int, default=None, help='Bitrate for CAN interfaces (bits per second)')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    # Instantiate CAN interfaces
    try:
        source = get_interface(args.source, backend=args.backend, bitrate=args.bitrate)
        target = get_interface(args.target, backend=args.backend, bitrate=args.bitrate)
    except Exception as ex:
        print(f"Error creating interfaces: {ex}")
        return 1
    # Load translation table
    if args.table:
        try:
            table = TranslationTable.from_json_file(args.table)
        except Exception as ex:
            print(f"Error loading translation table: {ex}")
            return 1
    else:
        table = TranslationTable()
    # Choose fuzzing strategy
    if args.fuzz == 'random':
        fuzzer = RandomByteFuzzer(num_random=3, flip_bits=True)
    else:
        fuzzer = NullFuzzer()
    # Create translator
    translator = CanTranslator(source=source, target=target, table=table, fuzzing=fuzzer)
    # Launch GUI
    gui = CanTranslatorGUI(translator)
    try:
        gui.run()
    finally:
        # Ensure translator is stopped on exit
        translator.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())