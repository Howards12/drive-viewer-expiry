#!/usr/bin/env python3
"""
Set expirationTime on Google Drive permissions for a folder and every item inside it.

Targets **reader** (view) and **writer** (edit) roles only; skips owner and other roles.

OAuth: Drive scope always; Spreadsheets scope only when logging to a Sheet (--spreadsheet-id);
Drive Activity readonly scope when syncing access history (--sync-access-activity / --also-log-access).
"""

from __future__ import annotations

import argparse
import json
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
ACTIVITY_SCOPE = "https://www.googleapis.com/auth/drive.activity.readonly"

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

# Access / Drive Activity v2 log (second tab).
ACTIVITY_HEADER_ROW = [
    "Time (UTC)",
    "Actor",
    "Action",
    "Target type",
    "Item ID",
    "Item name",
    "Root folder ID",
    "Root folder name",
    "Detail",
]
ACTIVITY_LOG_NUM_COLUMNS = len(ACTIVITY_HEADER_ROW)

# Keys on ActionDetail (v2) we surface in the Action column and Detail excerpt.
_ACTION_DETAIL_FIELD_ORDER = (
    "create",
    "edit",
    "move",
    "rename",
    "delete",
    "restore",
    "permissionChange",
    "comment",
    "dlpChange",
    "reference",
    "settingsChange",
    "appliedLabelChange",
)


def _sheet_append_range(sheet_tab: str, num_columns: int) -> str:
    """Range like 'Tab!A:M' covering all log columns."""
    n = num_columns
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
DEFAULT_ACTIVITY_SHEET_TAB = "Access log"


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


def _required_scopes(with_sheets_log: bool, with_activity: bool) -> list[str]:
    scopes = [DRIVE_SCOPE]
    if with_sheets_log:
        scopes.append(SHEETS_SCOPE)
    if with_activity:
        scopes.append(ACTIVITY_SCOPE)
    return scopes


def _needs_scope_reauth(creds: Credentials, required: list[str]) -> bool:
    have = set(creds.scopes or [])
    return any(s not in have for s in required)


def get_credentials(with_sheets_log: bool, with_activity: bool = False) -> Credentials:
    required = _required_scopes(with_sheets_log, with_activity)
    creds: Credentials | None = None
    if os.path.isfile(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, required)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if creds and _needs_scope_reauth(creds, required):
        creds = None

    if not creds or not creds.valid:
        client_config = _oauth_client_config_from_env()
        if client_config:
            flow = InstalledAppFlow.from_client_config(client_config, required)
        elif os.path.isfile(CLIENT_SECRET_FILE):
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, required)
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


def format_log_sheet_header(
    sheets_svc,
    spreadsheet_id: str,
    sheet_tab: str,
    *,
    width_by_col: list[int],
) -> None:
    """Bold header row, light header background, freeze row 1, set column widths."""
    sheet_id = _sheet_id_for_title(sheets_svc, spreadsheet_id, sheet_tab)
    end_col = len(width_by_col)
    header_bg = {"red": 0.86, "green": 0.90, "blue": 0.94}
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


def format_audit_sheet_header(sheets_svc, spreadsheet_id: str, sheet_tab: str) -> None:
    """Bold header row, light header background, freeze row 1, set column widths (expiry tab)."""
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
    format_log_sheet_header(sheets_svc, spreadsheet_id, sheet_tab, width_by_col=width_by_col)


def format_activity_sheet_header(sheets_svc, spreadsheet_id: str, sheet_tab: str) -> None:
    """Header styling for the Drive Activity access log tab."""
    width_by_col = [
        180,  # Time (UTC)
        220,  # Actor
        140,  # Action
        100,  # Target type
        220,  # Item ID
        240,  # Item name
        200,  # Root folder ID
        200,  # Root folder name
        360,  # Detail
    ]
    format_log_sheet_header(sheets_svc, spreadsheet_id, sheet_tab, width_by_col=width_by_col)


def sheet_ensure_headers(
    sheets, spreadsheet_id: str, sheet_tab: str, header_row: list[str]
) -> bool:
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
        body={"values": [header_row]},
    ).execute()
    return True


def ensure_sheet_tab_exists(sheets_svc, spreadsheet_id: str, sheet_tab: str) -> None:
    meta = (
        sheets_svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title))",
        )
        .execute()
    )
    titles = {
        (sh.get("properties") or {}).get("title")
        for sh in (meta.get("sheets") or [])
    }
    if sheet_tab in titles:
        return
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_tab}}}]},
    ).execute()


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
    sheets,
    spreadsheet_id: str,
    sheet_tab: str,
    rows: list[list[object]],
    *,
    num_columns: int = SHEET_LOG_NUM_COLUMNS,
) -> None:
    if not rows:
        return
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        chunk = [[_sheet_cell(v) for v in row] for row in rows[i : i + chunk_size]]
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=_sheet_append_range(sheet_tab, num_columns),
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": chunk},
        ).execute()


def _format_actor(actor: dict) -> str:
    if actor.get("user"):
        u = actor["user"]
        if u.get("knownUser"):
            ku = u["knownUser"]
            pn = (ku.get("personName") or "").strip()
            if ku.get("isCurrentUser"):
                return f"{pn} (current user)" if pn else "current user"
            return pn or "known user"
        if u.get("unknownUser") is not None:
            return "unknown user"
        if u.get("deletedUser") is not None:
            return "deleted user"
    if actor.get("administrator") is not None:
        return "administrator"
    if actor.get("anonymous") is not None:
        return "anonymous"
    if actor.get("system") is not None:
        se = actor["system"]
        st = (se.get("type") or "").strip() if isinstance(se, dict) else ""
        return f"system:{st}" if st else "system"
    if actor.get("impersonation") is not None:
        return "impersonation"
    return "actor"


def _actors_summary(actors: list[dict] | None) -> str:
    if not actors:
        return ""
    parts = [_format_actor(a) for a in actors if a]
    dedup: list[str] = []
    for p in parts:
        if p and p not in dedup:
            dedup.append(p)
    return "; ".join(dedup)


def _nonempty_detail_keys(detail: dict | None) -> list[str]:
    if not detail:
        return []
    return [k for k in _ACTION_DETAIL_FIELD_ORDER if detail.get(k) is not None]


def _action_labels_from_activity(activity: dict) -> str:
    pad = activity.get("primaryActionDetail")
    keys = _nonempty_detail_keys(pad if isinstance(pad, dict) else None)
    if keys:
        return ", ".join(keys)
    seen: list[str] = []
    for act in activity.get("actions") or []:
        d = act.get("detail")
        if not isinstance(d, dict):
            continue
        for k in _nonempty_detail_keys(d):
            if k not in seen:
                seen.append(k)
    return ", ".join(seen)


def _detail_excerpt(detail: dict | None, max_len: int = 450) -> str:
    if not detail:
        return ""
    pieces: list[str] = []
    if detail.get("rename"):
        r = detail["rename"]
        pieces.append(
            f'rename "{r.get("oldTitle", "")}" → "{r.get("newTitle", "")}"'
        )
    if detail.get("move"):
        m = detail["move"]
        add_n = len(m.get("addedParents") or [])
        rem_n = len(m.get("removedParents") or [])
        if add_n or rem_n:
            pieces.append(f"move parents +{add_n}/-{rem_n}")
    if detail.get("permissionChange"):
        pc = detail["permissionChange"]
        add_n = len(pc.get("addedPermissions") or [])
        rem_n = len(pc.get("removedPermissions") or [])
        if add_n or rem_n:
            pieces.append(f"permissions +{add_n}/-{rem_n}")
    if detail.get("reference"):
        ref = detail["reference"]
        t = ref.get("type") if isinstance(ref, dict) else None
        if t:
            pieces.append(f"reference:{t}")
    if detail.get("restore"):
        rest = detail["restore"]
        t = rest.get("type") if isinstance(rest, dict) else None
        if t:
            pieces.append(f"restore:{t}")
    if not pieces:
        # Fallback: compact JSON of non-null action keys only (truncated).
        slim = {k: detail[k] for k in _ACTION_DETAIL_FIELD_ORDER if detail.get(k) is not None}
        if slim:
            try:
                s = json.dumps(slim, default=str, separators=(",", ":"))
            except (TypeError, ValueError):
                s = str(slim)
            pieces.append(s[:max_len])
    else:
        s = "; ".join(pieces)
    return s[:max_len]


def _primary_target_fields(activity: dict) -> tuple[str, str, str]:
    """Returns (target_type, item_id, item_name)."""
    for t in activity.get("targets") or []:
        if not isinstance(t, dict):
            continue
        di = t.get("driveItem")
        if isinstance(di, dict):
            raw_name = (di.get("name") or "").strip()
            item_id = raw_name[6:] if raw_name.startswith("items/") else raw_name
            title = (di.get("title") or "").strip()
            if di.get("driveFolder") is not None:
                ttype = "folder"
            elif di.get("driveFile") is not None:
                ttype = "file"
            else:
                ttype = "item"
            return ttype, item_id, title
        dr = t.get("drive")
        if isinstance(dr, dict):
            nm = (dr.get("name") or "").strip()
            short = nm.split("/")[-1] if nm else ""
            return "shared_drive", short, (dr.get("title") or "").strip()
    return "", "", ""


def _activity_timestamp_utc(activity: dict) -> str:
    ts = activity.get("timestamp")
    if ts:
        return str(ts).replace("+00:00", "Z") if str(ts).endswith("+00:00") else str(ts)
    tr = activity.get("timeRange") or {}
    if isinstance(tr, dict):
        if tr.get("endTime"):
            x = str(tr["endTime"])
            return x.replace("+00:00", "Z") if x.endswith("+00:00") else x
        if tr.get("startTime"):
            x = str(tr["startTime"])
            return x.replace("+00:00", "Z") if x.endswith("+00:00") else x
    latest = ""
    for act in activity.get("actions") or []:
        if not isinstance(act, dict):
            continue
        if act.get("timestamp"):
            latest = str(act["timestamp"])
        tr2 = act.get("timeRange") or {}
        if isinstance(tr2, dict) and tr2.get("endTime"):
            latest = str(tr2["endTime"])
    if latest:
        return latest.replace("+00:00", "Z") if latest.endswith("+00:00") else latest
    return ""


def drive_activity_to_row(
    activity: dict,
    *,
    root_folder_id: str,
    root_folder_name: str,
) -> list[object]:
    detail_src = activity.get("primaryActionDetail")
    if not isinstance(detail_src, dict):
        detail_src = None
    if detail_src is None and activity.get("actions"):
        first = activity["actions"][0]
        if isinstance(first, dict) and isinstance(first.get("detail"), dict):
            detail_src = first["detail"]
    return [
        _activity_timestamp_utc(activity),
        _actors_summary(activity.get("actors")),
        _action_labels_from_activity(activity),
        *_primary_target_fields(activity),
        root_folder_id,
        root_folder_name,
        _detail_excerpt(detail_src),
    ]


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
            api_role = perm.get("role") or role
            service.permissions().update(
                fileId=file_id,
                permissionId=pid,
                body={"expirationTime": expire_str, "role": api_role},
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


def sync_access_activity_to_sheet(
    creds: Credentials,
    *,
    spreadsheet_id: str,
    activity_tab: str,
    folder_id: str,
    activity_hours: float,
) -> int:
    """Query Drive Activity v2 and append rows to the activity tab. Returns rows appended."""
    if activity_hours <= 0:
        raise ValueError("activity_hours must be positive")
    since = datetime.now(timezone.utc) - timedelta(hours=activity_hours)
    since_str = rfc3339_utc(since)
    filter_str = f'time >= "{since_str}"'

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    root_folder_name = _drive_folder_display_title(drive, folder_id, "")

    activity_svc = build("driveactivity", "v2", credentials=creds, cache_discovery=False)

    activities: list[dict] = []
    page_token: str | None = None
    while True:
        body: dict = {
            "ancestorName": f"items/{folder_id}",
            "filter": filter_str,
            "pageSize": 50,
            "consolidationStrategy": {"none": {}},
        }
        if page_token:
            body["pageToken"] = page_token
        resp = activity_svc.activity().query(body=body).execute()
        activities.extend(resp.get("activities") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    rows = [
        drive_activity_to_row(
            act,
            root_folder_id=folder_id,
            root_folder_name=root_folder_name,
        )
        for act in activities
        if isinstance(act, dict)
    ]

    sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    ensure_sheet_tab_exists(sheets_svc, spreadsheet_id, activity_tab)
    if sheet_ensure_headers(sheets_svc, spreadsheet_id, activity_tab, ACTIVITY_HEADER_ROW):
        format_activity_sheet_header(sheets_svc, spreadsheet_id, activity_tab)
    if rows:
        sheet_append_rows(
            sheets_svc,
            spreadsheet_id,
            activity_tab,
            rows,
            num_columns=ACTIVITY_LOG_NUM_COLUMNS,
        )
    return len(rows)


def cmd_sync_access_activity(args: argparse.Namespace) -> None:
    spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet_id)
    if not spreadsheet_id:
        print(
            "No spreadsheet id: use --spreadsheet-id, set SPREADSHEET_ID, or save an id via --create-audit-sheet.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if args.activity_hours <= 0:
        print("--activity-hours must be positive", file=sys.stderr)
        raise SystemExit(1)
    creds = get_credentials(with_sheets_log=True, with_activity=True)
    try:
        n = sync_access_activity_to_sheet(
            creds,
            spreadsheet_id=spreadsheet_id,
            activity_tab=args.activity_tab,
            folder_id=args.folder_id,
            activity_hours=args.activity_hours,
        )
    except HttpError as e:
        print(
            f"Access activity sync failed ({e}). Enable Google Drive Activity API for your Cloud project, "
            f"ensure OAuth includes drive.activity.readonly (delete credentials/token.json if you added the scope later), "
            f"and share the spreadsheet with the same account.",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
    print(f"Appended {n} access activity row(s) to tab {args.activity_tab!r}: {url}", flush=True)


def cmd_format_audit_sheet_header(args: argparse.Namespace) -> None:
    """Update row 1 with display headers and apply professional header formatting."""
    args.spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet_id)
    if not args.spreadsheet_id:
        print(
            "No spreadsheet id: use --spreadsheet-id, set SPREADSHEET_ID, or save an id via --create-audit-sheet.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    creds = get_credentials(with_sheets_log=True, with_activity=False)
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
    creds = get_credentials(with_sheets_log=True, with_activity=False)
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
        help="Worksheet tab name for permission expiry log (default Sheet1 or SHEET_TAB)",
    )
    parser.add_argument(
        "--activity-tab",
        default=os.environ.get("ACTIVITY_SHEET_TAB", DEFAULT_ACTIVITY_SHEET_TAB),
        help=f"Worksheet tab for Drive access activity (default {DEFAULT_ACTIVITY_SHEET_TAB!r} or ACTIVITY_SHEET_TAB)",
    )
    parser.add_argument(
        "--activity-hours",
        type=float,
        default=float(os.environ.get("ACTIVITY_HOURS", "24")),
        help="For access activity: include events back this many hours from now (default 24 or ACTIVITY_HOURS)",
    )
    parser.add_argument(
        "--sync-access-activity",
        action="store_true",
        help="Only fetch Drive Activity for --folder-id and append rows to --activity-tab (no permission expiry).",
    )
    parser.add_argument(
        "--also-log-access",
        action="store_true",
        help="After appending permission expiry rows, also append access activity for the same folder and spreadsheet.",
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
        cmd_create_audit_sheet(args)
        return

    if args.format_sheet:
        cmd_format_audit_sheet_header(args)
        return

    if args.sync_access_activity:
        cmd_sync_access_activity(args)
        return

    args.spreadsheet_id = resolve_spreadsheet_id(args.spreadsheet_id)

    if args.hours <= 0:
        print("--hours must be positive", file=sys.stderr)
        sys.exit(1)

    if args.also_log_access and not args.spreadsheet_id:
        print(
            "--also-log-access requires a spreadsheet (SPREADSHEET_ID, saved audit id, or --spreadsheet-id).",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.also_log_access and args.dry_run:
        print("--also-log-access is not compatible with --dry-run", file=sys.stderr)
        sys.exit(1)

    if args.also_log_access and args.activity_hours <= 0:
        print("--activity-hours must be positive when using --also-log-access", file=sys.stderr)
        sys.exit(1)

    expire_at = datetime.now(timezone.utc) + timedelta(hours=args.hours)
    expire_str = rfc3339_utc(expire_at)
    run_utc = rfc3339_utc(datetime.now(timezone.utc))

    use_sheet = bool(args.spreadsheet_id) and not args.dry_run
    creds = get_credentials(
        with_sheets_log=use_sheet or args.also_log_access,
        with_activity=args.also_log_access,
    )
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
            if sheet_ensure_headers(sheets_svc, args.spreadsheet_id, args.sheet_tab, SHEET_HEADER_ROW):
                format_audit_sheet_header(sheets_svc, args.spreadsheet_id, args.sheet_tab)
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

    if args.also_log_access and args.spreadsheet_id:
        try:
            n_act = sync_access_activity_to_sheet(
                creds,
                spreadsheet_id=args.spreadsheet_id,
                activity_tab=args.activity_tab,
                folder_id=args.folder_id,
                activity_hours=args.activity_hours,
            )
            print(
                f"Access activity: appended {n_act} row(s) to tab {args.activity_tab!r} "
                f"(window: last {args.activity_hours} h).",
                flush=True,
            )
        except HttpError as e:
            print(
                f"Access activity logging failed ({e}). Enable Drive Activity API and drive.activity.readonly; "
                f"delete credentials/token.json if the scope was added after first auth.",
                file=sys.stderr,
            )

    print(
        f"Done. Items: {len(tree_entries)}; permission rows seen: {total_perms}; "
        f"updates: {total_updated}; skipped role/type: {total_skipped}."
    )


if __name__ == "__main__":
    main()
