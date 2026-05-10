# Google Drive folder access — time-limited reader & writer

This tool sets **`expirationTime`** on **reader** (view) and **writer** (edit) permissions for a **folder and every file and subfolder inside it**, so those access levels end automatically after the configured duration (default **24 hours**).

**Commenter**, **fileOrganizer**, and other roles are left unchanged. **Owners** are never modified.

## Requirements

- A Google account where the Drive API allows **`expirationTime`** on permissions (often **Google Workspace**; consumer accounts may reject some updates).
- A **Google Cloud** project with **Google Drive API** and **Google Sheets API** enabled (Sheets only if you use logging below). For **access activity** logging, also enable **Google Drive Activity API** (`driveactivity.googleapis.com`) and add the OAuth scope **`https://www.googleapis.com/auth/drive.activity.readonly`** to your app (same as in code).
- **OAuth 2.0 Desktop app** credentials (`credentials/client_secret.json` or env vars).

## Setup

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **Google Drive API** for your project. If you use Sheet logging, enable **Google Sheets API**. For **access activity** (`--sync-access-activity` / `--also-log-access`), enable **Google Drive Activity API** (API id `driveactivity.googleapis.com`; search “Drive Activity API” in **APIs & Services** → **Library**).
2. **Credentials** → **OAuth client ID** → **Desktop app** → download JSON as `credentials/client_secret.json` (or set `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`).
3. Install dependencies:

   ```bash
   cd drive-viewer-expiry
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. First run opens a browser; tokens are stored in `credentials/token.json`.
5. **Audit Sheet (pick one):**
   - **Easiest:** after credentials exist, run **`python set_viewer_expiry.py --create-audit-sheet`** on your computer. That opens a browser, creates a titled spreadsheet in **your** Google Drive, writes the column headers, and saves the id to `credentials/audit_spreadsheet_id.txt` (gitignored). Later runs pick up that id automatically.
   - **Manual:** create a blank Sheet yourself, copy its id from the URL, set `SPREADSHEET_ID`, and share the Sheet with the same account that runs the script if needed.
   - If you already ran the tool **without** Sheets, delete `credentials/token.json` once before `--create-audit-sheet` so OAuth can add the Sheets scope.
   - If you add **Drive Activity** (access log) after an earlier auth, delete `credentials/token.json` once so OAuth can add `drive.activity.readonly`.

**I (or any remote assistant) cannot log into your Google or Cloud Console for you** — enabling APIs, placing `client_secret.json`, and completing the browser consent step has to happen on your side. The `--create-audit-sheet` step only needs that one-time setup, then it finishes Sheet creation for you.

## Usage

Process the default folder and **everything inside it** (recursive):

```bash
export FOLDER_ID=1FmlgqUQwqYFzOOdXYaJZvBWYDP7sD1Ve
python set_viewer_expiry.py
```

Only the **root folder** (no recursion):

```bash
python set_viewer_expiry.py --root-only
```

Include **link / anyone / domain** permissions:

```bash
python set_viewer_expiry.py --include-link
```

Large trees: optional throttle between items (helps with quota):

```bash
python set_viewer_expiry.py --throttle-seconds 0.2
```

Dry run:

```bash
python set_viewer_expiry.py --dry-run
```

### Daily log in Google Sheets

The spreadsheet can hold **two kinds of logs** (separate tabs):

1. **Permission expiry log** (default tab `Sheet1` or `SHEET_TAB` / `--sheet-tab`) — one row per **permission expiry change** this script applied (who was granted what role, previous/new expiration, status). This is **not** a full audit of every Drive event.
2. **Access activity log** (default tab **`Access log`**, or `ACTIVITY_SHEET_TAB` / `--activity-tab`) — rows from the **Google Drive Activity API v2** for the same `--folder-id` tree: edits, moves, renames, permission changes, etc., over a time window you choose.

**Drive Activity API availability:** The API is documented under **Google Workspace** Drive. **Consumer (personal Gmail) accounts** may return no or limited activity depending on Google’s policies and activity history settings—**test with the account and folder you care about**. If calls fail with 403 or empty results, confirm the API is enabled, the OAuth scope is granted, and the signed-in user can see activity for that content.

Each successful expiry run can **append** to the expiry tab. Access events are appended only when you run **`--sync-access-activity`** or **`--also-log-access`** (see below).

If you used **`--create-audit-sheet`**, you normally **do not** need `SPREADSHEET_ID`; the saved id is read automatically.

```bash
# One-time: create Sheet + save id (after OAuth / client_secret are in place)
python set_viewer_expiry.py --create-audit-sheet

# Routine runs (uses saved spreadsheet id if present)
python set_viewer_expiry.py
```

Or set explicitly:

```bash
export SPREADSHEET_ID=your_sheet_id_from_the_url
python set_viewer_expiry.py
```

Or: `python set_viewer_expiry.py --spreadsheet-id YOUR_ID --sheet-tab Audit`

On first use, the script writes a header row if cell `A1` is empty on that tab. **Expiry tab** columns: run time (UTC), root folder id, **root folder name**, file id, **file name**, permission id, grantee, type, role, previous expiration, **new expiration**, status, and error detail.

**Access activity tab** columns (Title Case): Time (UTC), Actor, Action, Target type, Item ID, Item name, Root folder ID, Root folder name, Detail. The first time that tab is used, the script **creates the tab** if needed, writes headers when `A1` is empty, then appends rows (same styling pattern as the expiry tab: bold header, freeze, widths).

`--create-audit-sheet` sets the **spreadsheet document title** to `AUDIT_SHEET_TITLE — <root folder name>` (root folder comes from `FOLDER_ID` / `--folder-id`). It does not add the access tab until you run an access sync.

After upgrading column layout, run `python set_viewer_expiry.py --format-sheet` to rewrite row 1 and column widths for the **expiry** tab only (uses `SPREADSHEET_ID`, saved id, or `--spreadsheet-id`; optional `--sheet-tab`).

#### Sync access activity only (e.g. daily cron)

Query Drive Activity under `ancestorName = items/<FOLDER_ID>` with `time >=` (now − hours), paginate, and append to the access tab—**no** permission expiry updates:

```bash
python set_viewer_expiry.py --sync-access-activity --folder-id "$FOLDER_ID"
```

Optional: `--activity-hours 24` (default **24**, float allowed), `--activity-tab "Access log"` (or env `ACTIVITY_SHEET_TAB`), same spreadsheet resolution as expiry (`SPREADSHEET_ID` / saved id / `--spreadsheet-id`).

#### Run expiry and then access log in one invocation

```bash
python set_viewer_expiry.py --also-log-access
```

Uses `--activity-hours` (default 24) and `--activity-tab` for the second append. Requires a spreadsheet id (not compatible with `--dry-run`).

**Note:** **Expiration timestamps** you set with this tool appear on the **expiry** tab. **Drive Activity** events (open/edit/move/share, etc.) appear on the **access** tab. They are not merged automatically; combine in Sheets manually if you need one view.

## Automatic daily updates (no manual run)

You can run **`scripts/run-daily-audit.sh`** on a schedule so **`--also-log-access`** runs without opening a terminal. That **re-applies** reader/writer expiries for the tree (using **`EXPIRY_HOURS`**, default 24), **appends rows to the expiry tab**, then **appends access activity** to **`ACTIVITY_SHEET_TAB`**. The script uses **`set -euo pipefail`**, resolves the repo root, prefers **`.venv/bin/python`** when present, and forwards **`SPREADSHEET_ID`**, **`SHEET_TAB`**, **`ACTIVITY_SHEET_TAB`**, **`ACTIVITY_HOURS`**, **`EXPIRY_HOURS`**, and **`FOLDER_ID`** when set in the environment.

**Rolling window:** a **daily** expiry run sets **`expirationTime` = now + `EXPIRY_HOURS` again**, so access **keeps getting extended** while the job runs. To let permissions actually lapse, **stop** the daily expiry job or run expiry **once** manually.

**Credentials:** `credentials/token.json` must exist on the machine that runs the job and stay valid (refresh tokens). The **first** OAuth browser consent is still **manual** on that machine; scheduled runs only work after that. You need scopes for **Drive**, **Sheets**, and **Drive Activity** (see `--sync-access-activity` / delete `token.json` after enabling APIs).

**Access-only:** In `scripts/run-daily-audit.sh`, swap the active line to **`--sync-access-activity`** if you only want the access tab (no expiry tab updates).

### macOS (`launchd`)

1. Create log directory: **`mkdir -p ~/Library/Logs/drive-viewer-expiry`**
2. Copy **`launchd/com.google.driveviewerexpiry.daily.plist.example`** to **`~/Library/LaunchAgents/`** (e.g. same filename without `.example`), open the copy in an editor, and replace every **`__PROJECT_ROOT__`** with the **absolute path** to this repo (the folder that contains `set_viewer_expiry.py` and `scripts/`). Adjust **`Hour`** / **`Minute`** under **`StartCalendarInterval`** if you want a time other than **07:00**. Optionally add an **`EnvironmentVariables`** dict to the plist for `FOLDER_ID`, `SPREADSHEET_ID`, etc.
3. Load: **`launchctl load ~/Library/LaunchAgents/com.google.driveviewerexpiry.daily.plist`** (use your actual plist filename).

**When the Mac is asleep or off:** `StartCalendarInterval` does not fire at the exact minute while the system is asleep. **launchd** runs the job **after the Mac wakes** (the event is not permanently skipped). For a reliable wall-clock time, the machine must be **on and awake** at the scheduled time (or accept post-wake execution).

### Linux (`cron`)

Example **daily 07:00** (adjust paths and variables):

```bash
0 7 * * * FOLDER_ID=your_folder_id /path/to/drive-viewer-expiry/scripts/run-daily-audit.sh
```

Set `SPREADSHEET_ID` and other variables in the crontab line or source a small env file before the script.

### GitHub Actions / cloud

Running this tool on a schedule in **GitHub Actions** or another host is possible in principle (e.g. OAuth **refresh tokens** stored as secrets), but setup and token lifecycle are **out of scope** for this snippet—use the local `launchd` / `cron` flow above unless you invest in a proper server-side OAuth story.

## Behaviour notes

- **Recursive listing** uses `files.list` with `'parent' in parents` for each folder; **Shared drives** are supported via `supportsAllDrives` / `includeItemsFromAllDrives`.
- **Inherited permissions** on children: the API may allow or reject updating a given row; failures are printed as `FAIL` with the HTTP error.
- **Public / anyone** shares are skipped unless you pass **`--include-link`**.

## Security

Do not commit `credentials/client_secret.json` or `credentials/token.json`. They are listed in `.gitignore`.
