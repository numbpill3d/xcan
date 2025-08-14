"""Graphical interface for the Cross‑Make CAN Message Translator.

This module defines a Tkinter‑based GUI that monitors the activities of a
``CanTranslator`` instance in real time and allows the user to manage
translation rules.  The aesthetic draws inspiration from early 1990s
Macintosh system software: neutral greys, crisp borders, Chicago‑like
fonts and widget metaphors reminiscent of System 7.  While Tkinter
does not provide an exact replica of these components, careful styling
can evoke a similar feel.

The GUI consists of a main window with three primary panels:

1. **Incoming Frames** – a scrolling list showing raw frames read from the
   source bus.  Columns include timestamp, arbitration ID and payload.
2. **Translated Frames** – a list showing the relationship between
   incoming frames and their translated counterparts (source ID → target
   ID) along with the resulting payload.
3. **Unknown Frames** – a log of frames for which no translation entry
   existed.  These are candidates for fuzzing or further reverse
   engineering.

Controls along the bottom allow starting/stopping the translator and
opening the translation table editor.  The editor (invoked in a separate
top‑level window) presents a form for adding or modifying translation
entries; it is intentionally verbose to encourage explicit input.

Threading Considerations
------------------------

Tkinter is not thread safe; all interaction with widgets must occur on
the main thread.  The ``CanTranslator`` class issues event callbacks on
its worker thread.  To safely update the UI, the GUI class collects
events into thread‑safe queues and periodically flushes them to the
widgets via the Tkinter ``after`` mechanism.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Optional, Tuple
from collections import deque

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from .can_interface import CanMessage
from .translator_core import CanTranslator
from .translation_table import SignalMapping, TranslationEntry


class CanTranslatorGUI:
    """Tkinter GUI wrapper around a ``CanTranslator`` instance."""

    def __init__(self, translator: CanTranslator) -> None:
        self.translator = translator
        self.root = tk.Tk()
        self.root.title("Cross‑Make CAN Translator")
        # Use a neutral grey reminiscent of old Mac GUIs
        self.root.configure(bg="#c0c0c0")
        # Create style for ttk widgets to approximate classic Macintosh look
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#c0c0c0')
        style.configure('TButton', background='#d9d9d9', foreground='black', relief='raised')
        style.configure('TLabel', background='#c0c0c0', foreground='black')
        style.configure('Treeview', background='#e0e0e0', fieldbackground='#e0e0e0', foreground='black')
        style.map('TButton', background=[('active', '#e6e6e6')])
        # Data structures to hold pending events (thread‑safe)
        self._queue_received: Deque[CanMessage] = deque()
        self._queue_translated: Deque[Tuple[CanMessage, CanMessage]] = deque()
        self._queue_unknown: Deque[CanMessage] = deque()
        self._queue_lock = threading.Lock()
        # Build the layout
        self._build_widgets()
        # Register translator listeners
        translator.add_listener('received', self._on_received)
        translator.add_listener('translated', self._on_translated)
        translator.add_listener('unknown', self._on_unknown)
        # Kick off periodic UI updates
        self._update_ui()

    def _build_widgets(self) -> None:
        """Create and layout all widgets in the main window."""
        top_frame = ttk.Frame(self.root)
        top_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)
        # Incoming frames panel
        incoming_frame = ttk.Frame(top_frame)
        incoming_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        incoming_label = ttk.Label(incoming_frame, text="Incoming Frames", font=('Helvetica', 10, 'bold'))
        incoming_label.pack(side=tk.TOP, anchor='w')
        self.incoming_tree = ttk.Treeview(incoming_frame, columns=('time', 'id', 'data'), show='headings', height=15)
        self.incoming_tree.heading('time', text='Time (s)')
        self.incoming_tree.heading('id', text='ID')
        self.incoming_tree.heading('data', text='Data')
        self.incoming_tree.column('time', width=80, anchor='e')
        self.incoming_tree.column('id', width=80, anchor='e')
        self.incoming_tree.column('data', width=160, anchor='w')
        self.incoming_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        incoming_scroll = ttk.Scrollbar(incoming_frame, orient='vertical', command=self.incoming_tree.yview)
        incoming_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.incoming_tree.configure(yscrollcommand=incoming_scroll.set)
        # Translated frames panel
        translated_frame = ttk.Frame(top_frame)
        translated_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        translated_label = ttk.Label(translated_frame, text="Translated Frames", font=('Helvetica', 10, 'bold'))
        translated_label.pack(side=tk.TOP, anchor='w')
        self.translated_tree = ttk.Treeview(translated_frame, columns=('src_id', 'tgt_id', 'src_data', 'tgt_data'), show='headings', height=15)
        self.translated_tree.heading('src_id', text='Src ID')
        self.translated_tree.heading('tgt_id', text='Tgt ID')
        self.translated_tree.heading('src_data', text='Src Data')
        self.translated_tree.heading('tgt_data', text='Tgt Data')
        self.translated_tree.column('src_id', width=60, anchor='e')
        self.translated_tree.column('tgt_id', width=60, anchor='e')
        self.translated_tree.column('src_data', width=130, anchor='w')
        self.translated_tree.column('tgt_data', width=130, anchor='w')
        self.translated_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        trans_scroll = ttk.Scrollbar(translated_frame, orient='vertical', command=self.translated_tree.yview)
        trans_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.translated_tree.configure(yscrollcommand=trans_scroll.set)
        # Unknown frames panel
        unknown_frame = ttk.Frame(top_frame)
        unknown_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0))
        unknown_label = ttk.Label(unknown_frame, text="Unknown Frames", font=('Helvetica', 10, 'bold'))
        unknown_label.pack(side=tk.TOP, anchor='w')
        self.unknown_tree = ttk.Treeview(unknown_frame, columns=('time', 'id', 'data'), show='headings', height=15)
        self.unknown_tree.heading('time', text='Time (s)')
        self.unknown_tree.heading('id', text='ID')
        self.unknown_tree.heading('data', text='Data')
        self.unknown_tree.column('time', width=80, anchor='e')
        self.unknown_tree.column('id', width=80, anchor='e')
        self.unknown_tree.column('data', width=160, anchor='w')
        self.unknown_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        unknown_scroll = ttk.Scrollbar(unknown_frame, orient='vertical', command=self.unknown_tree.yview)
        unknown_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.unknown_tree.configure(yscrollcommand=unknown_scroll.set)
        # Bottom control panel
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=8)
        self.start_button = ttk.Button(bottom_frame, text="Start", command=self._on_start)
        self.stop_button = ttk.Button(bottom_frame, text="Stop", command=self._on_stop)
        self.edit_button = ttk.Button(bottom_frame, text="Edit Table", command=self._on_edit_table)
        self.start_button.pack(side=tk.LEFT, padx=4)
        self.stop_button.pack(side=tk.LEFT, padx=4)
        self.edit_button.pack(side=tk.LEFT, padx=4)
        # Initially disable stop button
        self.stop_button.state(['disabled'])

    def _on_start(self) -> None:
        """Handler for the Start button."""
        try:
            self.translator.start()
            self.start_button.state(['disabled'])
            self.stop_button.state(['!disabled'])
        except Exception as ex:
            messagebox.showerror("Error", f"Could not start translator:\n{ex}")

    def _on_stop(self) -> None:
        """Handler for the Stop button."""
        try:
            self.translator.stop()
            self.stop_button.state(['disabled'])
            self.start_button.state(['!disabled'])
        except Exception as ex:
            messagebox.showerror("Error", f"Could not stop translator:\n{ex}")

    def _on_edit_table(self) -> None:
        """Open a dialog to add a new translation entry."""
        dialog = TranslationEntryDialog(self.root)
        result = dialog.show()
        if result is None:
            return
        try:
            self.translator.add_translation_entry(result)
            messagebox.showinfo("Success", "Translation entry added/updated.")
        except Exception as ex:
            messagebox.showerror("Error", f"Failed to add entry:\n{ex}")

    def _on_received(self, msg: CanMessage, _: Optional[CanMessage]) -> None:
        with self._queue_lock:
            self._queue_received.append(msg)

    def _on_translated(self, src: CanMessage, dst: Optional[CanMessage]) -> None:
        if dst is None:
            return
        with self._queue_lock:
            self._queue_translated.append((src, dst))

    def _on_unknown(self, msg: CanMessage, _: Optional[CanMessage]) -> None:
        with self._queue_lock:
            self._queue_unknown.append(msg)

    def _update_ui(self) -> None:
        """Periodically flush queued events into the GUI widgets."""
        # Process a limited number of queued items per iteration to keep UI responsive
        MAX_PER_ITER = 50
        with self._queue_lock:
            for _ in range(min(MAX_PER_ITER, len(self._queue_received))):
                msg = self._queue_received.popleft()
                ts = f"{msg.timestamp:.3f}"
                arb_id = f"0x{msg.arbitration_id:X}"
                data_hex = ' '.join(f"{b:02X}" for b in msg.data)
                self.incoming_tree.insert('', 'end', values=(ts, arb_id, data_hex))
                # Keep listboxes from growing indefinitely
                if len(self.incoming_tree.get_children()) > 1000:
                    self.incoming_tree.delete(self.incoming_tree.get_children()[0])
            for _ in range(min(MAX_PER_ITER, len(self._queue_translated))):
                src, dst = self._queue_translated.popleft()
                src_id = f"0x{src.arbitration_id:X}"
                dst_id = f"0x{dst.arbitration_id:X}"
                src_data = ' '.join(f"{b:02X}" for b in src.data)
                dst_data = ' '.join(f"{b:02X}" for b in dst.data)
                self.translated_tree.insert('', 'end', values=(src_id, dst_id, src_data, dst_data))
                if len(self.translated_tree.get_children()) > 1000:
                    self.translated_tree.delete(self.translated_tree.get_children()[0])
            for _ in range(min(MAX_PER_ITER, len(self._queue_unknown))):
                msg = self._queue_unknown.popleft()
                ts = f"{msg.timestamp:.3f}"
                arb_id = f"0x{msg.arbitration_id:X}"
                data_hex = ' '.join(f"{b:02X}" for b in msg.data)
                self.unknown_tree.insert('', 'end', values=(ts, arb_id, data_hex))
                if len(self.unknown_tree.get_children()) > 1000:
                    self.unknown_tree.delete(self.unknown_tree.get_children()[0])
        # Schedule the next update
        self.root.after(200, self._update_ui)

    def run(self) -> None:
        """Enter the Tkinter main loop."""
        self.root.mainloop()


class TranslationEntryDialog(simpledialog.Dialog):
    """Dialog for creating or editing a ``TranslationEntry``.

    Presents a form to the user for specifying the source ID, target ID,
    whether IDs are extended and the signal mappings.  Signal mappings are
    entered in a multiline text area where each line has the format:

        src_start_bit,length,dest_start_bit,scale,offset

    For example ``0,8,0,1.0,0.0`` maps the first byte of the source to
    the first byte of the target without scaling.  Missing trailing values
    default to scale=1 and offset=0.  The dialog returns a
    ``TranslationEntry`` or ``None`` if cancelled.
    """

    def body(self, master: tk.Frame) -> Optional[tk.Widget]:
        tk.Label(master, text="Source ID (hex):", bg="#c0c0c0").grid(row=0, column=0, sticky='e', padx=4, pady=2)
        tk.Label(master, text="Target ID (hex):", bg="#c0c0c0").grid(row=1, column=0, sticky='e', padx=4, pady=2)
        tk.Label(master, text="Source Extended ID:", bg="#c0c0c0").grid(row=2, column=0, sticky='e', padx=4, pady=2)
        tk.Label(master, text="Target Extended ID:", bg="#c0c0c0").grid(row=3, column=0, sticky='e', padx=4, pady=2)
        tk.Label(master, text="Default Target Bytes (comma‑sep hex):", bg="#c0c0c0").grid(row=4, column=0, sticky='e', padx=4, pady=2)
        tk.Label(master, text="Signals (one per line):", bg="#c0c0c0").grid(row=5, column=0, sticky='ne', padx=4, pady=2)
        self.src_id_var = tk.StringVar()
        self.tgt_id_var = tk.StringVar()
        self.src_ext_var = tk.BooleanVar(value=False)
        self.tgt_ext_var = tk.BooleanVar(value=False)
        self.default_bytes_var = tk.StringVar()
        # Inputs
        tk.Entry(master, textvariable=self.src_id_var).grid(row=0, column=1, padx=4, pady=2)
        tk.Entry(master, textvariable=self.tgt_id_var).grid(row=1, column=1, padx=4, pady=2)
        tk.Checkbutton(master, variable=self.src_ext_var, bg="#c0c0c0").grid(row=2, column=1, sticky='w', padx=4, pady=2)
        tk.Checkbutton(master, variable=self.tgt_ext_var, bg="#c0c0c0").grid(row=3, column=1, sticky='w', padx=4, pady=2)
        tk.Entry(master, textvariable=self.default_bytes_var).grid(row=4, column=1, padx=4, pady=2)
        self.signals_text = tk.Text(master, width=40, height=6)
        self.signals_text.grid(row=5, column=1, padx=4, pady=2)
        # Provide example placeholder
        self.signals_text.insert('1.0', "0,8,0,1.0,0.0\n")
        return self.signals_text

    def validate(self) -> bool:
        """Validate the form fields and show an error if invalid."""
        try:
            int(self.src_id_var.get(), 0)
            int(self.tgt_id_var.get(), 0)
        except Exception:
            messagebox.showerror("Invalid ID", "Source and target IDs must be valid integers (e.g. 0x123).")
            return False
        # Validate default bytes
        db = self.default_bytes_var.get().strip()
        if db:
            parts = db.split(',')
            try:
                for p in parts:
                    int(p.strip(), 16)
            except Exception:
                messagebox.showerror("Invalid bytes", "Default target bytes must be comma‑separated hex values (e.g. 00,FF,10).")
                return False
        # Validate signals lines
        lines = self.signals_text.get('1.0', 'end').strip().splitlines()
        for line in lines:
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 3:
                messagebox.showerror("Invalid signal", f"Signal definition '{line}' must have at least src_start_bit,length,dest_start_bit.")
                return False
            # Ensure numeric
            try:
                int(parts[0]); int(parts[1]); int(parts[2])
                if len(parts) > 3:
                    float(parts[3])
                if len(parts) > 4:
                    float(parts[4])
            except Exception:
                messagebox.showerror("Invalid signal", f"Signal definition '{line}' contains non‑numeric values.")
                return False
        return True

    def apply(self) -> None:
        """Create a TranslationEntry instance from the form."""
        src_id = int(self.src_id_var.get(), 0)
        tgt_id = int(self.tgt_id_var.get(), 0)
        src_ext = bool(self.src_ext_var.get())
        tgt_ext = bool(self.tgt_ext_var.get())
        default_bytes_str = self.default_bytes_var.get().strip()
        default_bytes: bytes
        if default_bytes_str:
            parts = [p.strip() for p in default_bytes_str.split(',')]
            default_bytes = bytes(int(p, 16) for p in parts)
        else:
            default_bytes = b""
        signals: list[SignalMapping] = []
        lines = self.signals_text.get('1.0', 'end').strip().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(',')]
            # Pad parts to length 5 with defaults
            while len(parts) < 5:
                parts.append('')
            sig = SignalMapping(
                src_start_bit=int(parts[0]),
                length=int(parts[1]),
                dest_start_bit=int(parts[2]),
                scale=float(parts[3]) if parts[3] else 1.0,
                offset=float(parts[4]) if parts[4] else 0.0,
            )
            signals.append(sig)
        self.result = TranslationEntry(
            source_id=src_id,
            target_id=tgt_id,
            signals=signals,
            default_payload=default_bytes,
            source_is_extended=src_ext,
            target_is_extended=tgt_ext,
        )

    def show(self) -> Optional[TranslationEntry]:
        """Display the dialog and return a TranslationEntry or None."""
        self.result = None
        super().__init__(self.root, title="Add/Update Translation Entry")
        return self.result