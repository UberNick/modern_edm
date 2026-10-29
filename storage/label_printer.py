"""
Recreates the legacy EDMWin Artifact Bag Label & Barcode Printer subsystem (modPrint.bas).
Generates human-readable ASCII labels and Zebra ZPL / Code-39 barcode payloads
for thermal field printers or local log files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def format_ascii_bag_label(point_record: Dict[str, Any], site_name: str = "EXCAVATION") -> str:
    """Formats a standard 5-line ASCII field artifact bag label."""
    unit = point_record.get("unit", "")
    pt_id = point_record.get("id", "")
    suffix = point_record.get("suffix", 0)
    x = float(point_record.get("x", 0.0))
    y = float(point_record.get("y", 0.0))
    z = float(point_record.get("z", 0.0))
    extra = point_record.get("extra_fields", {})
    code = extra.get("CODE", "")
    level = extra.get("LEVEL", "")
    excavator = extra.get("EXCAVATOR", "")
    ts = point_record.get("timestamp", "")

    barcode_str = f"*{unit}-{pt_id}-{suffix}*"
    return (
        f"========================================\n"
        f"SITE: {site_name}   DATE: {ts}\n"
        f"UNIT: {unit:<10} ID: {pt_id}.{suffix}\n"
        f"LVL : {level:<10} CODE: {code:<10} EXC: {excavator}\n"
        f"XYZ : {x:.3f}, {y:.3f}, {z:.3f}\n"
        f"BARCODE (Code39): {barcode_str}\n"
        f"========================================\n"
    )


def format_zpl_barcode_label(point_record: Dict[str, Any], site_name: str = "EXCAVATION") -> str:
    """Generates a Zebra Programming Language (ZPL II) label with Code-39 barcode."""
    unit = point_record.get("unit", "")
    pt_id = point_record.get("id", "")
    suffix = point_record.get("suffix", 0)
    x = float(point_record.get("x", 0.0))
    y = float(point_record.get("y", 0.0))
    z = float(point_record.get("z", 0.0))
    extra = point_record.get("extra_fields", {})
    code = extra.get("CODE", "")
    level = extra.get("LEVEL", "")
    barcode_payload = f"{unit}-{pt_id}-{suffix}"

    return (
        "^XA\n"
        f"^FO30,25^ADN,24,12^FD{site_name} - UNIT: {unit}  ID: {pt_id}.{suffix}^FS\n"
        f"^FO30,60^ADN,18,10^FDLEVEL: {level}   CODE: {code}^FS\n"
        f"^FO30,90^ADN,18,10^FDX:{x:.3f}  Y:{y:.3f}  Z:{z:.3f}^FS\n"
        f"^FO30,125^BY2^B3N,N,60,Y,N^FD{barcode_payload}^FS\n"
        "^XZ\n"
    )


def format_zpl_bag_label(point_record: Dict[str, Any], site_name: str = "EXCAVATION") -> str:
    """Alias for format_zpl_barcode_label."""
    return format_zpl_barcode_label(point_record, site_name)


def append_label_to_spool(
    point_record: Dict[str, Any],
    spool_file: str | Path,
    site_name: str = "EXCAVATION",
    use_zpl: bool = False,
) -> str:
    payload = (
        format_zpl_barcode_label(point_record, site_name)
        if use_zpl
        else format_ascii_bag_label(point_record, site_name)
    )
    p = Path(spool_file)
    with p.open("a", encoding="utf-8") as f:
        f.write(payload)
    return payload
