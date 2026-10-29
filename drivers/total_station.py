"""
Hardware, Serial, Bluetooth (RFCOMM / SPP / BLE UART), Wi-Fi / Ethernet TCP/IP,
UDP Stream, and Multi-Protocol Virtual Emulator Drivers for Modern EDM.

Supports all 12 instrument protocols:
1. LeicaGeoCOMDriver (WILD2 / LEICA GeoCOM ASCII RPC - TPS1000/1100/1200, Viva TS11-16, Nova MS50/60)
2. LeicaAutoTiltDriver (LEICA_AUTOTILT 11-step motorized GeoCOM lock & tilt compensation)
3. LeicaGSIDriver (WILD / LEICA GSI-8 & GSI-16 - TC400-TC2000, TCR300-800)
4. LeicaBuilderDriver (BUILDER - Leica Builder 100-500 / iCON '$MSR' / '/Dc ')
5. TopconDriver (TOPCON GTS-3, 210, 220, 301-303, 320, GM/GT/OS with BCC checksum & barrel-reversal checks)
6. SokkiaDriver & NikonDriver (SOKKIA SET / CX / SX / iX '$DNX' / DC1 0x11 & NIKON DTM / NPL / Nivo '!HAN' with DC2 0x12 abort)
7. TrimbleM3Driver (TRIMBLE / GEODIMETER GDS / M3 / 5600 / S-Series 'RG=1' / Label 37=Hz, 38=V, 39=SD)
8. ZeissRec500Driver (ZEISS Elta / REC-500 ASCII angle/distance frames)
9. MicroscribeDriver (MICROSCRIBE / PENTOGRAPH 'GETPOS' & Clipboard mm-to-m parser)
10. ManualDriver (MANUAL / NONE hand-entered V/Hz/SD or X/Y/Z)
11. SimulatorDriver (SIMULATE offline synthetic coordinate generator)

Supports 6 Physical & Virtual Transport Mechanisms:
- RS-232 / USB-Serial UART (`COM1`..`COM256`, `/dev/ttyUSB*`, `/dev/cu.usbserial*`)
- Bluetooth Classic SPP / Virtual COM (`Standard Serial over Bluetooth link`, `/dev/cu.Bluetooth*`)
- Direct Bluetooth RFCOMM Socket (`bt://AA:BB:CC:DD:EE:FF:1` or `rfcomm://AA:BB:CC:DD:EE:FF:1` via Python `socket.AF_BLUETOOTH`)
- Bluetooth Low Energy (BLE) GATT Nordic UART (`ble://AA:BB:CC:DD:EE:FF` or device name via `bleak` when available)
- Wi-Fi / Ethernet TCP/IP Socket (`tcp://192.168.1.100:5000` or `socket://host:port` for robotic Wi-Fi bridges, NPort, & pyserial RFC2217)
- UDP Datagram Bridge (`udp://127.0.0.1:5005`)
- Multi-Protocol Virtual Hardware Emulators (`SIMULATOR`, `VIRT-LEICA`, `VIRT-TOPCON`, `VIRT-SOKKIA`, `VIRT-TRIMBLE`, `VIRT-MICROSCRIBE`, `BT-SIM-LEICA`, `TCP-SIM-STATION`)
"""

from __future__ import annotations

import abc
import math
import random
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import serial
    import serial.tools.list_ports
    HAS_PYSERIAL = True
except ImportError:
    HAS_PYSERIAL = False


@dataclass(frozen=True)
class RawObservation:
    """Standardized observation returned by any Total Station driver."""
    slope_distance: float           # Meters (offset +0x0C in edmwin.exe)
    horizontal_angle_rad: float     # Radians [0, 2*pi) (offset +0x10 in edmwin.exe)
    vertical_angle_rad: float       # Radians Zenith angle (offset +0x14 in edmwin.exe)
    tilt_compensation: float = 0.0  # Cross/length tilt compensation (offset +0x18 / +0x20)
    direct_xyz: Optional[Tuple[float, float, float]] = None  # Used by Microscribe or Manual XYZ mode
    raw_tx: str = ""
    raw_rx: str = ""

    @property
    def horizontal_angle_deg(self) -> float:
        return math.degrees(self.horizontal_angle_rad) % 360.0

    @property
    def vertical_angle_deg(self) -> float:
        return math.degrees(self.vertical_angle_rad)

    @property
    def horizontal_angle_dddmmss(self) -> float:
        return radians_to_dddmmss(self.horizontal_angle_rad)

    @property
    def vertical_angle_dddmmss(self) -> float:
        return radians_to_dddmmss(self.vertical_angle_rad)


@dataclass
class SerialConfig:
    """
    Unified transport & serial configuration.
    `port` accepts:
      - Standard OS COM/tty ports: 'COM1', '/dev/ttyUSB0', '/dev/cu.Bluetooth-Incoming-Port'
      - Direct Bluetooth RFCOMM URIs: 'bt://00:11:22:33:44:55:1' or 'rfcomm://00:11:22:33:44:55:1'
      - Bluetooth Low Energy (BLE) URIs: 'ble://00:11:22:33:44:55'
      - Wi-Fi / Ethernet TCP/IP URIs: 'tcp://192.168.1.50:5000' or 'socket://192.168.1.50:5000'
      - UDP URIs: 'udp://127.0.0.1:9000'
      - Built-in Virtual Emulators: 'SIMULATOR', 'VIRT-LEICA', 'VIRT-TOPCON', 'VIRT-SOKKIA', 'VIRT-TRIMBLE', 'BT-SIM-LEICA', 'TCP-SIM-STATION'
    """
    port: str = "COM1"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"       # 'N', 'E', 'O'
    stopbits: int = 1
    timeout: float = 10.0
    edm_delay_time: float = 0.5  # EdmDelayTime from frmTheodolite (default 0.5s)
    dtr: bool = True
    rts: bool = True
    flow_control: str = "NONE"   # 'NONE', 'RTSCTS', 'XONXOFF'
    verify_bcc: bool = False
    check_barrel_reversal: bool = False


def dms_encoded_to_radians(dms_val: float) -> float:
    """
    Converts packed DDDMMSS or DDD.MMSS values into radians.
    Matches FUN_004cdb10 & FUN_004c8400 in edmwin.exe.
    """
    sign = -1.0 if dms_val < 0 else 1.0
    val = abs(dms_val)
    if val > 360.0:
        ival = int(round(val))
        deg = ival // 10000
        rem = ival % 10000
        minutes = rem // 100
        seconds = float(rem % 100)
    else:
        deg = int(val)
        rem = (val - deg) * 100.0
        minutes = int(round(rem + 1e-9))
        seconds = (rem - minutes) * 100.0
    decimal_deg = sign * (deg + minutes / 60.0 + seconds / 3600.0)
    return math.radians(decimal_deg)


def radians_to_dddmmss(angle_rad: float) -> float:
    """
    Exact replica of FUN_004c8400 + FUN_004c8540 in edmwin.exe:
    1. FUN_004c8400: decimal_deg = (angle_rad / (2*pi)) * 360.0
    2. FUN_004c8540: decomposes decimal_deg into Deg + Min/100 + Sec/10000 (DDD.MMSS).
    """
    decimal_deg = math.degrees(angle_rad) % 360.0
    deg = int(decimal_deg)
    rem_sec = (decimal_deg - deg) * 3600.0
    minutes = int(rem_sec // 60.0)
    seconds = int(round(rem_sec - minutes * 60.0))
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg = (deg + 1) % 360
    formatted = f"{deg}.{minutes:02d}{seconds:02d}"
    return float(formatted)


def format_dddmmss_8digit(ddd_mmss: float) -> str:
    """
    Formats DDD.MMSS into an 8-digit integer string 'DDDMMSS0' or '0DDDMMSS'
    matching FUN_004cdb10 for GSI / Sokkia / Nikon horizontal angle setting.
    """
    rad = dms_encoded_to_radians(ddd_mmss)
    dec_deg = math.degrees(rad) % 360.0
    deg = int(dec_deg)
    rem_sec = (dec_deg - deg) * 3600.0
    minutes = int(rem_sec // 60.0)
    sec_tenths = int(round((rem_sec - minutes * 60.0) * 10.0))
    if sec_tenths >= 600:
        sec_tenths -= 600
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg = (deg + 1) % 360
    return f"{deg:03d}{minutes:02d}{sec_tenths:03d}"


def format_dddmmss_7digit(ddd_mmss: float) -> str:
    """
    Formats DDD.MMSS into a 7-digit integer string 'DDDMMSS' (3-digit deg, 2-digit min, 2-digit sec)
    matching FUN_004cdb10 for Topcon and Sokkia horizontal angle presetting.
    """
    rad = dms_encoded_to_radians(ddd_mmss)
    dec_deg = math.degrees(rad) % 360.0
    deg = int(dec_deg)
    rem_sec = (dec_deg - deg) * 3600.0
    minutes = int(rem_sec // 60.0)
    seconds = int(round(rem_sec - minutes * 60.0))
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg = (deg + 1) % 360
    return f"{deg:03d}{minutes:02d}{seconds:02d}"


def compute_topcon_bcc(payload: str) -> str:
    """Computes XOR Block Check Character (BCC) for Topcon standard frames."""
    bcc = 0
    for ch in payload:
        bcc ^= ord(ch)
    return f"{bcc:02X}"


def compute_topcon_bcc_decimal3(payload: str) -> str:
    """
    Replicates FUN_004c57d0 in edmwin.exe:
    Computes the 3-digit decimal ASCII parity/BCC suffix used on Topcon GTS-3B/t60 frames
    (e.g., '?+00002413m0893100+0000000d+00002413t60+00+25107' -> '107').
    """
    bcc = 0
    for ch in payload:
        bcc ^= ord(ch)
    return f"{bcc:03d}"


# ==============================================================================
# Multi-Transport Socket & Bluetooth Stream Wrappers
# ==============================================================================

class _SocketStreamAdapter:
    """
    Wraps a Python `socket.socket` (TCP/IP, UDP, or Bluetooth RFCOMM `AF_BLUETOOTH`)
    in a pyserial-compatible interface (`is_open`, `write`, `read`, `in_waiting`, `close`,
    `reset_input_buffer`, `reset_output_buffer`) so all TotalStationDriver subclasses
    work identically over Serial, Bluetooth RFCOMM, Wi-Fi TCP, or UDP!
    """

    def __init__(
        self,
        sock: socket.socket,
        timeout: float = 10.0,
        udp_target: Optional[Tuple[str, int]] = None,
    ) -> None:
        self._sock = sock
        self._sock.settimeout(0.05)
        self.timeout = timeout
        self._udp_target = udp_target
        self._rx_buf = bytearray()
        self.is_open = True
        self.dtr = True
        self.rts = True

    def _pump_recv(self) -> None:
        if not self.is_open:
            return
        try:
            if self._udp_target is not None:
                chunk, _ = self._sock.recvfrom(4096)
            else:
                chunk = self._sock.recv(4096)
            if chunk:
                self._rx_buf.extend(chunk)
        except (BlockingIOError, socket.timeout, TimeoutError):
            pass
        except OSError:
            pass

    @property
    def in_waiting(self) -> int:
        self._pump_recv()
        return len(self._rx_buf)

    def read(self, size: int = 1) -> bytes:
        self._pump_recv()
        n = min(size, len(self._rx_buf))
        data = bytes(self._rx_buf[:n])
        del self._rx_buf[:n]
        return data

    def write(self, data: bytes) -> int:
        if self._udp_target is not None:
            return self._sock.sendto(data, self._udp_target)
        self._sock.sendall(data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._pump_recv()
        self._rx_buf.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        if self.is_open:
            self.is_open = False
            try:
                self._sock.close()
            except OSError:
                pass


def parse_transport_endpoint(port_spec: str) -> Dict[str, Any]:
    """
    Parses a connection string/URI into structured transport metadata:
      - 'bt://AA:BB:CC:DD:EE:FF:1' or 'rfcomm://AA:BB:CC:DD:EE:FF:1' -> {'scheme': 'BT_RFCOMM', 'address': 'AA:BB:CC:DD:EE:FF', 'channel': 1}
      - 'ble://AA:BB:CC:DD:EE:FF' -> {'scheme': 'BLE_UART', 'address': 'AA:BB:CC:DD:EE:FF'}
      - 'tcp://192.168.1.100:5000' or 'socket://192.168.1.100:5000' -> {'scheme': 'TCP', 'host': '192.168.1.100', 'port': 5000}
      - 'udp://127.0.0.1:5005' -> {'scheme': 'UDP', 'host': '127.0.0.1', 'port': 5005}
      - 'SIMULATOR' / 'VIRT-*' / 'BT-SIM-*' / 'TCP-SIM-*' -> {'scheme': 'VIRTUAL', 'port': port_spec}
      - 'COM3' / '/dev/ttyUSB0' -> {'scheme': 'SERIAL', 'port': port_spec}
    """
    raw = (port_spec or "").strip()
    upper = raw.upper()

    if TotalStationDriver.is_virtual_port(raw):
        return {"scheme": "VIRTUAL", "port": raw}

    if upper.startswith(("BT://", "RFCOMM://")):
        body = raw.split("://", 1)[1].strip().strip("/")
        mac_match = re.match(r"^([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?::(\d+))?$", body)
        if not mac_match:
            raise ValueError(
                f"Invalid Bluetooth RFCOMM URI {raw!r}. Expected format: bt://AA:BB:CC:DD:EE:FF:1"
            )
        mac_addr = mac_match.group(1).upper()
        channel = int(mac_match.group(2) or "1")
        return {"scheme": "BT_RFCOMM", "address": mac_addr, "channel": channel}

    if upper.startswith("BLE://"):
        addr = raw.split("://", 1)[1].strip().strip("/")
        if not addr:
            raise ValueError(f"Invalid BLE URI {raw!r}. Expected format: ble://AA:BB:CC:DD:EE:FF")
        return {"scheme": "BLE_UART", "address": addr}

    if upper.startswith(("TCP://", "SOCKET://")):
        body = raw.split("://", 1)[1].strip().strip("/")
        if ":" not in body:
            raise ValueError(f"Invalid TCP URI {raw!r}. Expected format: tcp://host:port")
        host, port_s = body.rsplit(":", 1)
        return {"scheme": "TCP", "host": host.strip(), "port": int(port_s)}

    if upper.startswith("UDP://"):
        body = raw.split("://", 1)[1].strip().strip("/")
        if ":" not in body:
            raise ValueError(f"Invalid UDP URI {raw!r}. Expected format: udp://host:port")
        host, port_s = body.rsplit(":", 1)
        return {"scheme": "UDP", "host": host.strip(), "port": int(port_s)}

    return {"scheme": "SERIAL", "port": raw}


def discover_bluetooth_devices() -> List[Tuple[str, str]]:
    """
    Discovers paired and nearby Bluetooth Total Stations / Serial Adapters across platforms:
    1. Scans OS serial ports (`serial.tools.list_ports.comports()`) for Bluetooth SPP
       descriptions (`Bluetooth`, `BTHENUM`, `SPP`, `RFCOMM`, `Parani`, `RN42`, `Leica`, `Topcon`, `Sokkia`, `Trimble`).
    2. On Windows, queries `Get-PnpDevice -Class Bluetooth` (PowerShell) to surface paired
       Bluetooth survey instruments and their MAC addresses even before a COM port is opened.
    3. Includes virtual Bluetooth & Wi-Fi test endpoints (`BT-SIM-LEICA`, `BT-SIM-TOPCON`, `TCP-SIM-STATION`)
       so Bluetooth and network workflows can be tested without physical hardware.
    """
    found: List[Tuple[str, str]] = []
    seen_ids: set[str] = set()

    if HAS_PYSERIAL:
        bt_keywords = ("bluetooth", "bthenum", "rfcomm", "spp", "parani", "rn42", "serial over bluetooth")
        for p in serial.tools.list_ports.comports():
            desc_lower = f"{p.device} {p.description} {getattr(p, 'hwid', '')}".lower()
            if any(k in desc_lower for k in bt_keywords):
                dev_id = p.device
                label = f"{p.device} - [Bluetooth SPP] {p.description}"
                if dev_id not in seen_ids:
                    found.append((dev_id, label))
                    seen_ids.add(dev_id)

    if sys.platform == "win32":
        try:
            ps_cmd = (
                "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                "Where-Object { $_.Status -eq 'OK' -and $_.FriendlyName -notmatch 'Enumerator|Adapter|Radio|Transport|Avrcp|Audio' } | "
                "Select-Object -First 8 FriendlyName, InstanceId | ConvertTo-Csv -NoTypeInformation"
            )
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=3.5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for line in proc.stdout.splitlines()[1:]:
                    parts = [p.strip('"') for p in line.split('","')]
                    if len(parts) >= 2:
                        fname, inst_id = parts[0], parts[1]
                        mac_m = re.search(r"([0-9A-Fa-f]{12})$", inst_id.replace("_", ""))
                        if mac_m:
                            raw_hex = mac_m.group(1).upper()
                            mac_colon = ":".join(raw_hex[i:i+2] for i in range(0, 12, 2))
                            bt_uri = f"bt://{mac_colon}:1"
                            if bt_uri not in seen_ids:
                                found.append((bt_uri, f"{bt_uri} - [Paired Bluetooth RFCOMM] {fname}"))
                                seen_ids.add(bt_uri)
        except Exception:
            pass

    sim_bt = [
        ("BT-SIM-LEICA", "BT-SIM-LEICA - [Simulated Bluetooth SPP] Leica TPS / TS16 GeoCOM"),
        ("BT-SIM-TOPCON", "BT-SIM-TOPCON - [Simulated Bluetooth SPP] Topcon GT / OS / GM Series"),
        ("BT-SIM-SOKKIA", "BT-SIM-SOKKIA - [Simulated Bluetooth SPP] Sokkia CX / SX / iX Series"),
        ("TCP-SIM-STATION", "TCP-SIM-STATION - [Simulated Wi-Fi TCP/IP] Robotic Total Station Bridge"),
    ]
    for dev_id, label in sim_bt:
        if dev_id not in seen_ids:
            found.append((dev_id, label))
            seen_ids.add(dev_id)

    return found


class TotalStationDriver(abc.ABC):
    """Abstract base class for all Total Station serial, Bluetooth, and network drivers."""

    VIRTUAL_PORTS = [
        ("SIMULATOR", "SIMULATOR - Built-in Multi-Protocol Hardware Emulator"),
        ("VIRT-LEICA", "VIRT-LEICA - Virtual Leica GeoCOM / AutoTilt / GSI Port"),
        ("VIRT-TOPCON", "VIRT-TOPCON - Virtual Topcon GTS / GM Series Port (with BCC)"),
        ("VIRT-SOKKIA", "VIRT-SOKKIA - Virtual Sokkia / Nikon SET Series Port"),
        ("VIRT-TRIMBLE", "VIRT-TRIMBLE - Virtual Trimble M3 / Geodimeter 5600 / S-Series Port"),
        ("VIRT-MICROSCRIBE", "VIRT-MICROSCRIBE - Virtual Microscribe 3D Digitizer Port"),
        ("BT-SIM-LEICA", "BT-SIM-LEICA - Virtual Bluetooth SPP Leica Total Station"),
        ("BT-SIM-TOPCON", "BT-SIM-TOPCON - Virtual Bluetooth SPP Topcon Total Station"),
        ("TCP-SIM-STATION", "TCP-SIM-STATION - Virtual Wi-Fi / TCP Total Station Bridge"),
    ]

    def __init__(
        self,
        config: SerialConfig,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.config = config
        self._serial: Optional[Any] = None
        self._log_callback = log_callback
        self._virtual_connected: bool = self.is_virtual_port(config.port)
        self._sim_rng = random.Random(42)

    @staticmethod
    def is_virtual_port(port_name: str) -> bool:
        p = (port_name or "").strip().upper()
        return (
            p == "SIMULATOR"
            or p.startswith("SIM")
            or p.startswith("VIRT")
            or p.startswith("BT-SIM")
            or p.startswith("TCP-SIM")
            or p == "LOOPBACK"
        )

    def _log(self, direction: str, payload: str) -> None:
        if self._log_callback:
            self._log_callback(direction, payload)

    @classmethod
    def list_available_ports(cls, include_bluetooth_scan: bool = False) -> List[Tuple[str, str]]:
        ports: List[Tuple[str, str]] = []
        seen: set[str] = set()
        if HAS_PYSERIAL:
            for p in serial.tools.list_ports.comports():
                desc = p.description or "Serial Port"
                is_bt = any(
                    k in f"{p.device} {desc} {getattr(p, 'hwid', '')}".lower()
                    for k in ("bluetooth", "bthenum", "rfcomm", "spp")
                )
                tag = "[Bluetooth SPP] " if is_bt else ""
                ports.append((p.device, f"{p.device} - {tag}{desc}"))
                seen.add(p.device)

        if include_bluetooth_scan:
            for dev_id, label in discover_bluetooth_devices():
                if dev_id not in seen:
                    ports.append((dev_id, label))
                    seen.add(dev_id)

        for v_id, v_desc in cls.VIRTUAL_PORTS:
            if v_id not in seen:
                ports.append((v_id, v_desc))
                seen.add(v_id)
        return ports

    def connect(self) -> None:
        if self._serial and getattr(self._serial, "is_open", False):
            self._serial.close()

        ep = parse_transport_endpoint(self.config.port)
        scheme = ep["scheme"]

        if scheme == "VIRTUAL":
            self._virtual_connected = True
            self._log(
                "INFO",
                f"Connected to virtual hardware emulator on {self.config.port} "
                f"({self.__class__.__name__} @ {self.config.baudrate},{self.config.parity},{self.config.bytesize},{self.config.stopbits})",
            )
            return

        if scheme == "BT_RFCOMM":
            if not hasattr(socket, "AF_BLUETOOTH"):
                raise RuntimeError(
                    "Direct Bluetooth RFCOMM sockets (AF_BLUETOOTH) are not supported by this Python build; "
                    "pair the instrument in OS Bluetooth settings and select its Bluetooth SPP COM port instead."
                )
            bt_proto = getattr(socket, "BTPROTO_RFCOMM", 3)
            sock = socket.socket(getattr(socket, "AF_BLUETOOTH"), socket.SOCK_STREAM, bt_proto)
            sock.settimeout(self.config.timeout)
            sock.connect((ep["address"], ep["channel"]))
            self._serial = _SocketStreamAdapter(sock, timeout=self.config.timeout)
            self._log("INFO", f"Connected via Bluetooth RFCOMM to {ep['address']} (channel {ep['channel']})")
            return

        if scheme == "TCP":
            sock = socket.create_connection((ep["host"], ep["port"]), timeout=self.config.timeout)
            self._serial = _SocketStreamAdapter(sock, timeout=self.config.timeout)
            self._log("INFO", f"Connected via TCP/IP to {ep['host']}:{ep['port']}")
            return

        if scheme == "UDP":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._serial = _SocketStreamAdapter(
                sock, timeout=self.config.timeout, udp_target=(ep["host"], ep["port"])
            )
            self._log("INFO", f"Opened UDP socket to {ep['host']}:{ep['port']}")
            return

        if scheme == "BLE_UART":
            # Support pyserial url handler if registered, or raise descriptive BLE guidance
            if HAS_PYSERIAL and hasattr(serial, "serial_for_url"):
                try:
                    self._serial = serial.serial_for_url(self.config.port, timeout=self.config.timeout)
                    return
                except Exception as exc:
                    raise RuntimeError(
                        f"BLE UART connection to {ep['address']} failed ({exc}). "
                        "Use a Bluetooth SPP COM port or bt://MAC:1 RFCOMM URI."
                    ) from exc

        if not HAS_PYSERIAL:
            raise RuntimeError("pyserial is not installed.")

        fc = (self.config.flow_control or "NONE").strip().upper()
        self._serial = serial.Serial(
            port=self.config.port,
            baudrate=self.config.baudrate,
            bytesize=self.config.bytesize,
            parity=self.config.parity,
            stopbits=self.config.stopbits,
            timeout=self.config.timeout,
            rtscts=(fc == "RTSCTS"),
            xonxoff=(fc == "XONXOFF"),
        )
        self._serial.dtr = self.config.dtr
        self._serial.rts = self.config.rts
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def disconnect(self) -> None:
        if self._serial and getattr(self._serial, "is_open", False):
            self._serial.close()
        self._serial = None
        self._virtual_connected = False

    def reset_connection(self) -> None:
        """Implements Station -> Reset Connection (FUN_005369d0)."""
        self.disconnect()
        self.connect()
        self.initialize_station_comm()

    @property
    def is_connected(self) -> bool:
        if self.is_virtual_port(self.config.port):
            return self._virtual_connected
        return bool(self._serial and getattr(self._serial, "is_open", False))

    def _emulate_protocol_response(self, data: str) -> str:
        """
        Generates authentic wire-protocol responses for ANY TotalStationDriver subclass
        when connected to a virtual port (SIMULATOR, VIRT-LEICA, VIRT-TOPCON, BT-SIM-LEICA, etc.).
        """
        cmd = data.strip()
        sd = round(self._sim_rng.uniform(1.15, 2.45), 4)
        hz_deg = round(self._sim_rng.uniform(35.0, 55.0), 4)
        v_deg = round(self._sim_rng.uniform(91.0, 96.0), 4)
        hz_rad = math.radians(hz_deg)
        v_rad = math.radians(v_deg)

        # 1. Leica GeoCOM / AutoTilt ASCII RPC commands
        if cmd.startswith("%R1Q"):
            if ",5008:" in cmd:
                return "%R1P,0,0:0,2026,'10','28','16','45','00'\r\n"
            if ",5004" in cmd:
                return '%R1P,0,0:0,"TPS1200+ Virtual Emulator"\r\n'
            if ",2187:" in cmd:
                tokens = (
                    ["0"] + ["0.0000"] * 10 + ["0.0042", "0.0000"]
                    + [f"{hz_rad:.8f}", f"{v_rad:.8f}", f"{sd:.4f}"]
                )
                return "%R1P,0,0:" + ",".join(tokens) + "\r\n"
            if ",2108:" in cmd:
                return f"%R1P,0,0:0,{hz_rad:.8f},{v_rad:.8f},{sd:.4f}\r\n"
            return "%R1P,0,0:0\r\n"

        # 2. Leica / Wild GSI-8 / GSI-16 & Builder 209 commands
        if cmd.startswith("SET/") or cmd.startswith("PUT/") or cmd.startswith("/Dc"):
            return "*\r\n"
        if cmd.startswith("GET/M/") or cmd == "$MSR":
            hz_gsi = int(round(hz_deg * 100000))
            v_gsi = int(round(v_deg * 100000))
            sd_mm = int(round(sd * 1000))
            return f"110001+00000001 21.323+{hz_gsi:08d} 22.323+{v_gsi:08d} 31..00+{sd_mm:08d}\r\n"

        # 3. Trimble M3 / Geodimeter 5600 / S-Series GDS commands
        if cmd.startswith("RG") or isinstance(self, TrimbleM3Driver):
            hz_dms = radians_to_dddmmss(hz_rad)
            v_dms = radians_to_dddmmss(v_rad)
            return f"37={hz_dms:.4f}\r\n38={v_dms:.4f}\r\n39={sd:.4f}\r\n"

        # 4. Zeiss Elta / REC-500 commands
        if isinstance(self, ZeissRec500Driver):
            hz_dms = radians_to_dddmmss(hz_rad)
            v_dms = radians_to_dddmmss(v_rad)
            return f"0001  SD {sd:8.4f}  Hz {hz_dms:8.4f}  V {v_dms:8.4f}\r\n"

        # 5. Topcon GTS / GM Series commands
        if "\x06R" in data or cmd.startswith("!HAN"):
            return "\x06\r\n"
        if "\x15?" in data or isinstance(self, TopconDriver):
            sd_mm = int(round(sd * 1000))
            v_dms = format_dddmmss_7digit(radians_to_dddmmss(v_rad))
            hz_dms = format_dddmmss_7digit(radians_to_dddmmss(hz_rad))
            body = f"?+{sd_mm:07d}m+{v_dms}d+{hz_dms}d+{sd_mm:07d}d"
            bcc = compute_topcon_bcc(body)
            return f"{body}{bcc}\r\n"

        # 6. Microscribe 3D Digitizer commands
        if cmd.upper() == "GETPOS" or isinstance(self, MicroscribeDriver):
            x_mm = round(100400.0 + self._sim_rng.uniform(50.0, 950.0), 2)
            y_mm = round(100400.0 + self._sim_rng.uniform(50.0, 950.0), 2)
            z_mm = round(9850.0 + self._sim_rng.uniform(-150.0, 150.0), 2)
            return f"{x_mm}, {y_mm}, {z_mm}\r\n"

        # 7. Sokkia / Nikon SET Series commands
        if "\x11" in data or isinstance(self, SokkiaNikonDriver):
            sd_mm = int(round(sd * 1000))
            v_dms = format_dddmmss_7digit(radians_to_dddmmss(v_rad))
            hz_dms = format_dddmmss_7digit(radians_to_dddmmss(hz_rad))
            return f"E 112 00.00 00.000\r\n {sd_mm:07d} {v_dms} {hz_dms}\r\n"

        return "OK\r\n"

    def send_raw(self, data: str) -> str:
        if self.is_virtual_port(self.config.port):
            self._virtual_connected = True
            self._log("Sent->", repr(data))
            rx = self._emulate_protocol_response(data)
            self._log("Received->", repr(rx))
            return rx

        if not self.is_connected or self._serial is None:
            self.connect()
        assert self._serial is not None
        self._serial.reset_input_buffer()
        self._log("Sent->", repr(data))
        self._serial.write(data.encode("ascii", errors="ignore"))
        self._serial.flush()

        chunks: List[bytes] = []
        deadline = time.monotonic() + self.config.timeout
        while time.monotonic() < deadline:
            waiting = self._serial.in_waiting
            if waiting > 0:
                chunk = self._serial.read(waiting)
                chunks.append(chunk)
                joined = b"".join(chunks)
                if joined.endswith(b"\r\n") or joined.endswith(b"\n") or b"\x03" in joined:
                    break
            else:
                time.sleep(0.03)

        rx = b"".join(chunks).decode("ascii", errors="replace")
        self._log("Received->", repr(rx))
        return rx

    def initialize_station_comm(self) -> str:
        """Runs protocol-specific initialization handshake (FUN_004c7de0)."""
        return "OK"

    def set_horizontal_angle(self, angle_dddmmss: float) -> str:
        """Sets horizontal angle on the instrument (FUN_004cdb10)."""
        cmd = self.format_set_horizontal_angle_cmd(angle_dddmmss)
        if not cmd:
            return "No-op"
        return self.send_raw(cmd)

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        """Returns the exact wire string to set the horizontal circle on this station."""
        return ""

    @abc.abstractmethod
    def measure(self) -> RawObservation:
        """Triggers measurement and returns parsed RawObservation."""

    @abc.abstractmethod
    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        """Parses a station protocol response frame into a RawObservation."""


class LeicaGeoCOMDriver(TotalStationDriver):
    """
    Driver for Leica WILD2 / GeoCOM Total Stations (FUN_004c7de0, FUN_004cad10, FUN_004c8840).
    """

    NULL_CHECK_CMD = "%R1Q,0:\r\n"
    SET_PRISM_CMD = "%R1Q,17021:0\r\n"
    DO_MEASURE_CMD = "\r\n%R1Q,2008:1,1\r\n"
    GET_SIMPLE_MEA_CMD = "%R1Q,2108:10000,1\r\n"
    GET_DATETIME_CMD = "%R1Q,5008:\r\n"
    GET_INSTR_NAME_CMD = "%R1Q,5004,6,21341:\r\n"

    def initialize_station_comm(self) -> str:
        rx = self.send_raw(self.SET_PRISM_CMD)
        return rx

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        rad = dms_encoded_to_radians(angle_dddmmss) % (2.0 * math.pi)
        return f"%R1Q,2113:{rad:.7f}\r\n"

    def query_datetime(self) -> str:
        """Implements frmDebug 'Get date/time' (FUN_004cc370)."""
        return self.send_raw(self.GET_DATETIME_CMD)

    def query_instrument_name(self) -> str:
        """Implements frmDebug 'Get Instr. Name' (FUN_004cc5a0)."""
        return self.send_raw(self.GET_INSTR_NAME_CMD)

    def measure(self) -> RawObservation:
        ack = self.send_raw(self.DO_MEASURE_CMD)
        if "%R1P" in ack and not ack.strip().endswith(":0"):
            parts = ack.strip().split(":")
            if len(parts) > 1 and parts[-1] != "0":
                raise RuntimeError(f"Leica GeoCOM TMC_DoMeasure error code: {parts[-1]}")
        rx = self.send_raw(self.GET_SIMPLE_MEA_CMD)
        return self.parse_response(rx, tx_cmd=self.DO_MEASURE_CMD.strip() + " ; " + self.GET_SIMPLE_MEA_CMD.strip())

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        clean = response.strip()
        if not clean.startswith("%R1P"):
            raise ValueError(f"Invalid Leica GeoCOM frame (1001): {response!r}")
        if ":" not in clean:
            raise ValueError(f"Malformed GeoCOM response (1002): {response!r}")

        _, payload = clean.split(":", 1)
        tokens = [t.strip() for t in payload.split(",")]
        if len(tokens) < 4:
            raise ValueError(f"GeoCOM payload has fewer than 4 fields (1003): {payload!r}")

        rc = int(tokens[0])
        if rc != 0:
            raise RuntimeError(f"Leica GeoCOM RPC error code: {rc}")

        hz_rad = float(tokens[1]) % (2.0 * math.pi)
        v_rad = float(tokens[2])
        slope_dist = float(tokens[3])

        return RawObservation(
            slope_distance=slope_dist,
            horizontal_angle_rad=hz_rad,
            vertical_angle_rad=v_rad,
            raw_tx=tx_cmd,
            raw_rx=response,
        )


class LeicaAutoTiltDriver(LeicaGeoCOMDriver):
    """
    Driver for Motorized Leica Total Stations with AutoTilt prism tracking (LEICA_AUTOTILT).
    Replicates FUN_004cc890 and FUN_004cd440 line-by-line.
    """

    AUTOTILT_SEQUENCE = [
        ("%R1Q,2183:1\r\n", "An error occurred setting tilt compensation on."),
        ("%R1Q,17021:0\r\n", "An error occurred setting station to use prism."),
        ("%R1Q,17008:3\r\n", "An error occurred setting prism type to 360."),
        ("%R1Q,18007:1\r\n", "An error occurred setting lock on."),
        ("%R1Q,17019:10\r\n", "An error occurred setting continuous EDM on."),
        ("%R1Q,9037:0.1,0.1,0\r\n", "An error occurred searching for the prism."),
        ("%R1Q,9013\r\n", "An error occurred locking onto the target."),
        ("%R1Q,2008:3,1\r\n", "An error occurred starting the measurement mode."),
    ]
    GET_MEA_WITH_TILT_CMD = "%R1Q,2187:10000,1\r\n"
    STOP_MEA_CMD = "%R1Q,2008:0,1\r\n"
    LOCK_OFF_CMD = "%R1Q,9014\r\n"

    @staticmethod
    def _extract_rc(resp: str) -> int:
        if ":" not in resp:
            return -1
        _, tail = resp.strip().split(":", 1)
        first_tok = tail.split(",", 1)[0].strip()
        return int(first_tok) if first_tok.lstrip("-").isdigit() else -1

    def measure(self) -> RawObservation:
        for cmd, err_prefix in self.AUTOTILT_SEQUENCE:
            rx = self.send_raw(cmd)
            rc = self._extract_rc(rx)
            if rc != 0:
                raise RuntimeError(f"{err_prefix} The error code is {rc}")

        meas_rx = self.send_raw(self.GET_MEA_WITH_TILT_CMD)
        self.send_raw(self.STOP_MEA_CMD)
        self.send_raw(self.LOCK_OFF_CMD)
        return self.parse_response(meas_rx, tx_cmd=self.GET_MEA_WITH_TILT_CMD.strip())

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        """
        Replicates FUN_004cd440:
        Parses %R1P,0,0:RC,t2,t3,...,t12(tilt),t13,t14(Hz),t15(V),t16(SD)...
        """
        clean = response.strip()
        if ":" not in clean:
            raise ValueError(f"Malformed AutoTilt response: {response!r}")
        _, payload = clean.split(":", 1)
        tokens = [t.strip() for t in payload.split(",") if t.strip()]
        if not tokens:
            raise ValueError("Empty AutoTilt payload")

        rc = int(float(tokens[0]))
        if rc != 0:
            raise RuntimeError(f"An error occurred getting the measurement with tilt data. The error code is {rc}")

        if len(tokens) >= 16:
            tilt_val = float(tokens[11])
            hz_rad = float(tokens[13]) % (2.0 * math.pi)
            v_rad = float(tokens[14])
            sd_val = float(tokens[15])
        elif len(tokens) >= 4:
            hz_rad = float(tokens[1]) % (2.0 * math.pi)
            v_rad = float(tokens[2])
            sd_val = float(tokens[3])
            tilt_val = float(tokens[4]) if len(tokens) >= 5 else 0.0
        else:
            raise ValueError(f"AutoTilt payload too short: {payload!r}")

        return RawObservation(
            slope_distance=sd_val,
            horizontal_angle_rad=hz_rad,
            vertical_angle_rad=v_rad,
            tilt_compensation=tilt_val,
            raw_tx=tx_cmd,
            raw_rx=response,
        )


class LeicaGSIDriver(TotalStationDriver):
    """
    Driver for Leica / Wild Total Stations using GSI-8 or GSI-16 format (WILD / LEICA).
    Replicates FUN_004c7de0, FUN_004cad10, FUN_004c8840, and FUN_004cdb10.
    """

    INIT_CMD_1 = "SET/41/0\r\n"
    INIT_CMD_2 = "SET/149/2\r\n"
    TRIGGER_CMD = "GET/M/WI11/WI21/WI22/WI31/WI51\r\n"

    def initialize_station_comm(self) -> str:
        self.send_raw(self.INIT_CMD_1)
        self.send_raw(self.INIT_CMD_2)
        time.sleep(self.config.edm_delay_time)
        return "OK"

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        digits8 = format_dddmmss_8digit(angle_dddmmss)
        return f"PUT/21...4+{digits8} \r\n"

    def measure(self) -> RawObservation:
        rx = self.send_raw(self.TRIGGER_CMD)
        return self.parse_response(rx, tx_cmd=self.TRIGGER_CMD.strip())

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        clean = response.strip().lstrip("*")
        if not clean:
            raise ValueError("Empty Leica GSI response")

        words = clean.split()
        hz_rad: Optional[float] = None
        v_rad: Optional[float] = None
        slope_dist: Optional[float] = None

        for word in words:
            w = word.lstrip("*")
            if len(w) < 7:
                continue
            wi = w[0:2]
            unit_code = w[5]
            sign_char = w[6]
            raw_digits = w[7:]
            sign = -1.0 if sign_char == "-" else 1.0

            if not raw_digits.isdigit():
                continue
            val_int = int(raw_digits)

            if wi in ("21", "22"):
                if unit_code == "2":
                    gons = sign * (val_int * 1e-5)
                    angle_rad = gons * (math.pi / 200.0)
                elif unit_code == "3":
                    deg = sign * (val_int * 1e-5)
                    angle_rad = math.radians(deg)
                elif unit_code == "4":
                    deg = val_int // 100000
                    rem = val_int % 100000
                    minutes = rem // 1000
                    sec = (rem % 1000) / 10.0
                    angle_rad = math.radians(sign * (deg + minutes / 60.0 + sec / 3600.0))
                else:
                    raise ValueError("ERORR: Angles not in degrees. Reset instrument.")

                if wi == "21":
                    hz_rad = angle_rad % (2.0 * math.pi)
                else:
                    v_rad = angle_rad

            elif wi == "31":
                if unit_code == "0":
                    slope_dist = sign * (val_int / 1000.0)
                elif unit_code == "6":
                    slope_dist = sign * (val_int / 10000.0)
                elif unit_code == "1":
                    slope_dist = sign * (val_int / 1000.0) * 0.3048
                else:
                    slope_dist = sign * (val_int / 1000.0)

        if hz_rad is None or v_rad is None or slope_dist is None:
            raise ValueError(f"Incomplete GSI observation in frame: {response!r}")

        return RawObservation(
            slope_distance=slope_dist,
            horizontal_angle_rad=hz_rad,
            vertical_angle_rad=v_rad,
            raw_tx=tx_cmd,
            raw_rx=response,
        )


class LeicaBuilderDriver(LeicaGSIDriver):
    """
    Driver for Leica Builder 209 (BUILDER mode in edmwin.exe at 0x00431474 / 0x00431c40).
    Uses '$MSR' to trigger measurement and '/Dc ' to set horizontal angle.
    """

    TRIGGER_CMD = "$MSR\r\n"

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        return f"/Dc {angle_dddmmss:.4f}\r\n"


class TopconDriver(TotalStationDriver):
    """
    Driver for Topcon GTS-3, GTS-210, GTS-220, GTS-301/302/303, GTS-320, and GM-55.
    Supports both standard Topcon frames (?+0012345m+0900000d+1800000d...) and
    EDMWin GTS-3B/t60 frames (?+00002413m0893100+0000000d+00002413t60+00+25107),
    including BCC checksum verification (FUN_004c57d0) and barrel-reversal checks.
    """

    TRIGGER_CMD = "\x06R\x03\r\n"
    READ_CMD = "\x15?\x03\r\n"

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        digits8 = format_dddmmss_8digit(angle_dddmmss)
        return f"!HAN{digits8}\r\n"

    def measure(self) -> RawObservation:
        self.send_raw(self.TRIGGER_CMD)
        time.sleep(self.config.edm_delay_time)
        rx = self.send_raw(self.READ_CMD)
        return self.parse_response(rx, tx_cmd="R ; ?")

    @staticmethod
    def verify_frame_bcc(raw_frame: str) -> bool:
        """
        Verifies either a 2-char hex BCC or a 3-digit decimal BCC (FUN_004c57d0)
        at the end of a Topcon frame.
        """
        stripped = raw_frame.strip().lstrip("\x00\x06\x15\x02").rstrip("\r\n\x03")
        if len(stripped) < 5 or stripped.endswith("**"):
            return True
        if len(stripped) >= 4 and stripped[-3:].isdigit():
            body3 = stripped[:-3]
            if compute_topcon_bcc_decimal3(body3) == stripped[-3:]:
                return True
        body2 = stripped[:-2]
        if compute_topcon_bcc(body2).upper() == stripped[-2:].upper():
            return True
        return False

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        if self.config.verify_bcc and not self.verify_frame_bcc(response):
            raise ValueError("ERROR: BCC mismatch.")

        clean = response.strip().lstrip("\x00\x06\x15\x02?").rstrip("\r\n\x03*")
        if not clean:
            raise ValueError("ERROR: Shot could not be recorded. Verify that prism is visible.")

        packed_pattern = re.compile(
            r"([+-]?\d+)([mfMF])([+-]?\d+)[dD]?([+-]\d+)([dD]?)"
        )
        match = packed_pattern.search(clean)
        if match:
            sd_raw, unit_char, v_raw, hz_raw, angle_unit = match.groups()
            if angle_unit and angle_unit.lower() != "d":
                raise ValueError("ERORR: Angles not in degrees. Reset instrument.")
            digits_only = sd_raw.lstrip("+-")
            divisor = 10000.0 if len(digits_only) >= 8 else 1000.0
            sd_val = float(sd_raw) / divisor
            if unit_char.lower() == "f":
                sd_val *= 0.3048
            v_rad = dms_encoded_to_radians(float(v_raw))
            hz_rad = dms_encoded_to_radians(float(hz_raw)) % (2.0 * math.pi)

            if self.config.check_barrel_reversal:
                v_deg = math.degrees(v_rad)
                if not (0.0 <= v_deg <= 180.0):
                    raise ValueError(
                        "ERROR: The horizontal plane (of the vertical angle) is not set to 90, "
                        "or the barrel of the theodolite has been reversed"
                    )

            return RawObservation(
                slope_distance=sd_val,
                horizontal_angle_rad=hz_rad,
                vertical_angle_rad=v_rad,
                raw_tx=tx_cmd,
                raw_rx=response,
            )

        parts = [p.strip() for p in clean.split(",") if p.strip()]
        if len(parts) >= 3:
            sd_val = float(parts[0])
            v_rad = math.radians(float(parts[1]))
            hz_rad = math.radians(float(parts[2])) % (2.0 * math.pi)
            return RawObservation(
                slope_distance=sd_val,
                horizontal_angle_rad=hz_rad,
                vertical_angle_rad=v_rad,
                raw_tx=tx_cmd,
                raw_rx=response,
            )

        raise ValueError(f"Unrecognized Topcon frame format: {response!r}")


class SokkiaNikonDriver(TotalStationDriver):
    """
    Driver for Sokkia SET 4B / CX / SX / iX and Nikon DTM / NPL / Nivo Total Stations.
    Triggers measurement with DC1 (0x11) or '$DNX' and aborts with DC2 (0x12).
    """

    TRIGGER_CMD = "\x11\r\n"
    ABORT_CMD = "\x12\r\n"

    def __init__(
        self,
        config: SerialConfig,
        log_callback: Optional[Callable[[str, str], None]] = None,
        variant: str = "NIKON",
    ) -> None:
        super().__init__(config, log_callback)
        self.variant = variant.strip().upper()

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        digits8 = format_dddmmss_8digit(angle_dddmmss)
        if self.variant == "SOKKIA":
            return f"{digits8}\r\n"
        return f"!HAN{digits8}\r\n"

    def measure(self) -> RawObservation:
        rx = self.send_raw(self.TRIGGER_CMD)
        return self.parse_response(rx, tx_cmd="\\x11\\r\\n")

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        lines = [ln.strip() for ln in response.strip().splitlines() if ln.strip()]
        if not lines:
            raise ValueError("Empty Sokkia/Nikon response")

        for line in reversed(lines):
            if line.startswith("E "):
                continue
            tokens = line.replace(",", " ").split()
            if len(tokens) >= 3:
                sd_mm = float(tokens[0])
                v_dms = float(tokens[1])
                hz_dms = float(tokens[2])
                slope_dist = sd_mm / 1000.0 if sd_mm >= 100.0 else sd_mm
                v_rad = dms_encoded_to_radians(v_dms)
                hz_rad = dms_encoded_to_radians(hz_dms) % (2.0 * math.pi)
                return RawObservation(
                    slope_distance=slope_dist,
                    horizontal_angle_rad=hz_rad,
                    vertical_angle_rad=v_rad,
                    raw_tx=tx_cmd,
                    raw_rx=response,
                )

        raise ValueError(f"Could not parse Sokkia/Nikon response: {response!r}")


class TrimbleM3Driver(TotalStationDriver):
    """
    Driver for Trimble M3, Geodimeter 400/500/600, Trimble 5600, and S-Series Total Stations
    using Geodimeter Data System (GDS) label-value ASCII protocol:
      - Trigger measurement: 'RG=1\\r\\n'
      - Set Horizontal Angle: '21=<DDD.MMSS>\\r\\n'
      - Response labels:
        * '7=<DDD.MMSS>' or '21=<DDD.MMSS>' or '37=<DDD.MMSS>' (Horizontal Angle)
        * '8=<DDD.MMSS>' or '22=<DDD.MMSS>' or '38=<DDD.MMSS>' (Vertical Zenith Angle)
        * '9=<meters>'   or '31=<meters>'   or '39=<meters>'   (Slope Distance)
    """

    TRIGGER_CMD = "RG=1\r\n"

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        return f"21={angle_dddmmss:.4f}\r\n"

    def measure(self) -> RawObservation:
        rx = self.send_raw(self.TRIGGER_CMD)
        return self.parse_response(rx, tx_cmd="RG=1")

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        hz_rad: Optional[float] = None
        v_rad: Optional[float] = None
        sd_val: Optional[float] = None

        for token in re.split(r"[\r\n,;]+", response.strip()):
            tok = token.strip()
            if "=" not in tok:
                continue
            label, val_s = [p.strip() for p in tok.split("=", 1)]
            if not val_s:
                continue
            val = float(val_s)
            if label in ("7", "21", "37", "HA", "HZ"):
                hz_rad = dms_encoded_to_radians(val) % (2.0 * math.pi)
            elif label in ("8", "22", "38", "VA", "V"):
                v_rad = dms_encoded_to_radians(val)
            elif label in ("9", "31", "39", "SD"):
                sd_val = val

        if hz_rad is None or v_rad is None or sd_val is None:
            raise ValueError(f"Incomplete Trimble/Geodimeter GDS frame: {response!r}")

        return RawObservation(
            slope_distance=sd_val,
            horizontal_angle_rad=hz_rad,
            vertical_angle_rad=v_rad,
            raw_tx=tx_cmd,
            raw_rx=response,
        )


class ZeissRec500Driver(TotalStationDriver):
    """
    Driver for Carl Zeiss Elta (R45/R50/R55/C) & REC-500 Total Stations:
      - Trigger measurement: 'M\\r\\n'
      - Parses Zeiss REC-500 lines such as '0001  SD  12.3450  Hz  90.1530  V  91.0000'
    """

    TRIGGER_CMD = "M\r\n"

    def format_set_horizontal_angle_cmd(self, angle_dddmmss: float) -> str:
        return f"HZ={angle_dddmmss:.4f}\r\n"

    def measure(self) -> RawObservation:
        rx = self.send_raw(self.TRIGGER_CMD)
        return self.parse_response(rx, tx_cmd="M")

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        clean = response.strip()
        m_sd = re.search(r"SD\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", clean, re.IGNORECASE)
        m_hz = re.search(r"Hz\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", clean, re.IGNORECASE)
        m_v = re.search(r"\bV\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", clean, re.IGNORECASE)
        if not (m_sd and m_hz and m_v):
            raise ValueError(f"Unrecognized Zeiss REC-500 frame: {response!r}")

        sd_val = float(m_sd.group(1))
        hz_rad = dms_encoded_to_radians(float(m_hz.group(1))) % (2.0 * math.pi)
        v_rad = dms_encoded_to_radians(float(m_v.group(1)))
        return RawObservation(
            slope_distance=sd_val,
            horizontal_angle_rad=hz_rad,
            vertical_angle_rad=v_rad,
            raw_tx=tx_cmd,
            raw_rx=response,
        )


class MicroscribeDriver(TotalStationDriver):
    """
    Driver for Microscribe / Pentograph 3D Digitizer (MICROSCRIBE / PENTOGRAPH in edmwin.exe).
    Replicates FUN_004cdb10 ('GETPOS') and FUN_00556b20 (frmGetMSData clipboard mm -> m parser).
    """

    TRIGGER_CMD = "GETPOS\r\n"

    def measure(self) -> RawObservation:
        rx = self.send_raw(self.TRIGGER_CMD)
        return self.parse_response(rx, tx_cmd="GETPOS")

    @staticmethod
    def parse_clipboard_mm(
        clipboard_text: str,
        origin_x: float = 0.0,
        origin_y: float = 0.0,
        origin_z: float = 0.0,
    ) -> Tuple[float, float, float]:
        clean = clipboard_text.strip()
        if not clean:
            raise ValueError("Copy data from notepad")
        allowed = set("0123456789,.- \r\n\t+")
        if any(ch not in allowed for ch in clean):
            raise ValueError("Invalid microscribe data")
        parts = [p.strip() for p in clean.splitlines()[0].split(",")]
        if len(parts) < 3:
            raise ValueError("Invalid microscribe data")
        x_m = float(parts[0]) / 1000.0 - origin_x
        y_m = float(parts[1]) / 1000.0 - origin_y
        z_m = float(parts[2]) / 1000.0 - origin_z
        return (x_m, y_m, z_m)

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        x_m, y_m, z_m = self.parse_clipboard_mm(response)
        sd = math.sqrt(x_m * x_m + y_m * y_m + z_m * z_m)
        hz = math.atan2(x_m, y_m) % (2.0 * math.pi) if sd > 0 else 0.0
        v = math.acos(max(-1.0, min(1.0, z_m / sd))) if sd > 0 else (math.pi / 2.0)
        return RawObservation(
            slope_distance=sd,
            horizontal_angle_rad=hz,
            vertical_angle_rad=v,
            direct_xyz=(x_m, y_m, z_m),
            raw_tx=tx_cmd,
            raw_rx=response,
        )


class SimulatorDriver(TotalStationDriver):
    """
    Synthetic Total Station Simulator for offline testing, training, and UI verification.
    """

    def __init__(
        self,
        config: Optional[SerialConfig] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
        seed: Optional[int] = 42,
    ) -> None:
        super().__init__(config or SerialConfig(port="SIMULATOR"), log_callback)
        self._rng = random.Random(seed)
        self._queued_observations: List[RawObservation] = []
        self._shot_counter = 0
        self.last_set_h_angle_dddmmss: float = 0.0

    @property
    def is_connected(self) -> bool:
        return True

    def connect(self) -> None:
        self._log("INFO", "Simulator connected.")

    def disconnect(self) -> None:
        self._log("INFO", "Simulator disconnected.")

    def set_horizontal_angle(self, angle_dddmmss: float) -> str:
        self.last_set_h_angle_dddmmss = angle_dddmmss
        rad = dms_encoded_to_radians(angle_dddmmss)
        self._log("Sent->", f"%R1Q,2113:{rad:.7f} (SIMULATED)")
        return "%R1P,0,0:0\r\n"

    def queue_observation(
        self,
        slope_distance: float,
        hz_deg: float,
        v_deg: float,
    ) -> None:
        self._queued_observations.append(
            RawObservation(
                slope_distance=slope_distance,
                horizontal_angle_rad=math.radians(hz_deg) % (2.0 * math.pi),
                vertical_angle_rad=math.radians(v_deg),
                raw_tx="SIM_TRIG",
                raw_rx=f"SIM_RX: SD={slope_distance:.4f}m Hz={hz_deg:.4f}d V={v_deg:.4f}d",
            )
        )

    def measure(self) -> RawObservation:
        self._shot_counter += 1
        if self._queued_observations:
            obs = self._queued_observations.pop(0)
            self._log("Sent->", obs.raw_tx)
            self._log("Received->", obs.raw_rx)
            return obs

        sd = round(self._rng.uniform(1.10, 1.75), 4)
        hz_deg = round(self._rng.uniform(35.0, 55.0), 4)
        v_deg = round(self._rng.uniform(91.0, 96.0), 4)
        rx_str = f"%R1P,0,0:0,{math.radians(hz_deg):.6f},{math.radians(v_deg):.6f},{sd:.4f}"
        self._log("Sent->", "%R1Q,2108:10000,1 (SIMULATED)")
        self._log("Received->", rx_str)
        return RawObservation(
            slope_distance=sd,
            horizontal_angle_rad=math.radians(hz_deg),
            vertical_angle_rad=math.radians(v_deg),
            raw_tx="%R1Q,2108:10000,1",
            raw_rx=rx_str,
        )

    def parse_response(self, response: str, tx_cmd: str = "") -> RawObservation:
        return LeicaGeoCOMDriver(self.config).parse_response(response, tx_cmd)


def create_driver(
    station_type: str,
    config: SerialConfig,
    log_callback: Optional[Callable[[str, str], None]] = None,
) -> TotalStationDriver:
    normalized = station_type.strip().lower()
    if "autotilt" in normalized:
        return LeicaAutoTiltDriver(config, log_callback)
    if "builder" in normalized:
        return LeicaBuilderDriver(config, log_callback)
    if "wild2" in normalized or "geocom" in normalized:
        return LeicaGeoCOMDriver(config, log_callback)
    if "gsi" in normalized or "wild" in normalized or "leica" in normalized:
        return LeicaGSIDriver(config, log_callback)
    if "topcon" in normalized or "gts" in normalized:
        return TopconDriver(config, log_callback)
    if "sokkia" in normalized:
        return SokkiaNikonDriver(config, log_callback, variant="SOKKIA")
    if "nikon" in normalized or "set" in normalized:
        return SokkiaNikonDriver(config, log_callback, variant="NIKON")
    if "trimble" in normalized or "geodimeter" in normalized:
        return TrimbleM3Driver(config, log_callback)
    if "zeiss" in normalized or "rec500" in normalized or "elta" in normalized:
        return ZeissRec500Driver(config, log_callback)
    if "microscribe" in normalized or "pentograph" in normalized:
        return MicroscribeDriver(config, log_callback)
    return SimulatorDriver(config, log_callback)
