#!/usr/bin/env python3
"""
Set expirationTime on Google Drive permissions for a folder and every item inside it.

Targets **reader** (view) and **writer** (edit) roles only; skips owner and other roles.

OAuth: Drive scope always; Spreadsheets scope only when logging to a Sheet (--spreadsheet-id).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

LOG_HEADERS = [
    "run_utc",
    "root_folder_id",
    "file_id",
    "permission_id",
    "grantee",
    "grantee_type",
    "role",
    "previous_expiration",
    "new_expiration",
    "status",
    "error_detail",
]

DEFAULT_FOLDER_ID = "1FmlgqUQwqYFzOOdXYaJZvBWYDP7sD1Ve"
FOLDER_MIME = "application/vnd.google-apps.folder"

CREDENTIALS_DIR = os.path.join(os.path.dirname(__file__), "credentials")
CLIENT_SECRET_FILE = os.path.join(CREDENTIALS_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token.json")

ROLES_TO_EXPIRE = frozenset({"reader", "writer"})


def _oauth_client_config_from_env() -> dict | None:
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not cid or not csec:
        return None
    return {
        "installed": {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _required_scopes(with_sheets_log: bool) -> list[str]:
    scopes = [DRIVE_SCOPE]
    if with_sheets_log:
        scopes.append(SHEETS_SCOPE)
    return scopes


def _needs_sheets_reauth(creds: Credentials, with_sheets_log: bool) -> bool:
    if not with_sheets_log:
        return False
    have = set(creds.scopes or [])
    return SHEETS_SCOPE not in have


def get_credentials(with_sheets_log: bool) -> Credentials:
    scopes = _required_scopes(with_sheets_log)
    creds: Credentials | None = None
    if os.path.isfile(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, scopes)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if creds and _needs_sheets_reauth(creds, with_sheets_log):
        creds = None

    if not creds or not creds.valid:
        client_config = _oauth_client_config_from_env()
        if client_config:
            flow = InstalledAppFlow.from_client_config(client_config, scopes)
        elif os.path.isfile(CLIENT_SECRET_FILE):
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, scopes)
        else:
            print(
                "Missing OAuth config. Add credentials/client_secret.json or set "
                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.",
                file=sys.stderr,
            )
            sys.exit(1)
        creds = flow.run_local_server(port=0)
        os.makedirs(CREDENTIALS_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return creds


def rfc3339_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sheet_cell(value: object) -> str:
    """Avoid Sheets treating values as formulas."""
    s = "" if value is None else str(value)
    if s and s[0] in "=+-@":
        return "'" + s
    return s


def sheet_ensure_headers(sheets, spreadsheet_id: str, sheet_tab: str) -> None:
    rng = f"{sheet_tab}!A1:A1"
    existing = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
    )
    if existing.get("values"):
        return
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [LOG_HEADERS]},
    ).execute()


def sheet_append_rows(
    sheets, spreadsheet_id: str, sheet_tab: str, rows: list[list[object]]
) -> None:
    if not rows:
        return
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        chunk = [[_sheet_cell(v) for v in row] for row in rows[i : i + chunk_size]]
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_tab}!A:K",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": chunk},
        ).execute()


def iter_folder_tree_file_ids(service, root_folder_id: str):
    """Yield the root folder id, then every descendant file and subfolder id."""
    yield root_folder_id
    stack = [root_folder_id]
    while stack:
        parent_id = stack.pop()
        page_token = None
        while True:
            try:
                resp = (
                    service.files()
                    .list(
                        q=f"'{parent_id}' in parents and trashed=false",
                        spaces="drive",
                        fields="nextPageToken, files(id, mimeType, name)",
                        pageSize=1000,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as e:
                print(f"WARN list children of {parent_id}: {e}", file=sys.stderr)
                break
            for f in resp.get("files") or []:
                fid = f["id"]
                yield fid
                if f.get("mimeType") == FOLDER_MIME:
                    stack.append(fid)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break


def list_permissions_for_file(service, file_id: str) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.permissions()
            .list(
                fileId=file_id,
                fields="nextPageToken, permissions(id,emailAddress,domain,type,role,expirationTime)",
                supportsAllDrives=True,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(resp.get("permissions") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def process_file_permissions(
    service,
    file_id: str,
    expire_str: str,
    *,
    root_folder_id: str,
    run_utc: str,
    include_link: bool,
    dry_run: bool,
    log_rows: list[list[object]] | None,
) -> tuple[int, int, int]:
    """Returns (updated_count, skipped_count, permissions_listed)."""
    permission_items = list_permissions_for_file(service, file_id)
    updated = 0
    skipped = 0

    for perm in permission_items:
        pid = perm.get("id")
        role = (perm.get("role") or "").lower()
        ptype = perm.get("type")

        if role == "owner":
            skipped += 1
            continue
        if role not in ROLES_TO_EXPIRE:
            skipped += 1
            continue

        if ptype in ("anyone", "domain") and not include_link:
            skipped += 1
            continue

        label = f"{file_id[:8]}… {ptype}:{perm.get('emailAddress') or perm.get('domain') or pid}"

        current_exp = perm.get("expirationTime") or ""
        grantee = perm.get("emailAddress") or perm.get("domain") or ""
        if ptype == "anyone" and not grantee:
            grantee = "anyoneWithLink"

        def append_log(status: str, error_detail: str = "") -> None:
            if log_rows is None or dry_run:
                return
            log_rows.append(
                [
                    run_utc,
                    root_folder_id,
                    file_id,
                    pid or "",
                    grantee,
                    ptype or "",
                    role,
                    current_exp,
                    expire_str,
                    status,
                    error_detail[:500] if error_detail else "",
                ]
            )

        if dry_run:
            print(f"[dry-run] {label} ({role}) -> {expire_str} (was {current_exp})")
            updated += 1
            continue

        try:
            service.permissions().update(
                fileId=file_id,
                permissionId=pid,
                body={"expirationTime": expire_str},
                supportsAllDrives=True,
                removeExpiration=False,
            ).execute()
            print(f"OK {label} ({role}) -> expires {expire_str}")
            updated += 1
            append_log("ok")
        except HttpError as e:
            err = str(e)
            print(f"FAIL {label} ({role}): {e}", file=sys.stderr)
            append_log("fail", err)

    return updated, skipped, len(permission_items)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expire reader and writer permissions on a folder and all items inside it."
    )
    parser.add_argument(
        "--folder-id",
        default=os.environ.get("FOLDER_ID", DEFAULT_FOLDER_ID),
        help="Drive folder ID (default: env FOLDER_ID or built-in default)",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=float(os.environ.get("EXPIRY_HOURS", "24")),
        help="Hours until permission expiration (default: 24 or EXPIRY_HOURS)",
    )
    parser.add_argument(
        "--include-link",
        action="store_true",
        help="Also set expiration on type=anyone / domain (link-style) permissions",
    )
    parser.add_argument(
        "--root-only",
        action="store_true",
        help="Only process the folder itself, not files/subfolders inside it",
    )
    parser.add_argument(
        "--throttle-seconds",
        type=float,
        default=0.0,
        help="Sleep this many seconds after each file's permission updates (quota safety)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned updates only; no API writes")
    parser.add_argument(
        "--spreadsheet-id",
        default=(os.environ.get("SPREADSHEET_ID") or "").strip() or None,
        help="Google Sheet ID (from the URL); enables audit log tab. Or set SPREADSHEET_ID.",
    )
    parser.add_argument(
        "--sheet-tab",
        default=os.environ.get("SHEET_TAB", "Sheet1"),
        help="Worksheet tab name (default Sheet1 or SHEET_TAB)",
    )
    args = parser.parse_args()

    if args.hours <= 0:
        print("--hours must be positive", file=sys.stderr)
        sys.exit(1)

    expire_at = datetime.now(timezone.utc) + timedelta(hours=args.hours)
    expire_str = rfc3339_utc(expire_at)
    run_utc = rfc3339_utc(datetime.now(timezone.utc))

    use_sheet = bool(args.spreadsheet_id) and not args.dry_run
    creds = get_credentials(with_sheets_log=use_sheet)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets_svc = None
    if use_sheet:
        sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    if args.root_only:
        file_ids = [args.folder_id]
    else:
        file_ids = list(iter_folder_tree_file_ids(service, args.folder_id))

    total_updated = 0
    total_skipped = 0
    total_perms = 0
    log_rows: list[list[object]] | None = [] if use_sheet else None

    print(f"Processing {len(file_ids)} item(s); roles={sorted(ROLES_TO_EXPIRE)}; expires {expire_str}", flush=True)
    if use_sheet:
        print(f"Sheet log: {args.sheet_tab!r} in spreadsheet {args.spreadsheet_id}", flush=True)

    for i, fid in enumerate(file_ids, start=1):
        if len(file_ids) > 1 and i % 50 == 1:
            print(f"… item {i}/{len(file_ids)}", flush=True)
        u, s, n = process_file_permissions(
            service,
            fid,
            expire_str,
            root_folder_id=args.folder_id,
            run_utc=run_utc,
            include_link=args.include_link,
            dry_run=args.dry_run,
            log_rows=log_rows,
        )
        total_updated += u
        total_skipped += s
        total_perms += n
        if args.throttle_seconds > 0:
            time.sleep(args.throttle_seconds)

    if use_sheet and sheets_svc and log_rows is not None:
        try:
            sheet_ensure_headers(sheets_svc, args.spreadsheet_id, args.sheet_tab)
            sheet_append_rows(sheets_svc, args.spreadsheet_id, args.sheet_tab, log_rows)
            print(
                f"Appended {len(log_rows)} row(s) to "
                f"https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}/edit",
                flush=True,
            )
        except HttpError as e:
            print(
                f"Sheet logging failed ({e}). Enable Google Sheets API for your Cloud project, "
                f"share the spreadsheet with the same Google account as Drive, and re-run.",
                file=sys.stderr,
            )

    print(
        f"Done. Items: {len(file_ids)}; permission rows seen: {total_perms}; "
        f"updates: {total_updated}; skipped role/type: {total_skipped}."
    )


if __name__ == "__main__":
    main()
