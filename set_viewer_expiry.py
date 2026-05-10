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

# Display row written to row 1. Column order matches append_log / sheet_append_rows.
# Internal keys (for reference): run_utc, root_folder_id, root_folder_name, file_id, file_name,
# permission_id, grantee, grantee_type, role, previous_expiration, new_expiration, status, error_detail.
SHEET_HEADER_ROW = [
    "Run (UTC)",
    "Root folder ID",
    "Root folder name",
    "File ID",
    "File name",
    "Permission ID",
    "Grantee",
    "Grantee type",
    "Role",
    "Previous expiration",
    "New expiration",
    "Status",
    "Error detail",
]

SHEET_LOG_NUM_COLUMNS = len(SHEET_HEADER_ROW)


def _sheet_append_range(sheet_tab: str) -> str:
    """Range like 'Tab!A:K' covering all log columns."""
    n = SHEET_LOG_NUM_COLUMNS
    end = ""
    while n:
        n, r = divmod(n - 1, 26)
        end = chr(65 + r) + end
    return f"{sheet_tab}!A:{end}"


DEFAULT_FOLDER_ID = "1FmlgqUQwqYFzOOdXYaJZvBWYDP7sD1Ve"
FOLDER_MIME = "application/vnd.google-apps.folder"

CREDENTIALS_DIR = os.path.join(os.path.dirname(__file__), "credentials")
CLIENT_SECRET_FILE = os.path.join(CREDENTIALS_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token.json")
AUDIT_SPREADSHEET_ID_FILE = os.path.join(CREDENTIALS_DIR, "audit_spreadsheet_id.txt")

ROLES_TO_EXPIRE = frozenset({"reader", "writer"})
DEFAULT_AUDIT_SHEET_TITLE = "Drive permission expiry log"


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


def _sheet_id_for_title(sheets_svc, spreadsheet_id: str, sheet_tab: str) -> int:
    meta = (
        sheets_svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute()
    )
    for sh in meta.get("sheets") or []:
        props = sh.get("properties") or {}
        if props.get("title") == sheet_tab:
            return int(props["sheetId"])
    raise ValueError(f"No sheet tab named {sheet_tab!r} in spreadsheet {spreadsheet_id}")


def format_audit_sheet_header(sheets_svc, spreadsheet_id: str, sheet_tab: str) -> None:
    """Bold header row, light header background, freeze row 1, set column widths."""
    sheet_id = _sheet_id_for_title(sheets_svc, spreadsheet_id, sheet_tab)
    end_col = SHEET_LOG_NUM_COLUMNS  # exclusive endColumnIndex
    # Light blue-gray header fill (professional, readable on screen).
    header_bg = {"red": 0.86, "green": 0.90, "blue": 0.94}
    width_by_col = [
        150,  # Run (UTC)
        200,  # Root folder ID
        200,  # Root folder name
        220,  # File ID
        220,  # File name
        200,  # Permission ID
        180,  # Grantee
        110,  # Grantee type
        90,  # Role
        170,  # Previous expiration
        170,  # New expiration
        85,  # Status
        300,  # Error detail
    ]
    requests: list[dict] = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": end_col,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": header_bg,
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
    ]
    for i, px in enumerate(width_by_col):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1,
                    },
                    "properties": {"pixelSize": px},
                    "fields": "pixelSize",
                }
            }
        )
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()


def sheet_ensure_headers(sheets, spreadsheet_id: str, sheet_tab: str) -> bool:
    rng = f"{sheet_tab}!A1:A1"
    existing = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
    )
    if existing.get("values"):
        return False
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [SHEET_HEADER_ROW]},
    ).execute()
    return True


def load_saved_spreadsheet_id() -> str | None:
    if not os.path.isfile(AUDIT_SPREADSHEET_ID_FILE):
        return None
    with open(AUDIT_SPREADSHEET_ID_FILE, encoding="utf-8") as f:
        return (f.read() or "").strip() or None


def save_audit_spreadsheet_id(spreadsheet_id: str) -> None:
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)
    with open(AUDIT_SPREADSHEET_ID_FILE, "w", encoding="utf-8") as f:
        f.write(spreadsheet_id.strip() + "\n")


def resolve_spreadsheet_id(cli_value: str | None) -> str | None:
    if cli_value and str(cli_value).strip():
        return str(cli_value).strip()
    env_id = (os.environ.get("SPREADSHEET_ID") or "").strip()
    if env_id:
        return env_id
    return load_saved_spreadsheet_id()


def create_audit_spreadsheet(sheets, *, title: str, sheet_tab: str) -> str:
    """Create a new spreadsheet with one tab and header row; return spreadsheetId."""
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": sheet_tab}}],
    }
    created = sheets.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    sid = created["spreadsheetId"]
    sheets.spreadsheets().values().update(
        spreadsheetId=sid,
        range=f"{sheet_tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [SHEET_HEADER_ROW]},
    ).execute()
    format_audit_sheet_header(sheets, sid, sheet_tab)
    return sid


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
            range=_sheet_append_range(sheet_tab),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": chunk},
        ).execute()


def iter_folder_tree_with_names(
    service, root_folder_id: str
):
    """Yield (file_id, item_name, root_folder_name) for the root, then every descendant."""
    root = (
        service.files()
        .get(
            fileId=root_folder_id,
            fields="id,name",
            supportsAllDrives=True,
        )
        .execute()
    )
    root_name = root.get("name", "")
    yield root_folder_id, root_name, root_name

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
                item_name = f.get("name", "")
                yield fid, item_name, root_name
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
    root_folder_name: str,
    file_name: str,
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
                    root_folder_name,
                    file_id,
                    file_name,
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


def cmd_format_audit_sheet_header(args: argparse.Namespace) -> None:
    """Update row 1 with display headers and apply professional header formatting."""
    args.spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet_id)
    if not args.spreadsheet_id:
        print(
            "No spreadsheet id: use --spreadsheet-id, set SPREADSHEET_ID, or save an id via --create-audit-sheet.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    creds = get_credentials(with_sheets_log=True)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        sheets.spreadsheets().values().update(
            spreadsheetId=args.spreadsheet_id,
            range=f"{args.sheet_tab}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [SHEET_HEADER_ROW]},
        ).execute()
        format_audit_sheet_header(sheets, args.spreadsheet_id, args.sheet_tab)
    except HttpError as e:
        print(
            f"Could not format sheet ({e}). Check Sheets API access, tab name --sheet-tab, and sharing.",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    url = f"https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}/edit"
    print(f"Audit sheet header updated: {url}", flush=True)


def _drive_folder_display_title(service, folder_id: str, fallback: str) -> str:
    try:
        meta = (
            service.files()
            .get(fileId=folder_id, fields="name", supportsAllDrives=True)
            .execute()
        )
        return (meta.get("name") or "").strip() or fallback
    except HttpError:
        return fallback


def cmd_create_audit_sheet(args: argparse.Namespace) -> None:
    """Create a log spreadsheet in the signed-in user's Drive; save ID for later runs."""
    creds = get_credentials(with_sheets_log=True)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    folder_title = _drive_folder_display_title(drive, args.folder_id, args.folder_id)
    doc_title = f"{args.audit_sheet_title} — {folder_title}"
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    try:
        sid = create_audit_spreadsheet(
            sheets,
            title=doc_title,
            sheet_tab=args.sheet_tab,
        )
    except HttpError as e:
        print(
            f"Could not create spreadsheet ({e}). Enable Google Sheets API on your Cloud project "
            f"and ensure OAuth includes the Sheets scope (delete credentials/token.json if you added Sheets later).",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    if args.save_audit_id:
        save_audit_spreadsheet_id(sid)
        print(f"Saved spreadsheet id to {AUDIT_SPREADSHEET_ID_FILE}", flush=True)
    url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
    print(f"Created audit sheet: {url}", flush=True)
    print(f"Export for scripts: export SPREADSHEET_ID={sid}", flush=True)


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
        default=None,
        help="Google Sheet ID (from the URL); or set SPREADSHEET_ID; or use saved id from --create-audit-sheet.",
    )
    parser.add_argument(
        "--sheet-tab",
        default=os.environ.get("SHEET_TAB", "Sheet1"),
        help="Worksheet tab name (default Sheet1 or SHEET_TAB)",
    )
    parser.add_argument(
        "--create-audit-sheet",
        action="store_true",
        help="Create a new Google Sheet for logs in your Drive, write headers, save its id locally (no expiry run).",
    )
    parser.add_argument(
        "--audit-sheet-title",
        default=os.environ.get("AUDIT_SHEET_TITLE", DEFAULT_AUDIT_SHEET_TITLE),
        help="Title for the spreadsheet created by --create-audit-sheet",
    )
    parser.add_argument(
        "--no-save-audit-id",
        action="store_true",
        help="With --create-audit-sheet, do not write credentials/audit_spreadsheet_id.txt",
    )
    parser.add_argument(
        "--format-sheet",
        action="store_true",
        help="Rewrite row 1 as human-readable headers and apply header formatting (freeze, widths); no expiry run.",
    )
    args = parser.parse_args()

    if args.create_audit_sheet:
        args.save_audit_id = not args.no_save_audit_id
        # folder_id already on args (default FOLDER_ID / DEFAULT_FOLDER_ID)
        cmd_create_audit_sheet(args)
        return

    if args.format_sheet:
        cmd_format_audit_sheet_header(args)
        return

    args.spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet_id)

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
        rn = _drive_folder_display_title(service, args.folder_id, "")
        tree_entries = [(args.folder_id, rn, rn)]
    else:
        tree_entries = list(iter_folder_tree_with_names(service, args.folder_id))

    total_updated = 0
    total_skipped = 0
    total_perms = 0
    log_rows: list[list[object]] | None = [] if use_sheet else None

    print(
        f"Processing {len(tree_entries)} item(s); roles={sorted(ROLES_TO_EXPIRE)}; expires {expire_str}",
        flush=True,
    )
    if use_sheet:
        print(f"Sheet log: {args.sheet_tab!r} in spreadsheet {args.spreadsheet_id}", flush=True)

    for i, (fid, file_name, root_folder_name) in enumerate(tree_entries, start=1):
        if len(tree_entries) > 1 and i % 50 == 1:
            print(f"… item {i}/{len(tree_entries)}", flush=True)
        u, s, n = process_file_permissions(
            service,
            fid,
            expire_str,
            root_folder_id=args.folder_id,
            root_folder_name=root_folder_name,
            file_name=file_name,
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
            if sheet_ensure_headers(sheets_svc, args.spreadsheet_id, args.sheet_tab):
                format_audit_sheet_header(
                    sheets_svc, args.spreadsheet_id, args.sheet_tab
                )
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
        f"Done. Items: {len(tree_entries)}; permission rows seen: {total_perms}; "
        f"updates: {total_updated}; skipped role/type: {total_skipped}."
    )


if __name__ == "__main__":
    main()
