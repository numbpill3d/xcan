"""Core logic for mapping CAN messages between different networks.

This module implements the translation runtime.  The main class,
``CanTranslator``, encapsulates a worker thread that continuously reads
incoming frames from a source CAN interface, looks up matching translation
entries in a ``TranslationTable``, applies the specified signal
transformations and emits the resulting frame on a target interface.  The
translator also supports optional adaptive fuzzing for unknown messages and
event callbacks to drive a GUI or logging subsystem.

The design is intentionally maximalist: each step is broken down into
separate methods with copious comments explaining the rationale.  Callers
can register multiple listeners for events such as "message received" and
"message sent", which will be invoked synchronously on the worker thread.

Thread safety is a primary concern: the translator uses locks to protect
shared state (e.g., the translation table) and ensures that interfaces are
opened before use.  The worker thread can be cleanly stopped via a flag
and will join on shutdown.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Any

from .can_interface import BaseCanInterface, CanMessage
from .translation_table import TranslationTable, TranslationEntry
from .fuzzing import FuzzingStrategy, NullFuzzer


class CanTranslator:
    """Translate CAN frames from a source to a target using a translation table.

    Parameters
    ----------
    source : BaseCanInterface
        Interface from which raw frames are read.
    target : BaseCanInterface
        Interface on which translated frames are emitted.
    table : TranslationTable
        The mapping from source IDs to target IDs and signal transformations.
    fuzzing : FuzzingStrategy, optional
        An object implementing the fuzzing API.  If provided, unknown
        messages (those without a translation entry) will be passed to the
        fuzzer.  If ``None``, a default no‑op fuzzer is used.
    """

    def __init__(self, source: BaseCanInterface, target: BaseCanInterface, table: TranslationTable, fuzzing: Optional[FuzzingStrategy] = None) -> None:
        self.source = source
        self.target = target
        self.table = table
        self.fuzzer: FuzzingStrategy = fuzzing if fuzzing is not None else NullFuzzer()
        # Dictionary mapping event names to lists of callbacks
        self._listeners: Dict[str, List[Callable[[CanMessage, Optional[CanMessage]], None]]] = {
            "received": [],
            "translated": [],
            "sent": [],
            "unknown": [],
        }
        # Internal thread for reading and translating
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    def add_listener(self, event: str, callback: Callable[[CanMessage, Optional[CanMessage]], None]) -> None:
        """Register a callback for a specific event.

        Valid event names are:

        * ``'received'`` – called with (src_msg, None) when a frame is read.
        * ``'translated'`` – called with (src_msg, dst_msg) after translation but before sending.
        * ``'sent'`` – called with (dst_msg, None) after a frame is emitted.
        * ``'unknown'`` – called with (src_msg, None) when no translation exists.
        """
        if event not in self._listeners:
            raise ValueError(f"Unknown event name: {event}")
        self._listeners[event].append(callback)

    def _notify(self, event: str, src_msg: CanMessage, dst_msg: Optional[CanMessage] = None) -> None:
        """Invoke all callbacks for a given event.

        Callbacks are executed synchronously on the translator's worker thread.
        If a callback raises an exception it is logged but does not stop
        processing.
        """
        for cb in list(self._listeners.get(event, [])):
            try:
                cb(src_msg, dst_msg)
            except Exception as ex:
                # Print to stderr; in a production system you might log this
                print(f"[Translator] Callback for event '{event}' raised {ex}")

    def start(self) -> None:
        """Open interfaces and launch the worker thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # Open both interfaces if not already open
            if not self.source.is_open():
                self.source.open()
            if not self.target.is_open():
                self.target.open()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="CanTranslatorThread", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Signal the worker thread to terminate and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        """Background worker: poll source bus, translate and send frames."""
        while not self._stop_event.is_set():
            try:
                msg = self.source.receive(timeout=0.1)
            except Exception as ex:
                print(f"[Translator] Error receiving from source: {ex}")
                time.sleep(0.5)
                continue
            if msg is None:
                continue
            # Notify listeners of receipt
            self._notify("received", msg)
            # Attempt translation
            entry = self.table.get_entry(msg.arbitration_id, is_extended=msg.is_extended_id)
            if entry is None:
                # Unknown message: delegate to fuzzer and notify
                self._notify("unknown", msg)
                try:
                    fuzz_frames = self.fuzzer.handle_unknown(msg)
                    for fmsg in fuzz_frames:
                        self.target.send(fmsg)
                        self._notify("sent", fmsg, None)
                except Exception as ex:
                    print(f"[Translator] Fuzzer error: {ex}")
                continue
            try:
                dst_msg = entry.apply(msg)
            except Exception as ex:
                print(f"[Translator] Translation error for ID {msg.arbitration_id:#x}: {ex}")
                continue
            if dst_msg is None:
                # DLC mismatch or mapping returned nothing
                continue
            # Notify of translation
            self._notify("translated", msg, dst_msg)
            try:
                self.target.send(dst_msg)
                self._notify("sent", dst_msg, None)
            except Exception as ex:
                print(f"[Translator] Error sending to target: {ex}")

    def add_translation_entry(self, entry: TranslationEntry) -> None:
        """Add or update a translation entry while running."""
        with self._lock:
            self.table.add_entry(entry)
        # It is safe to modify the table on the fly; lookups are atomic
