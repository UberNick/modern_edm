"""
Cloud Storage (Dropbox & Google Drive) and Email / Gmail Attachment Sharing for Modern EDM.
Built entirely on the Python standard library (urllib.request, smtplib, email.message, ssl, zipfile)
so PyInstaller builds remain lightweight and zero extra dependencies are required:

1. Export Packaging (`build_export_artifact`):
   - 'CSV_BUNDLE': 4-table EDMWin CSV bundle zipped into `<prefix>_edmwin_bundle.zip`
   - 'POINTS_CSV': Single `<prefix>_points.csv`
   - 'GEOJSON': 3D QGIS `<prefix>.geojson` (Points + MultiShot LineStrings)
   - 'SQLITE_BACKUP': Atomic backup of the `.sqlite` project database + active `.cfg` in a `.zip`

2. Dropbox Integration (`DropboxClient`):
   - Direct REST API v2 upload (`https://content.dropboxapi.com/2/files/upload`) and
     download (`https://content.dropboxapi.com/2/files/download`) using an OAuth2 Access Token.
   - Local synced folder auto-detection (`~/Dropbox`).
   - Built-in Offline Simulation mode (`SIMULATE` token) for testing without network credentials.

3. Google Drive Integration (`GoogleDriveClient`):
   - Direct REST API v3 multipart upload (`https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart`)
     and media download (`https://www.googleapis.com/drive/v3/files/<file_id>?alt=media`) using an OAuth2 Bearer Token.
   - Local Google Drive for Desktop synced folder auto-detection (`~/Google Drive` or `~/My Drive`).
   - Built-in Offline Simulation mode (`SIMULATE` token).

4. Email & Gmail Attachment Sharing (`EmailShareClient`):
   - Direct SMTP / Gmail (`smtp.gmail.com:587` STARTTLS or `:465` SSL) with MIME file attachments.
   - Gmail API v1 REST send (`https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send`) via OAuth2 token.
   - `.eml` RFC-5322 draft generator (opens directly in Outlook, Apple Mail, Thunderbird, or browser).
   - Built-in Offline Simulation mode (`SIMULATE` host/password).
"""

from __future__ import annotations

import base64
import email.utils
import json
import mimetypes
import os
import shutil
import smtplib
import ssl
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from storage.database import EDMDatabase


@dataclass
class CloudTransferResult:
    provider: str           # 'DROPBOX', 'GOOGLE_DRIVE', 'EMAIL_SMTP', 'GMAIL_API', 'EML_DRAFT'
    operation: str          # 'EXPORT' or 'IMPORT'
    simulated: bool
    local_path: str
    remote_id_or_path: str
    bytes_transferred: int
    message: str


def build_export_artifact(
    db: EDMDatabase,
    output_dir: str | Path,
    export_format: str = "CSV_BUNDLE",
    prefix: str = "excavation_export",
    cfg_path: Optional[str | Path] = None,
) -> Path:
    """
    Generates a ready-to-upload/attach file artifact from the active EDMDatabase:
    - 'CSV_BUNDLE': Zips the 4-table EDMWin CSV bundle (_points, _datums, _prisms, _units) + optional .CFG
    - 'POINTS_CSV': Single points CSV file
    - 'GEOJSON': QGIS 3D GeoJSON file
    - 'PROJECT_ZIP': Zips a clean copy of the SQLite database + 4 CSVs + GeoJSON + .CFG
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = export_format.strip().upper()
    clean_prefix = prefix.strip() or "excavation_export"

    if fmt == "POINTS_CSV":
        csv_file = out_dir / f"{clean_prefix}_points.csv"
        db.export_points_csv(csv_file)
        return csv_file

    if fmt == "GEOJSON":
        geo_file = out_dir / f"{clean_prefix}.geojson"
        db.export_geojson(geo_file)
        return geo_file

    if fmt == "PROJECT_ZIP":
        zip_path = out_dir / f"{clean_prefix}_full_project.zip"
        bundle = db.export_edmwin_csv_bundle(out_dir / clean_prefix)
        geo_file = out_dir / f"{clean_prefix}.geojson"
        db.export_geojson(geo_file)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p_str in bundle.values():
                p = Path(p_str)
                if p.is_file():
                    zf.write(p, arcname=p.name)
            if geo_file.is_file():
                zf.write(geo_file, arcname=geo_file.name)
            db_file = Path(db.db_path)
            if db_file.is_file():
                zf.write(db_file, arcname=db_file.name)
            if cfg_path and Path(cfg_path).is_file():
                cp = Path(cfg_path)
                zf.write(cp, arcname=cp.name)
        return zip_path

    # Default: 'CSV_BUNDLE' -> 4-table EDMWin CSV bundle zipped for single-file cloud/email attachment
    bundle = db.export_edmwin_csv_bundle(out_dir / clean_prefix)
    zip_path = out_dir / f"{clean_prefix}_edmwin_bundle.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p_str in bundle.values():
            p = Path(p_str)
            if p.is_file():
                zf.write(p, arcname=p.name)
        if cfg_path and Path(cfg_path).is_file():
            cp = Path(cfg_path)
            zf.write(cp, arcname=cp.name)
    return zip_path


def import_artifact_into_db(
    db: EDMDatabase,
    artifact_path: str | Path,
    extract_dir: Optional[str | Path] = None,
) -> Dict[str, int]:
    """
    Imports a downloaded cloud file (.zip bundle, _points.csv, .mdb, or .sqlite) into `db`.
    Returns a summary dictionary of imported counts.
    """
    p = Path(artifact_path)
    if not p.is_file():
        raise FileNotFoundError(f"Artifact file not found: {p}")

    work_dir = Path(extract_dir) if extract_dir else p.parent / "_cloud_import_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    suffix = p.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(p, "r") as zf:
            zf.extractall(work_dir)
        pts_candidates = list(work_dir.glob("*_points.csv"))
        if pts_candidates:
            return db.import_edmwin_csv_bundle(pts_candidates[0])
        csv_candidates = list(work_dir.glob("*.csv"))
        if csv_candidates:
            cnt = db.import_points_csv(csv_candidates[0])
            return {"points": cnt, "datums": 0, "prisms": 0, "units": 0, "unit_states": 0}
        raise ValueError(f"No CSV files found inside archive: {p.name}")

    if suffix in (".mdb", ".accdb"):
        return db.import_edmwin_mdb(p)

    if suffix == ".csv":
        if p.stem.lower().endswith(("_points", "_datums", "_prisms", "_units")):
            return db.import_edmwin_csv_bundle(p)
        cnt = db.import_points_csv(p)
        return {"points": cnt, "datums": 0, "prisms": 0, "units": 0, "unit_states": 0}

    raise ValueError(f"Unsupported import file format: {p.suffix}")


# ==============================================================================
# 1. Dropbox REST API v2 & Local Synced Folder Client
# ==============================================================================

class DropboxClient:
    """
    Supports Dropbox API v2 (`files/upload` and `files/download`) as well as
    local Dropbox folder sync and offline simulation (`access_token='SIMULATE'`).
    """

    UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
    DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"

    def __init__(
        self,
        access_token: str = "",
        urlopen_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.access_token = access_token.strip()
        self._urlopen = urlopen_fn or urllib.request.urlopen

    @staticmethod
    def detect_local_sync_folder() -> Optional[Path]:
        home = Path.home()
        for candidate in (
            home / "Dropbox",
            Path(os.environ.get("USERPROFILE", "")) / "Dropbox",
        ):
            if candidate.is_dir():
                return candidate
        return None

    @property
    def is_simulated(self) -> bool:
        tok = self.access_token.upper()
        return not tok or tok in ("SIMULATE", "SIMULATOR", "TEST", "DEMO")

    def upload_file(
        self,
        local_path: str | Path,
        dropbox_dest_path: str = "/ModernEDM/excavation_export.zip",
    ) -> CloudTransferResult:
        src = Path(local_path)
        payload = src.read_bytes()
        norm_dest = "/" + dropbox_dest_path.strip().lstrip("/")

        if self.is_simulated:
            return CloudTransferResult(
                provider="DROPBOX",
                operation="EXPORT",
                simulated=True,
                local_path=str(src),
                remote_id_or_path=norm_dest,
                bytes_transferred=len(payload),
                message=f"[Simulated Dropbox Upload] Uploaded {src.name} ({len(payload)} bytes) -> {norm_dest}",
            )

        api_arg = json.dumps(
            {"path": norm_dest, "mode": "overwrite", "autorename": False, "mute": False}
        )
        req = urllib.request.Request(
            self.UPLOAD_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": api_arg,
            },
        )
        with self._urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        remote_path = body.get("path_display") or norm_dest
        return CloudTransferResult(
            provider="DROPBOX",
            operation="EXPORT",
            simulated=False,
            local_path=str(src),
            remote_id_or_path=remote_path,
            bytes_transferred=len(payload),
            message=f"Uploaded {src.name} ({len(payload)} bytes) to Dropbox: {remote_path}",
        )

    def download_file(
        self,
        dropbox_source_path: str,
        local_dest_path: str | Path,
        simulated_source_bytes: Optional[bytes] = None,
    ) -> CloudTransferResult:
        dst = Path(local_dest_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        norm_src = "/" + dropbox_source_path.strip().lstrip("/")

        if self.is_simulated:
            data = simulated_source_bytes if simulated_source_bytes is not None else (
                dst.read_bytes() if dst.is_file() else b""
            )
            if simulated_source_bytes is not None:
                dst.write_bytes(data)
            return CloudTransferResult(
                provider="DROPBOX",
                operation="IMPORT",
                simulated=True,
                local_path=str(dst),
                remote_id_or_path=norm_src,
                bytes_transferred=len(data),
                message=f"[Simulated Dropbox Download] Downloaded {norm_src} ({len(data)} bytes) -> {dst}",
            )

        api_arg = json.dumps({"path": norm_src})
        req = urllib.request.Request(
            self.DOWNLOAD_URL,
            data=b"",
            method="POST",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Dropbox-API-Arg": api_arg,
            },
        )
        with self._urlopen(req, timeout=30) as resp:
            data = resp.read()
        dst.write_bytes(data)
        return CloudTransferResult(
            provider="DROPBOX",
            operation="IMPORT",
            simulated=False,
            local_path=str(dst),
            remote_id_or_path=norm_src,
            bytes_transferred=len(data),
            message=f"Downloaded {norm_src} ({len(data)} bytes) from Dropbox to {dst}",
        )


# ==============================================================================
# 2. Google Drive REST API v3 & Local Synced Folder Client
# ==============================================================================

class GoogleDriveClient:
    """
    Supports Google Drive API v3 (`upload/drive/v3/files?uploadType=multipart` and
    `drive/v3/files/<file_id>?alt=media`) as well as Google Drive for Desktop synced folders
    and offline simulation (`access_token='SIMULATE'`).
    """

    UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
    FILES_URL = "https://www.googleapis.com/drive/v3/files"

    def __init__(
        self,
        access_token: str = "",
        urlopen_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.access_token = access_token.strip()
        self._urlopen = urlopen_fn or urllib.request.urlopen

    @staticmethod
    def detect_local_sync_folder() -> Optional[Path]:
        home = Path.home()
        for candidate in (
            home / "Google Drive" / "My Drive",
            home / "Google Drive",
            home / "My Drive",
            Path("G:/My Drive"),
        ):
            if candidate.is_dir():
                return candidate
        return None

    @property
    def is_simulated(self) -> bool:
        tok = self.access_token.upper()
        return not tok or tok in ("SIMULATE", "SIMULATOR", "TEST", "DEMO")

    def upload_file(
        self,
        local_path: str | Path,
        drive_filename: Optional[str] = None,
        folder_id: str = "",
    ) -> CloudTransferResult:
        src = Path(local_path)
        file_bytes = src.read_bytes()
        remote_name = (drive_filename or src.name).strip()

        if self.is_simulated:
            fake_id = f"gdrive_sim_{remote_name}"
            return CloudTransferResult(
                provider="GOOGLE_DRIVE",
                operation="EXPORT",
                simulated=True,
                local_path=str(src),
                remote_id_or_path=fake_id,
                bytes_transferred=len(file_bytes),
                message=f"[Simulated Google Drive Upload] Uploaded {remote_name} ({len(file_bytes)} bytes) -> ID: {fake_id}",
            )

        meta: Dict[str, Any] = {"name": remote_name}
        if folder_id.strip():
            meta["parents"] = [folder_id.strip()]

        boundary = "===============ModernEDMDriveBoundary7MA4YWxkTrZu0gW=="
        mime_type = mimetypes.guess_type(src.name)[0] or "application/octet-stream"

        body_parts = [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("utf-8"),
            json.dumps(meta).encode("utf-8"),
            f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
        multipart_body = b"".join(body_parts)

        req = urllib.request.Request(
            self.UPLOAD_URL,
            data=multipart_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
                "Content-Length": str(len(multipart_body)),
            },
        )
        with self._urlopen(req, timeout=30) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))
        file_id = resp_json.get("id", remote_name)
        return CloudTransferResult(
            provider="GOOGLE_DRIVE",
            operation="EXPORT",
            simulated=False,
            local_path=str(src),
            remote_id_or_path=file_id,
            bytes_transferred=len(file_bytes),
            message=f"Uploaded {remote_name} ({len(file_bytes)} bytes) to Google Drive (File ID: {file_id})",
        )

    def download_file(
        self,
        file_id: str,
        local_dest_path: str | Path,
        simulated_source_bytes: Optional[bytes] = None,
    ) -> CloudTransferResult:
        dst = Path(local_dest_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        fid = file_id.strip()
        if not fid:
            raise ValueError("Please provide a Google Drive File ID to download.")

        if self.is_simulated:
            data = simulated_source_bytes if simulated_source_bytes is not None else (
                dst.read_bytes() if dst.is_file() else b""
            )
            if simulated_source_bytes is not None:
                dst.write_bytes(data)
            return CloudTransferResult(
                provider="GOOGLE_DRIVE",
                operation="IMPORT",
                simulated=True,
                local_path=str(dst),
                remote_id_or_path=fid,
                bytes_transferred=len(data),
                message=f"[Simulated Google Drive Download] Downloaded File ID {fid} ({len(data)} bytes) -> {dst}",
            )

        url = f"{self.FILES_URL}/{fid}?alt=media"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        with self._urlopen(req, timeout=30) as resp:
            data = resp.read()
        dst.write_bytes(data)
        return CloudTransferResult(
            provider="GOOGLE_DRIVE",
            operation="IMPORT",
            simulated=False,
            local_path=str(dst),
            remote_id_or_path=fid,
            bytes_transferred=len(data),
            message=f"Downloaded Google Drive File ID {fid} ({len(data)} bytes) to {dst}",
        )


# ==============================================================================
# 3. Email & Gmail Attachment Sharing (SMTP / Gmail API / .EML Draft)
# ==============================================================================

class EmailShareClient:
    """
    Constructs RFC-5322 MIME emails with attached survey exports (.zip, .csv, .geojson)
    and delivers them via:
    - Gmail / Standard SMTP (`smtp.gmail.com:587` STARTTLS or `:465` SSL)
    - Gmail REST API v1 (`https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send`)
    - Local `.eml` draft file (for one-click opening in desktop mail clients or offline archiving)
    """

    GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    @staticmethod
    def build_mime_message(
        sender_email: str,
        recipient_emails: str | List[str],
        subject: str,
        body_text: str,
        attachment_paths: List[str | Path],
    ) -> EmailMessage:
        if isinstance(recipient_emails, str):
            recipients = [r.strip() for r in recipient_emails.split(",") if r.strip()]
        else:
            recipients = [r.strip() for r in recipient_emails if r.strip()]

        if not recipients:
            raise ValueError("Please enter at least one recipient email address.")

        msg = EmailMessage()
        msg["From"] = sender_email.strip() or "modern-edm@localhost"
        msg["To"] = ", ".join(recipients)
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Subject"] = subject.strip() or "Modern EDM Excavation Survey Export"
        msg.set_content(body_text or "Attached is the exported survey data from Modern EDM.")

        for att in attachment_paths:
            p = Path(att)
            if not p.is_file():
                continue
            ctype, encoding = mimetypes.guess_type(p.name)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            msg.add_attachment(
                p.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=p.name,
            )
        return msg

    @classmethod
    def save_eml_draft(
        cls,
        eml_dest_path: str | Path,
        sender_email: str,
        recipient_emails: str | List[str],
        subject: str,
        body_text: str,
        attachment_paths: List[str | Path],
    ) -> CloudTransferResult:
        msg = cls.build_mime_message(
            sender_email, recipient_emails, subject, body_text, attachment_paths
        )
        dst = Path(eml_dest_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = msg.as_bytes()
        dst.write_bytes(raw_bytes)
        return CloudTransferResult(
            provider="EML_DRAFT",
            operation="EXPORT",
            simulated=False,
            local_path=str(attachment_paths[0]) if attachment_paths else "",
            remote_id_or_path=str(dst),
            bytes_transferred=len(raw_bytes),
            message=f"Saved RFC-5322 email draft with attachment(s) to {dst}",
        )

    @classmethod
    def send_via_smtp(
        cls,
        smtp_host: str,
        smtp_port: int,
        sender_email: str,
        password_or_app_password: str,
        recipient_emails: str | List[str],
        subject: str,
        body_text: str,
        attachment_paths: List[str | Path],
        use_tls: bool = True,
    ) -> CloudTransferResult:
        msg = cls.build_mime_message(
            sender_email, recipient_emails, subject, body_text, attachment_paths
        )
        raw_bytes = msg.as_bytes()
        host_clean = smtp_host.strip()
        pwd_clean = password_or_app_password.strip()

        if (
            not host_clean
            or host_clean.upper() in ("SIMULATE", "SIMULATOR", "TEST")
            or pwd_clean.upper() in ("SIMULATE", "SIMULATOR", "TEST")
        ):
            return CloudTransferResult(
                provider="EMAIL_SMTP",
                operation="EXPORT",
                simulated=True,
                local_path=str(attachment_paths[0]) if attachment_paths else "",
                remote_id_or_path=msg["To"],
                bytes_transferred=len(raw_bytes),
                message=(
                    f"[Simulated SMTP / Gmail Send] To: {msg['To']} | Subject: {msg['Subject']!r} "
                    f"({len(raw_bytes)} bytes MIME with {len(attachment_paths)} attachment(s))"
                ),
            )

        context = ssl.create_default_context()
        if int(smtp_port) == 465:
            with smtplib.SMTP_SSL(host_clean, int(smtp_port), context=context, timeout=30) as server:
                if pwd_clean:
                    server.login(sender_email.strip(), pwd_clean)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host_clean, int(smtp_port), timeout=30) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                if pwd_clean:
                    server.login(sender_email.strip(), pwd_clean)
                server.send_message(msg)

        return CloudTransferResult(
            provider="EMAIL_SMTP",
            operation="EXPORT",
            simulated=False,
            local_path=str(attachment_paths[0]) if attachment_paths else "",
            remote_id_or_path=msg["To"],
            bytes_transferred=len(raw_bytes),
            message=f"Sent email with attachment(s) to {msg['To']} via {host_clean}:{smtp_port}",
        )

    @classmethod
    def send_via_gmail_api(
        cls,
        oauth_access_token: str,
        sender_email: str,
        recipient_emails: str | List[str],
        subject: str,
        body_text: str,
        attachment_paths: List[str | Path],
        urlopen_fn: Optional[Callable[..., Any]] = None,
    ) -> CloudTransferResult:
        msg = cls.build_mime_message(
            sender_email, recipient_emails, subject, body_text, attachment_paths
        )
        raw_bytes = msg.as_bytes()
        tok = oauth_access_token.strip()
        opener = urlopen_fn or urllib.request.urlopen

        if not tok or tok.upper() in ("SIMULATE", "SIMULATOR", "TEST", "DEMO"):
            return CloudTransferResult(
                provider="GMAIL_API",
                operation="EXPORT",
                simulated=True,
                local_path=str(attachment_paths[0]) if attachment_paths else "",
                remote_id_or_path=msg["To"],
                bytes_transferred=len(raw_bytes),
                message=(
                    f"[Simulated Gmail REST API Send] To: {msg['To']} | Subject: {msg['Subject']!r} "
                    f"({len(raw_bytes)} bytes MIME)"
                ),
            )

        b64_raw = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
        payload = json.dumps({"raw": b64_raw}).encode("utf-8")
        req = urllib.request.Request(
            cls.GMAIL_SEND_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
            },
        )
        with opener(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
        msg_id = resp_data.get("id", "sent")
        return CloudTransferResult(
            provider="GMAIL_API",
            operation="EXPORT",
            simulated=False,
            local_path=str(attachment_paths[0]) if attachment_paths else "",
            remote_id_or_path=f"{msg['To']} (Message ID: {msg_id})",
            bytes_transferred=len(raw_bytes),
            message=f"Sent email with attachment(s) via Gmail API to {msg['To']} (ID: {msg_id})",
        )
