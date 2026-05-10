# Google Drive folder access — time-limited reader & writer

This tool sets **`expirationTime`** on **reader** (view) and **writer** (edit) permissions for a **folder and every file and subfolder inside it**, so those access levels end automatically after the configured duration (default **24 hours**).

**Commenter**, **fileOrganizer**, and other roles are left unchanged. **Owners** are never modified.

## Requirements

- A Google account where the Drive API allows **`expirationTime`** on permissions (often **Google Workspace**; consumer accounts may reject some updates).
- A **Google Cloud** project with **Google Drive API** and **Google Sheets API** enabled (Sheets only if you use logging below).
- **OAuth 2.0 Desktop app** credentials (`credentials/client_secret.json` or env vars).

## Setup

1. In [Google Cloud Console](https://console.cloud.google.com/), enable **Google Drive API** for your project.
2. **Credentials** → **OAuth client ID** → **Desktop app** → download JSON as `credentials/client_secret.json` (or set `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`).
3. Install dependencies:

   ```bash
   cd drive-viewer-expiry
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. First run opens a browser; tokens are stored in `credentials/token.json`.
5. **Optional audit Sheet:** Create a new Google Sheet, copy its ID from the URL (`https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`). Share the Sheet with the **same Google account** that runs the script (Editor access). If you add logging after an earlier run, delete `credentials/token.json` once so OAuth can include the Sheets scope.

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

Each successful run can **append one row per permission change** (status `ok` or `fail`) to a tab you choose (default `Sheet1`). **Where to view:** open that spreadsheet in the browser; new rows appear at the bottom after each run.

```bash
export SPREADSHEET_ID=your_sheet_id_from_the_url
# optional: export SHEET_TAB=Audit
python set_viewer_expiry.py
```

Or: `python set_viewer_expiry.py --spreadsheet-id YOUR_ID --sheet-tab Audit`

On first use, the script writes a header row if cell `A1` is empty. Columns include: run time (UTC), root folder id, file id, permission id, grantee, type, role, previous expiration, **new expiration**, status, and error text for failures.

**Note:** This log records **expiry changes the script made**, not who opened or viewed files in Drive (that requires Workspace admin reports or other tooling).

## Behaviour notes

- **Recursive listing** uses `files.list` with `'parent' in parents` for each folder; **Shared drives** are supported via `supportsAllDrives` / `includeItemsFromAllDrives`.
- **Inherited permissions** on children: the API may allow or reject updating a given row; failures are printed as `FAIL` with the HTTP error.
- **Public / anyone** shares are skipped unless you pass **`--include-link`**.

## Security

Do not commit `credentials/client_secret.json` or `credentials/token.json`. They are listed in `.gitignore`.
