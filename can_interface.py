"""Abstraction layer for interacting with Controller Area Network (CAN) buses.

This module defines a set of classes that unify the process of reading and
writing raw CAN frames across different backends.  By coding against the
`BaseCanInterface` API you can easily switch between hardware, software and
mock implementations.  The primary classes are:

* :class:`CanMessage`: a lightweight container for CAN identifiers, payload
  bytes, timestamp and addressing mode (standard vs extended).
* :class:`BaseCanInterface`: an abstract base class specifying the methods
  required to send and receive messages.  Concrete subclasses must
  implement these methods and manage any necessary threading or resource
  cleanup.
* :class:`SocketCanInterface`: uses the Linux ``socket`` module to open a
  raw socket on an interface such as ``can0``.  It is purely based on the
  standard library and thus available even without third‑party packages.
* :class:`PythonCanInterface`: wraps the higher level
  [`python‑can`](https://python‑can.readthedocs.io/en/latest/) API when
  available.  This backend supports many different adapters and operating
  systems.  If the library cannot be imported, this class will not be
  registered.
* :class:`MockCanInterface`: an in‑memory implementation useful for unit
  testing without physical hardware.  Messages sent through a mock interface
  are stored in a queue that another instance can read from, simulating a
  wire.

All interfaces are thread‑safe; they internally use locks or queue
mechanisms to ensure that concurrent reads/writes do not corrupt state.

The code is deliberately explicit and verbose.  Detailed docstrings and
comments accompany each operation to aid newcomers.  If you wish to add a
new backend (e.g., for Serial or WebSocket based CAN), subclass
``BaseCanInterface`` and implement the abstract methods.

"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional, Iterable, Tuple, List, Dict, Any

try:
    import socket
    _HAVE_SOCKET = True
except Exception:
    # In restricted environments the socket module may not be available.
    _HAVE_SOCKET = False

try:
    import can  # type: ignore
    _HAVE_PYTHON_CAN = True
except Exception:
    # The python‑can package is optional.  If it's not present, we fall back
    # to socketcan or mock implementations.
    _HAVE_PYTHON_CAN = False


@dataclass
class CanMessage:
    """Represents a single CAN frame.

    Attributes
    ----------
    arbitration_id : int
        The 11‑bit (standard) or 29‑bit (extended) identifier of the frame.
    data : bytes
        Up to eight bytes of payload.  For CAN FD this could be longer but
        this translator is restricted to classical CAN frames for simplicity.
    timestamp : float
        A monotonic timestamp (seconds since an unspecified epoch) when the
        frame was received.  For transmitted frames you may set this to the
        current time.
    is_extended_id : bool
        True if the identifier is 29‑bit extended format, False for 11‑bit
        standard format.
    """

    arbitration_id: int
    data: bytes
    timestamp: float
    is_extended_id: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data, (bytes, bytearray)):
            raise TypeError(f"data must be bytes, got {type(self.data)!r}")
        if len(self.data) > 8:
            raise ValueError("Classical CAN frames support at most 8 bytes of payload")


class BaseCanInterface:
    """Abstract base class for CAN bus interfaces.

    Subclasses must implement :meth:`receive` and :meth:`send`.

    All implementations should be thread‑safe; multiple threads may call
    ``send`` concurrently while another thread is blocking in ``receive``.
    """

    def __init__(self, interface: str, bitrate: Optional[int] = None) -> None:
        """Initialise the CAN interface.

        Parameters
        ----------
        interface : str
            A platform‑specific identifier for the CAN channel.  On Linux
            socketcan this might be ``can0``, ``vcan0`` or ``slcan0``.
        bitrate : int, optional
            Desired baud rate.  Some backends (e.g. python‑can) allow setting
            the bitrate programmatically; others assume it has already been
            configured out‑of‑band.  Passing ``None`` means to leave the
            interface at its existing speed.
        """
        self.interface = interface
        self.bitrate = bitrate
        # A flag to indicate if the interface has been opened.  Subclasses
        # should set this to True once ready to send/receive.
        self._is_open = False
        # Lock protects any state that could be mutated concurrently.
        self._lock = threading.RLock()

    def open(self) -> None:
        """Open the CAN interface.

        Subclasses should override this method to perform any setup
        operations (e.g., binding a socket).  The default implementation
        simply marks the interface as open.
        """
        with self._lock:
            self._is_open = True

    def close(self) -> None:
        """Close the CAN interface and release resources.

        Subclasses should override this to close sockets or stop threads.
        The default implementation just marks the interface closed.
        """
        with self._lock:
            self._is_open = False

    def is_open(self) -> bool:
        """Return True if the interface is currently open."""
        with self._lock:
            return self._is_open

    def receive(self, timeout: Optional[float] = None) -> Optional[CanMessage]:
        """Block until a CAN frame is received or until `timeout` elapses.

        Parameters
        ----------
        timeout : float, optional
            Maximum number of seconds to wait.  If ``None`` (default)
            ``receive`` will block indefinitely.  A value of ``0`` performs
            a non‑blocking poll.

        Returns
        -------
        CanMessage or None
            The next received frame, or ``None`` if the timeout expired.
        """
        raise NotImplementedError("receive must be implemented by subclasses")

    def send(self, message: CanMessage) -> None:
        """Transmit a CAN frame on the interface.

        Parameters
        ----------
        message : CanMessage
            The frame to send.  Its ``data`` attribute must not exceed 8
            bytes; if it does, a ``ValueError`` should be raised.
        """
        raise NotImplementedError("send must be implemented by subclasses")

    # Aliases to integrate with python‑can like API
    def recv(self, timeout: Optional[float] = None) -> Optional[CanMessage]:
        return self.receive(timeout)

    def send_msg(self, arbitration_id: int, data: bytes, is_extended_id: bool = False) -> None:
        msg = CanMessage(arbitration_id=arbitration_id, data=data, timestamp=time.monotonic(), is_extended_id=is_extended_id)
        self.send(msg)


class SocketCanInterface(BaseCanInterface):
    """CAN interface implementation using Linux SocketCAN.

    Linux exposes CAN busses through the AF_CAN socket family.  This
    implementation binds a raw CAN socket to the given interface name and
    uses ``socket.recvfrom`` and ``socket.send`` to exchange frames.  It
    supports only classical CAN at up to 8 data bytes.

    This class will be available only if the standard library ``socket``
    module exposes ``AF_CAN``.  On non‑Linux platforms or restricted
    environments this may not be the case.
    """

    # The CAN frame struct formats are defined by linux/can.h.  A classical CAN
    # frame consists of an 8‑byte ID/control field followed by 8 bytes of data.
    # The format '<IB3x8s' corresponds to:
    #   - can_id (uint32)  : 32 bits containing the identifier and flags
    #   - can_dlc (uint8)  : data length code (0–8)
    #   - padding (3 bytes): align to 8 bytes
    #   - data (8s)        : payload bytes
    _CAN_FRAME_FMT = "<IB3x8s"
    _CAN_FRAME_SIZE = struct.calcsize(_CAN_FRAME_FMT)

    def __init__(self, interface: str, bitrate: Optional[int] = None) -> None:
        super().__init__(interface, bitrate)
        if not _HAVE_SOCKET or not hasattr(socket, "AF_CAN"):
            raise EnvironmentError("SocketCAN is not available on this system")
        self._sock: Optional[socket.socket] = None
        self._recv_lock = threading.RLock()

    def open(self) -> None:
        """Bind a raw CAN socket to the specified interface.

        On Linux, this requires appropriate permissions (CAP_NET_ADMIN).
        """
        if self._is_open:
            return
        with self._lock:
            self._sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)  # type: ignore[attr-defined]
            # Optional: set bitrate; requires root and may not be supported on all drivers.
            # This implementation does not attempt to change the bitrate at runtime.
            self._sock.bind((self.interface,))
            self._is_open = True

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        super().close()

    def receive(self, timeout: Optional[float] = None) -> Optional[CanMessage]:
        if not self._is_open or self._sock is None:
            raise RuntimeError("SocketCAN interface is not open")
        # Set socket timeout.  None -> block forever; 0 -> non‑blocking
        self._sock.settimeout(timeout)
        try:
            frame, _ = self._sock.recvfrom(self._CAN_FRAME_SIZE)
            can_id, dlc, data = struct.unpack(self._CAN_FRAME_FMT, frame)
            is_extended = bool(can_id & socket.CAN_EFF_FLAG)  # type: ignore[attr-defined]
            # Mask out EFF/RTR/ERR flags to get just the arbitration ID
            arbitration_id = can_id & socket.CAN_EFF_MASK if is_extended else can_id & socket.CAN_SFF_MASK  # type: ignore[attr-defined]
            message = CanMessage(
                arbitration_id=arbitration_id,
                data=data[:dlc],
                timestamp=time.monotonic(),
                is_extended_id=is_extended,
            )
            return message
        except socket.timeout:
            return None

    def send(self, message: CanMessage) -> None:
        if not self._is_open or self._sock is None:
            raise RuntimeError("SocketCAN interface is not open")
        if len(message.data) > 8:
            raise ValueError("SocketCAN supports only up to 8 data bytes")
        can_id = message.arbitration_id
        if message.is_extended_id:
            can_id |= socket.CAN_EFF_FLAG  # type: ignore[attr-defined]
        # Construct the frame.  Pad payload to 8 bytes.
        data_padded = message.data.ljust(8, b"\x00")
        frame = struct.pack(self._CAN_FRAME_FMT, can_id, len(message.data), data_padded)
        with self._recv_lock:
            self._sock.send(frame)


class PythonCanInterface(BaseCanInterface):
    """CAN interface implementation using the python‑can library.

    This class is only defined if the `can` module can be imported.  It
    delegates to `can.interface.Bus` for actual I/O.  See
    https://python‑can.readthedocs.io/ for supported interfaces (virtual,
    USB, serial, etc.).
    """

    def __init__(self, interface: str, bitrate: Optional[int] = None, channel: Optional[str] = None, bustype: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(interface, bitrate)
        # Determine bustype/channel.  If ``channel`` or ``bustype`` is not
        # provided, fall back to the ``interface`` argument.
        self.channel = channel or interface
        self.bustype = bustype or "socketcan"  # default to socketcan; python‑can also supports slcan, kvaser, etc.
        self.can_kwargs = kwargs
        self._bus: Optional[can.Bus] = None  # type: ignore[name-defined]

    def open(self) -> None:
        if self._is_open:
            return
        if not _HAVE_PYTHON_CAN:
            raise EnvironmentError("python‑can is not installed")
        from can import Bus  # delayed import to avoid import errors at module import time
        self._bus = Bus(channel=self.channel, bustype=self.bustype, bitrate=self.bitrate, **self.can_kwargs)
        self._is_open = True

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:
                pass
            finally:
                self._bus = None
        super().close()

    def receive(self, timeout: Optional[float] = None) -> Optional[CanMessage]:
        if not self._is_open or self._bus is None:
            raise RuntimeError("python‑can bus is not open")
        # python‑can uses seconds; None blocks forever; 0 returns immediately
        msg = self._bus.recv(timeout)
        if msg is None:
            return None
        return CanMessage(
            arbitration_id=msg.arbitration_id,
            data=bytes(msg.data),
            timestamp=msg.timestamp if hasattr(msg, 'timestamp') and msg.timestamp is not None else time.monotonic(),
            is_extended_id=msg.is_extended_id,
        )

    def send(self, message: CanMessage) -> None:
        if not self._is_open or self._bus is None:
            raise RuntimeError("python‑can bus is not open")
        from can import Message  # type: ignore
        if len(message.data) > 8:
            raise ValueError("CAN data must be <=8 bytes for classical CAN")
        can_msg = Message(
            arbitration_id=message.arbitration_id,
            data=bytearray(message.data),
            is_extended_id=message.is_extended_id,
            is_fd=False,
        )
        self._bus.send(can_msg)


class MockCanInterface(BaseCanInterface):
    """In‑memory CAN interface for testing without hardware.

    Instances of this class maintain an internal thread‑safe queue of
    messages.  When you call :meth:`send`, the message is appended to the
    queue.  When you call :meth:`receive`, a message is popped from the
    queue.  You can connect two MockCanInterfaces together by passing one
    instance to another's ``peer`` attribute (set during initialisation) so
    that sending on one interface delivers to the peer.  This allows you
    simulate communication between a source and a target without any
    physical CAN hardware.
    """

    def __init__(self, interface: str, peer: Optional["MockCanInterface"] = None) -> None:
        super().__init__(interface, bitrate=None)
        from collections import deque
        self._queue: "deque[CanMessage]" = deque()
        self._queue_lock = threading.Condition()
        self.peer: Optional["MockCanInterface"] = peer

    def open(self) -> None:
        with self._lock:
            self._is_open = True

    def close(self) -> None:
        with self._lock:
            self._is_open = False
            # Wake up any waiting receivers
            with self._queue_lock:
                self._queue_lock.notify_all()

    def _deliver(self, message: CanMessage) -> None:
        """Internal helper to deliver a message to the local queue."""
        with self._queue_lock:
            self._queue.append(message)
            self._queue_lock.notify()

    def receive(self, timeout: Optional[float] = None) -> Optional[CanMessage]:
        if not self._is_open:
            raise RuntimeError("MockCanInterface is not open")
        with self._queue_lock:
            if not self._queue and timeout == 0:
                return None
            end_time = time.monotonic() + timeout if timeout is not None else None
            while not self._queue:
                remaining = None
                if end_time is not None:
                    remaining = end_time - time.monotonic()
                    if remaining <= 0:
                        return None
                self._queue_lock.wait(timeout=remaining)
            return self._queue.popleft()

    def send(self, message: CanMessage) -> None:
        if not self._is_open:
            raise RuntimeError("MockCanInterface is not open")
        if self.peer is not None:
            # Deliver to the peer's queue
            self.peer._deliver(message)
        else:
            # Loop back to our own queue if no peer is set
            self._deliver(message)


def get_interface(name: str, backend: Optional[str] = None, **kwargs: Any) -> BaseCanInterface:
    """Factory function to instantiate a CAN interface.

    Parameters
    ----------
    name : str
        The OS name of the interface (e.g. ``can0``) or a special token such
        as ``mock``.
    backend : str, optional
        Force the backend to use: ``'socketcan'``, ``'python-can'`` or
        ``'mock'``.  If unspecified, a reasonable default is chosen based on
        availability and the name.
    **kwargs : any
        Additional arguments forwarded to the backend constructor.  For
        ``python-can`` this may include ``channel`` and ``bustype``.

    Returns
    -------
    BaseCanInterface
        An instance ready to be opened.
    """
    # Determine backend automatically if not specified
    if backend is None:
        # Use mock if name is 'mock' or 'virtual'
        if name.lower() in ("mock", "virtual", "test"):
            backend = "mock"
        # Prefer python‑can if available
        elif _HAVE_PYTHON_CAN:
            backend = "python-can"
        # Fallback to socketcan if available
        elif _HAVE_SOCKET and hasattr(socket, "AF_CAN"):
            backend = "socketcan"
        else:
            raise RuntimeError("No suitable CAN backend available")
    backend = backend.lower()
    if backend == "socketcan":
        return SocketCanInterface(interface=name, bitrate=kwargs.get("bitrate"))
    elif backend == "python-can":
        return PythonCanInterface(interface=name, bitrate=kwargs.get("bitrate"), channel=kwargs.get("channel"), bustype=kwargs.get("bustype"), **kwargs)
    elif backend == "mock":
        # For mocks you may specify a peer via kwargs
        peer = kwargs.get("peer")
        return MockCanInterface(interface=name, peer=peer)
    else:
        raise ValueError(f"Unsupported backend {backend!r}")