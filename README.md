# Cross‑Make CAN Message Translator

The **Cross‑Make CAN Message Translator** is a middleware framework written in
Python that allows you to map controller‑area network (CAN) messages from one
vehicle platform to another.  For example, you can send an *unlock* command on
a Toyota bus and have the translator automatically emit the equivalent BMW
message.  This removes the need to perform bespoke reverse engineering for
every new model you integrate.

The system is modular and deliberately verbose.  Each module is heavily
documented to aid understanding and extension:

* `can_interface.py` provides a hardware‑agnostic abstraction for interacting
  with CAN busses.  It includes concrete implementations based on
  [`socket`](https://docs.python.org/3/library/socket.html) (for Linux
  `socketcan` devices), optional [`python‑can`](https://python‑can.readthedocs.io/en/latest/)
  support, and an in‑memory mock for testing without hardware.
* `translation_table.py` defines the data structures used to map message
  identifiers, extract bit‑fields and apply scaling/offsets.  It includes
  utilities to load mappings from JSON.
* `translator_core.py` contains the high‑level logic: reading messages from
  a source bus, applying the translation rules, optionally performing adaptive
  fuzzing, and transmitting the translated frames on the target bus.
* `fuzzing.py` implements a rudimentary adaptive fuzzing strategy.  When
  activated, unknown messages are injected into the target bus with small
  variations in their payloads in order to discover new behaviours.  This
  component is intentionally conservative and is disabled by default.
* `gui.py` provides a graphical user interface (GUI) built with
  [`tkinter`](https://docs.python.org/3/library/tkinter.html).  The look
  consciously evokes early Macintosh/Classic Mac OS aesthetics with grey
  backgrounds, raised buttons and Chicago‑style fonts.  Through the GUI you
  can view captured messages in real time, edit translation entries on the
  fly and monitor the fuzzing subsystem.
* `main.py` ties everything together.  It loads the translation table,
  constructs the required interfaces, spawns the translator worker and
  launches the GUI on the main thread.

The system is designed to be **maximalist**: it favours explicitness over
implicit behaviour, detailed comments over brevity, and comprehensive error
handling.  It is targeted at researchers and practitioners who need full
control and visibility over their vehicle communications experiments.

## Caveats and Safety

Manipulating live vehicle networks can have **serious safety implications**.
This tool should only be used on test rigs or in controlled environments.
Remember that raw CAN frames are not human‑readable; decoding them into
engineering values requires a DBC file or prior reverse engineering.  In
practice, you will need to build a translation table by capturing traces on
both vehicles and identifying equivalent messages.  The translator merely
applies those mappings; it does not magically infer them on its own.  For
background on decoding raw CAN data and the role of DBC files, see CSS
Electronics' primer which explains that raw CAN data must be decoded into
physical values using information such as bit start, bit length, offset and
scaling parameters【417055235814080†L755-L786】.  DBC files are generally
proprietary and obtained from the original equipment manufacturer, so you
should ensure you are authorised to use them【417055235814080†L792-L820】.

## Quick Start

1. Ensure you have Python 3.8 or later.  Optional: install `python‑can` if
   you wish to use that backend.  If the library is not available, the
   translator will fall back to the low‑level `socketcan` implementation or
   operate entirely in mock mode.
2. Create a JSON file describing your translation rules.  Each entry should
   specify the source message ID, the destination ID, and how to extract and
   transform the relevant bits (see `translation_table.py` for details).
3. Run `python main.py --help` to see available command‑line options.  At a
   minimum you must provide the source and target interface names (e.g.
   `can0` and `can1` on Linux) and the path to your translation table.
4. The GUI will open and you will begin seeing raw frames as they arrive
   from the source bus.  If a frame has a mapping, its translated version
   will be sent out on the target bus.  You can edit mappings on the fly via
   the *Translation Table* window.

Please read the source code to understand the nuances of the translation
process and the limitations of the fuzzing implementation.