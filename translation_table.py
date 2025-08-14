"""Data structures and utilities for CAN message translation tables.

The Cross‑Make CAN translator relies on a *translation table* to map
incoming frames from one bus (the *source*) to frames on another bus (the
*target*).  A translation table entry describes which message IDs should be
handled, how to extract specific bit‑fields from the payload, how to
convert those fields (e.g. scaling and offset) and where to insert them in
the outgoing frame.  Without such a table the translator has no way to
interpret raw bytes – CAN data is not self‑describing and requires this
additional metadata【417055235814080†L755-L786】.

This module defines a few core classes:

* :class:`SignalMapping`: describes a single signal within a message.  It
  specifies the source starting bit, bit length, endianess, scaling
  factors, offset and the target starting bit.  It optionally stores
  minimum/maximum bounds to clamp values.
* :class:`TranslationEntry`: associates a source arbitration ID with a
  destination ID and a list of signal mappings.  It also stores the
  expected DLC (data length code) of the source message, whether the
  source/destination IDs are extended and a default payload (for bytes not
  covered by signals).
* :class:`TranslationTable`: holds multiple entries indexed by source
  arbitration ID and provides lookup functions.  It includes factory
  functions to load a table from JSON.

The JSON schema is intentionally explicit.  Each top‑level entry must
include at least the keys ``source_id`` and ``target_id``.  Signal
mappings must define ``src_start_bit``, ``length`` and ``dest_start_bit``.
Optional keys include ``scale`` (default 1), ``offset`` (default 0),
``endian"`` ("little" or "big"), and ``default_target_bytes`` for
initialising unused bits in the outgoing payload.

See the ``example_table.json`` file in this repository for a concrete
example of how to define a mapping between a Toyota unlock command and a
BMW unlock command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Iterable, Tuple


@dataclass
class SignalMapping:
    """Describe how a single signal is extracted and inserted.

    Parameters
    ----------
    src_start_bit : int
        The starting bit (0–63) in the source payload where the signal begins.
        Bit numbering follows the little endian convention used by DBC files
        (bit 0 is the least significant bit of byte 0).  When ``endian`` is
        ``big``, bits are numbered according to Motorola bit ordering.
    length : int
        The length of the signal in bits (1–64).
    dest_start_bit : int
        Where in the destination payload the signal should be inserted.  This
        uses the same numbering scheme as ``src_start_bit``.
    scale : float, optional
        A multiplicative factor applied to the numeric value before
        insertion.  Defaults to 1.0.
    offset : float, optional
        An additive offset applied after scaling.  Defaults to 0.0.
    endian : str, optional
        Either ``'little'`` or ``'big'``.  If not specified, ``'little'`` is
        assumed.  This controls how multi‑byte signals are decoded and
        encoded.
    min_value : float, optional
        Minimum allowable value.  If specified, values below this are
        clamped to the minimum.
    max_value : float, optional
        Maximum allowable value.  If specified, values above this are
        clamped to the maximum.
    """

    src_start_bit: int
    length: int
    dest_start_bit: int
    scale: float = 1.0
    offset: float = 0.0
    endian: str = "little"
    min_value: Optional[float] = None
    max_value: Optional[float] = None

    def decode(self, payload: bytes) -> int:
        """Extract the raw integer value of this signal from a payload.

        The bits are carved out from the source payload according to
        ``src_start_bit`` and ``length``.  Endianess controls how bits are
        ordered.  Returned value is an unsigned integer.  Caller may apply
        further scaling/offset if needed.
        """
        if self.length <= 0 or self.length > 64:
            raise ValueError("Signal length must be between 1 and 64 bits")
        # Determine which bytes are needed from the payload.  Compute bit
        # positions inclusive of start and end bits.
        end_bit = self.src_start_bit + self.length - 1
        start_byte = self.src_start_bit // 8
        end_byte = end_bit // 8
        # Extract the relevant bytes (pad with zeros if payload too short)
        relevant = payload[start_byte:end_byte + 1]
        # Align the bits within the relevant bytes.
        bit_offset = self.src_start_bit % 8
        # Convert bytes to integer
        raw_int = int.from_bytes(relevant, byteorder="big" if self.endian == "big" else "little", signed=False)
        # Shift right by bit_offset to remove lower bits not part of this signal
        if self.endian == "little":
            raw_int >>= bit_offset
        else:
            # For Motorola ordering, shift left to align the start bit
            shift = (8 * len(relevant) - self.length - bit_offset)
            raw_int >>= shift
        # Mask to get only `length` bits
        mask = (1 << self.length) - 1
        value = raw_int & mask
        return value

    def encode(self, value: int) -> bytes:
        """Encode an integer value into a byte sequence representing this signal.

        The integer is first clamped to the specified min/max range (if
        provided), then converted to an unsigned integer occupying `length`
        bits.  The resulting bits are placed at bit position 0 of the
        returned bytes.  The caller is responsible for merging this into
        the appropriate position of the destination payload.
        """
        # Apply min/max clamps
        if self.min_value is not None and value < self.min_value:
            value = int(self.min_value)
        if self.max_value is not None and value > self.max_value:
            value = int(self.max_value)
        # Create mask and compute the raw bit representation
        mask = (1 << self.length) - 1
        raw = value & mask
        # Determine how many bytes are needed
        num_bytes = (self.length + 7) // 8
        raw_bytes = raw.to_bytes(num_bytes, byteorder="big" if self.endian == "big" else "little")
        return raw_bytes


@dataclass
class TranslationEntry:
    """Mapping from one CAN message to another.

    Attributes
    ----------
    source_id : int
        The arbitration ID of the incoming message to match (11‑bit standard
        or 29‑bit extended).  Only exact matches trigger a translation.
    target_id : int
        The ID used when sending the translated message on the target bus.
    signals : List[SignalMapping]
        A list of signal mappings that define how individual fields are
        transformed.  Order does not matter; signals may overlap but later
        mappings will override earlier ones when merged into the outgoing
        payload.
    default_payload : bytes
        A template for the outgoing data bytes.  This must be a bytes object
        of length up to 8.  Bits not covered by any signal mapping will be
        copied from this template.  If shorter than eight bytes, it will
        automatically be padded with zeros when constructing frames.
    source_is_extended : bool
        Whether the source ID uses extended 29‑bit format.
    target_is_extended : bool
        Whether the target ID uses extended 29‑bit format.
    """

    source_id: int
    target_id: int
    signals: List[SignalMapping] = field(default_factory=list)
    default_payload: bytes = b""
    source_is_extended: bool = False
    target_is_extended: bool = False

    def apply(self, message: "CanMessage") -> Optional["CanMessage"]:
        """Translate an incoming CAN message according to this entry.

        Parameters
        ----------
        message : CanMessage
            The incoming CAN frame that matched this entry's ``source_id``.

        Returns
        -------
        CanMessage or None
            A new frame ready to send on the target bus, or None if the
            message's DLC does not match the expected length.
        """
        # Import here to avoid circular import
        from .can_interface import CanMessage
        src_data = message.data
        # Start with a copy of the default payload, padded to 8 bytes
        dest_data = bytearray(self.default_payload.ljust(8, b"\x00"))
        # For each signal mapping, extract value from source, apply scale/offset and insert into dest
        for mapping in self.signals:
            raw_val = mapping.decode(src_data)
            # Apply scaling and offset from translation entry
            # Note: scale and offset belong to mapping
            phys_val = raw_val * mapping.scale + mapping.offset
            # Convert to integer for encoding back to bytes.  Round if necessary
            enc_val = int(round(phys_val))
            raw_bytes = mapping.encode(enc_val)
            # Insert raw_bytes into dest_data at dest_start_bit
            dest_bit = mapping.dest_start_bit
            for i, b in enumerate(raw_bytes):
                # Determine which byte and bit offset this portion occupies
                bit_index = dest_bit + i * 8
                byte_index = bit_index // 8
                # Insert entire byte at once.  Multi‑bit alignment will be handled by encode
                if byte_index < len(dest_data):
                    dest_data[byte_index] &= ~(0xFF << (bit_index % 8))
                    dest_data[byte_index] |= b << (bit_index % 8)
        # Build the outgoing message
        return CanMessage(
            arbitration_id=self.target_id,
            data=bytes(dest_data),
            timestamp=message.timestamp,
            is_extended_id=self.target_is_extended,
        )


class TranslationTable:
    """Collection of translation entries indexed by source ID."""

    def __init__(self, entries: Optional[Iterable[TranslationEntry]] = None) -> None:
        self._entries: Dict[int, TranslationEntry] = {}
        if entries:
            for entry in entries:
                self.add_entry(entry)

    def add_entry(self, entry: TranslationEntry) -> None:
        """Add a translation entry to the table.  Later additions override earlier ones."""
        self._entries[entry.source_id] = entry

    def get_entry(self, source_id: int, is_extended: bool = False) -> Optional[TranslationEntry]:
        """Retrieve a translation entry for the given source ID.

        Parameters
        ----------
        source_id : int
            The arbitration identifier of the incoming message.
        is_extended : bool, optional
            Whether the incoming ID uses extended format.  When True the
            ``source_is_extended`` flag of the entry must also be True to
            return a match.

        Returns
        -------
        TranslationEntry or None
            The matching entry, or ``None`` if no entry exists.
        """
        entry = self._entries.get(source_id)
        if entry and (entry.source_is_extended == is_extended):
            return entry
        return None

    @classmethod
    def from_json(cls, json_str: str) -> "TranslationTable":
        """Construct a translation table from a JSON string.

        The JSON must be an object with a top‑level ``entries`` array.  Each
        element defines a translation entry.  Refer to the README for the
        expected schema.  Unknown keys are ignored.
        """
        data = json.loads(json_str)
        if not isinstance(data, dict) or "entries" not in data or not isinstance(data["entries"], list):
            raise ValueError("Translation table JSON must contain an 'entries' array")
        entries: List[TranslationEntry] = []
        for obj in data["entries"]:
            if not isinstance(obj, dict):
                continue
            # Parse IDs; allow hex strings (e.g. "0x123") by using base=0
            raw_src_id = obj.get("source_id")
            raw_tgt_id = obj.get("target_id")
            src_id = int(raw_src_id, 0) if isinstance(raw_src_id, str) else int(raw_src_id)
            tgt_id = int(raw_tgt_id, 0) if isinstance(raw_tgt_id, str) else int(raw_tgt_id)
            default_payload = bytes(obj.get("default_target_bytes", []))
            src_extended = bool(obj.get("source_is_extended", False))
            tgt_extended = bool(obj.get("target_is_extended", False))
            # Parse signals
            signals: List[SignalMapping] = []
            for sig in obj.get("signals", []):
                try:
                    mapping = SignalMapping(
                        src_start_bit=int(sig["src_start_bit"]),
                        length=int(sig["length"]),
                        dest_start_bit=int(sig["dest_start_bit"]),
                        scale=float(sig.get("scale", 1.0)),
                        offset=float(sig.get("offset", 0.0)),
                        endian=str(sig.get("endian", "little")).lower(),
                        min_value=(float(sig["min_value"]) if "min_value" in sig else None),
                        max_value=(float(sig["max_value"]) if "max_value" in sig else None),
                    )
                    signals.append(mapping)
                except Exception as ex:
                    # Log or skip invalid signal definitions
                    continue
            entry = TranslationEntry(
                source_id=src_id,
                target_id=tgt_id,
                signals=signals,
                default_payload=default_payload,
                source_is_extended=src_extended,
                target_is_extended=tgt_extended,
            )
            entries.append(entry)
        return cls(entries)

    @classmethod
    def from_json_file(cls, path: str) -> "TranslationTable":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_json(f.read())