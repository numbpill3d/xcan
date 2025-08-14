"""Adaptive fuzzing strategies for unknown CAN messages.

One of the challenges when dealing with proprietary automotive networks is
the plethora of undocumented messages.  When a frame arrives for which
there is no entry in the translation table, the Cross‑Make CAN translator
can optionally invoke a *fuzzer* to generate candidate frames on the
target bus.  Fuzzers are useful for discovering how a vehicle reacts to
unexpected inputs and for reverse engineering new commands.

This module defines a small API for fuzzers:

* :class:`FuzzingStrategy`: abstract base class with a single method
  :meth:`handle_unknown` which takes an incoming frame and returns an
  iterable of frames to transmit on the target bus.
* :class:`NullFuzzer`: a no‑op implementation that returns an empty list.
* :class:`RandomByteFuzzer`: a rudimentary strategy that duplicates the
  unknown frame, perturbs its payload and sends the results.  It tries
  flipping each bit individually as well as randomising bytes.  This
  strategy is simplistic and may not uncover complex state machines but
  serves as a starting point for experimentation.

Fuzzers can maintain internal state across calls (e.g. to avoid
re‑transmitting previously tried payloads).  They should be careful not to
overwhelm the target bus – generating too many frames may disrupt normal
operation or cause denial of service.
"""

from __future__ import annotations

import random
from typing import Iterable, List

from .can_interface import CanMessage


class FuzzingStrategy:
    """Abstract base class for fuzzing unknown CAN messages."""

    def handle_unknown(self, msg: CanMessage) -> Iterable[CanMessage]:
        """Given an unknown message, produce one or more frames for the target.

        Parameters
        ----------
        msg : CanMessage
            The frame received on the source bus that had no translation entry.

        Returns
        -------
        iterable of CanMessage
            Frames to transmit on the target bus.  May be an empty iterable
            if no fuzzing is desired.
        """
        raise NotImplementedError


class NullFuzzer(FuzzingStrategy):
    """Fuzzer that does nothing.  Always returns an empty list."""

    def handle_unknown(self, msg: CanMessage) -> Iterable[CanMessage]:
        return []


class RandomByteFuzzer(FuzzingStrategy):
    """Simple fuzzing strategy that flips bits and randomises bytes.

    This fuzzer takes an unknown message, creates a handful of modified
    versions and returns them.  By default it flips each bit in the
    original payload once and also generates a few frames with entirely
    random payloads.  Users can configure the number of random frames via
    ``num_random`` and whether bit flipping is performed via
    ``flip_bits``.

    Caution: indiscriminate fuzzing on a live vehicle can produce
    unpredictable results.  Only use this strategy on test benches or with
    components isolated from critical systems.
    """

    def __init__(self, num_random: int = 3, flip_bits: bool = True) -> None:
        self.num_random = num_random
        self.flip_bits = flip_bits
        # Maintain a set of tried payloads to avoid duplicates
        self._seen: set[bytes] = set()

    def _random_payload(self, length: int) -> bytes:
        return bytes(random.getrandbits(8) for _ in range(length))

    def handle_unknown(self, msg: CanMessage) -> Iterable[CanMessage]:
        payload = msg.data
        results: List[CanMessage] = []
        # Bit flip fuzzing
        if self.flip_bits:
            for i in range(len(payload) * 8):
                byte_index = i // 8
                bit_index = i % 8
                # Flip the bit
                new_byte = payload[byte_index] ^ (1 << bit_index)
                new_payload = bytearray(payload)
                new_payload[byte_index] = new_byte
                # Avoid sending identical payloads
                if bytes(new_payload) in self._seen:
                    continue
                self._seen.add(bytes(new_payload))
                results.append(CanMessage(
                    arbitration_id=msg.arbitration_id,
                    data=bytes(new_payload),
                    timestamp=msg.timestamp,
                    is_extended_id=msg.is_extended_id,
                ))
        # Random payload fuzzing
        for _ in range(self.num_random):
            rnd = self._random_payload(len(payload))
            if rnd in self._seen:
                continue
            self._seen.add(rnd)
            results.append(CanMessage(
                arbitration_id=msg.arbitration_id,
                data=rnd,
                timestamp=msg.timestamp,
                is_extended_id=msg.is_extended_id,
            ))
        return results