# Modern EDM — Total Station Excavation & Archaeological Survey Recorder

**Modern EDM** is a cross-platform (Windows & macOS) Python/PySide6 replacement for the legacy Visual Basic 6 **EDM Windows (`edmwin.exe` v3.2 / v3.05.0010)** total-station surveying program originally developed by Harold L. Dibble and Shannon P. McPherron (`OldStoneAge.com`).

Built from a complete Ghidra decompilation and subsystem audit of `edmwin.exe`, Modern EDM preserves 100% wire-protocol, mathematical, UI dialog, and `.CFG` workflow parity with legacy archaeological excavations while replacing the 32-bit Microsoft Jet/DAO `.mdb` engine with crash-safe atomic SQLite (WAL) storage, live 2D Plan/Section plotting, and QGIS GeoJSON export.

---

## Key Features

### 1. Hardware Total Station & 3D Digitizer Drivers (`drivers/total_station.py`)
Supports all 9 instrument modes from `edmwin.exe`:
- **Leica GeoCOM (`WILD2`):** Standard ASCII RPC (`%R1Q,2008:1,1` trigger, `%R1Q,2108:10000,1` read, `%R1Q,2113:<rad>` horizontal angle preset, `%R1Q,5008:` date/time, `%R1Q,5004,6,21341:` instrument name).
- **Leica Motorized AutoTilt (`LEICA_AUTOTILT`):** Full 11-step GeoCOM target search, 360° prism lock, tilt-compensated measurement (`%R1Q,2187:10000,1`), and lock release.
- **Leica / Wild GSI-8 & GSI-16 (`WILD` / `LEICA`):** `SET/41/0`, `SET/149/2`, `GET/M/WI11/WI21/WI22/WI31/WI51`, and `PUT/21...4+` angle preset across gons, decimal degrees, DMS, mm, 0.1 mm, and millifeet.
- **Leica Builder 209 (`BUILDER`):** `$MSR` trigger and `/Dc ` horizontal angle preset.
- **Topcon (`TOPCON`):** GTS-3, GTS-210, GTS-220, GTS-301/302/303, GTS-320, and GM-55 (`\x06R\x03` / `\x15?\x03`, `!HAN` angle preset, 2-char hex & 3-digit decimal BCC checksum verification, and barrel-reversal checks).
- **Sokkia (`SOKKIA`) & Nikon (`NIKON`):** `DC1` (`\x11`) trigger, `DC2` (`\x12`) abort, Sokkia raw 8-digit angle preset, and Nikon `!HAN` preset.
- **Microscribe / Pentograph (`MICROSCRIBE` / `PENTOGRAPH`):** `GETPOS` serial polling and MUS clipboard `X,Y,Z` millimeter-to-meter block origin reduction.
- **Offline Simulator (`SIMULATE`):** Built-in deterministic synthetic total station for testing and training without hardware connected.

### 2. Geodetic & Surveying Math Engine (`survey/math_engine.py`)
- **Polar-to-Cartesian Reduction:** Zenith vertical angle, clockwise azimuth + station orientation offset, prism constant offset (`[-0.05, +0.05]` m) and pole height (`[-10.0, +10.0]` m, supporting negative heights for cave ceilings).
- **4 Station Initialization Modes:**
  1. **Direct / Set Angle Only** (with `Flip 180 Degrees` helper).
  2. **1-Point Setup** (unknown occupation shooting 1 known datum).
  3. **2-Point Backsight** (known occupation shooting a reference datum; automatically sets hardware horizontal circle and reports expected vs. recorded distance residual).
  4. **N-Point Free-Station Resection** (2+ known datums with least-squares refinement and 2D/3D RMS residual reporting).
- **Station Verify (`frmStationVerify`):** Shoots a known control nail and reports `(dX, dY, dZ)`, 2D/3D error, and horizontal distance residual.
- **6-Way Post-Shot Coordinate Offset (`frmOffSet`):** `Offset East`, `West`, `North`, `South`, `Up`, and `Down` with 1 km sanity guard and cross-unit boundary detection.

### 3. Complete `edmwin.exe` Form & Dialog Parity (`ui/dialogs.py`, `ui/main_window.py`)

| Legacy VB6 Form | Modern PySide6 Class | Capabilities |
| :--- | :--- | :--- |
| `frmRecord` | `EDMMainWindow` | Dynamic `.CFG` form, `[BUTTON1]`–`[BUTTON6]` speedbutton bar, live 2D Plan/Section plot canvas (`XY`/`XZ`/`YZ`), and points table. |
| `frmStationSetup` / `frmStationVerify` | `StationSetupDialog` | 5-tab wizard for Direct, 1-Point, 2-Point Backsight, N-Point Resection, and Station Verify. |
| `frmEditField` | `CFGFieldEditorDialog` | Visual `.CFG` field & menu editor (up to 30 fields, Carry/Increment/UnitField flags, and Quick-Add Angles/Date/Time/Datum Info buttons). |
| `frmButtons` | `SpeedButtonEditorDialog` | Visual 6-slot macro editor with Caption, `Ctrl+<Key>` shortcut conflict check, Alpha/Numeric ID toggle, and multi-field presets. |
| `frmDataGrid` | `DataGridDialog` | Full spreadsheet view with column-visibility checklist, inline cell editing, and series-aware row deletion. |
| `frmFilter` | `FieldFilterDialog` | Distinct field/value browser (`SELECT DISTINCT`) and Current-Station filter. |
| `frmSubUnits` | `ContextDefaultsDialog` | Master/Dependent variable default linking (`EDM_Defaults`) with live auto-fill and post-shot rule learning. |
| `frmManualshot` | `ManualShotDialog` | Hand-entered `ddd.mmss` angles + slope distance + pole height or direct `(X, Y, Z)` coordinates. |
| `frmDupID` | `DuplicateIDDialog` | 3-way duplicate ID resolution (**Convert to alpha ID** `42a`, **New ID**, or **Continuation `+Shot`**). |
| `frmOffSet` | `OffsetPointDialog` | 6-directional post-shot coordinate offset (`East`, `West`, `North`, `South`, `Up`, `Down`) with 1 km guard. |
| `frmDatumSheet` / `frmPolesheet` / `AddUnits` | `EntityManagerDialog` | CRUD managers for Control Datums, Prisms (`[-10, 10]` m height, `[-0.05, 0.05]` m offset), and Excavation Units (`RECT`, `CIRCLE`, `NOLIMITS`). |
| `frmTheodolite` | `SerialTerminalDialog` | Total station protocol selector, COM/Bluetooth port scanner, and raw ASCII serial diagnostic terminal. |
| `frmDebug` | `DebugStationDialog` | Hardware Initialize, X-Shot coordinate test, and 5 GeoCOM preset commands (`0 Horizontal Angle`, `Get date/time`, `Get Instr. Name`, `Launch measurement`, `Get meas. with tilt`). |
| `frmSetupLogFiles` | `SetupLogFilesDialog` | Timestamped Total Station serial log (`TSLogFile`) and General setup log (`GeneralLogFile`) configuration and clearing. |
| `frmDefault` | `CreateDefaultProjectDialog` | Paleolithic default `.CFG` and database generator (`Main` datum, `Zero`/`10cm` prisms, `Unit1`/`Unit2` units). |
| `modPrint` | `LabelPrinterDialog` | Live preview and multi-copy reprint spooler for Plain-Text ASCII and Zebra ZPL II (Code-39 barcode) artifact bag labels. |
| `frmStatus` / `frmHelp` / `frmAbout` | `EDMStatusDialog` / `HelpViewerDialog` / `AboutDialog` | Full optical-center status summary, searchable 8-topic offline field manual (`[F1]`), and credits. |

---

## Quick Start (Development)

```bash
pip install -r requirements.txt
python main.py
```

### Run the Automated Parity Test Suite
```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## Building Standalone Executables (Windows & macOS)

### Windows (`dist\ModernEDM.exe`)
```powershell
python build_release.py
```

### macOS (`dist/ModernEDM.app` & `dist/ModernEDM-macOS.dmg`)
```bash
chmod +x build_macos.sh
./build_macos.sh
```

---

## License
MIT License — see [LICENSE](LICENSE) for details.
