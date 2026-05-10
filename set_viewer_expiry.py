#!/usr/bin/env python3
"""
Set expirationTime on Google Drive permissions for a folder and every item inside it.

Targets **reader** (view) and **writer** (edit) roles only; skips owner and other roles.

Requires OAuth with scope https://www.googleapis.com/auth/drive
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

SCOPES = ["https://www.googleapis.com/auth/drive"]

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


def get_credentials() -> Credentials:
    creds: Credentials | None = None
    if os.path.isfile(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        client_config = _oauth_client_config_from_env()
        if client_config:
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
        elif os.path.isfile(CLIENT_SECRET_FILE):
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
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
    include_link: bool,
    dry_run: bool,
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

        current_exp = perm.get("expirationTime")
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
        except HttpError as e:
            print(f"FAIL {label} ({role}): {e}", file=sys.stderr)

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
    args = parser.parse_args()

    if args.hours <= 0:
        print("--hours must be positive", file=sys.stderr)
        sys.exit(1)

    expire_at = datetime.now(timezone.utc) + timedelta(hours=args.hours)
    expire_str = rfc3339_utc(expire_at)

    creds = get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    if args.root_only:
        file_ids = [args.folder_id]
    else:
        file_ids = list(iter_folder_tree_file_ids(service, args.folder_id))

    total_updated = 0
    total_skipped = 0
    total_perms = 0

    print(f"Processing {len(file_ids)} item(s); roles={sorted(ROLES_TO_EXPIRE)}; expires {expire_str}", flush=True)

    for i, fid in enumerate(file_ids, start=1):
        if len(file_ids) > 1 and i % 50 == 1:
            print(f"… item {i}/{len(file_ids)}", flush=True)
        u, s, n = process_file_permissions(
            service,
            fid,
            expire_str,
            include_link=args.include_link,
            dry_run=args.dry_run,
        )
        total_updated += u
        total_skipped += s
        total_perms += n
        if args.throttle_seconds > 0:
            time.sleep(args.throttle_seconds)

    print(
        f"Done. Items: {len(file_ids)}; permission rows seen: {total_perms}; "
        f"updates: {total_updated}; skipped role/type: {total_skipped}."
    )


if __name__ == "__main__":
    main()
