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
5. **Audit Sheet (pick one):**
   - **Easiest:** after credentials exist, run **`python set_viewer_expiry.py --create-audit-sheet`** on your computer. That opens a browser, creates a titled spreadsheet in **your** Google Drive, writes the column headers, and saves the id to `credentials/audit_spreadsheet_id.txt` (gitignored). Later runs pick up that id automatically.
   - **Manual:** create a blank Sheet yourself, copy its id from the URL, set `SPREADSHEET_ID`, and share the Sheet with the same account that runs the script if needed.
   - If you already ran the tool **without** Sheets, delete `credentials/token.json` once before `--create-audit-sheet` so OAuth can add the Sheets scope.

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

Each successful run can **append one row per permission change** (status `ok` or `fail`) to a tab you choose (default `Sheet1`). **Where to view:** open that spreadsheet in the browser; new rows appear at the bottom after each run.

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

On first use, the script writes a header row if cell `A1` is empty. Columns include: run time (UTC), root folder id, file id, permission id, grantee, type, role, previous expiration, **new expiration**, status, and error text for failures.

To refresh human-readable headers and header styling on an **existing** sheet (e.g. after an older run used snake_case titles), run: `python set_viewer_expiry.py --format-sheet` (uses `SPREADSHEET_ID`, saved id, or `--spreadsheet-id`; optional `--sheet-tab`).

**Note:** This log records **expiry changes the script made**, not who opened or viewed files in Drive (that requires Workspace admin reports or other tooling).

## Behaviour notes

- **Recursive listing** uses `files.list` with `'parent' in parents` for each folder; **Shared drives** are supported via `supportsAllDrives` / `includeItemsFromAllDrives`.
- **Inherited permissions** on children: the API may allow or reject updating a given row; failures are printed as `FAIL` with the HTTP error.
- **Public / anyone** shares are skipped unless you pass **`--include-link`**.

## Security

Do not commit `credentials/client_secret.json` or `credentials/token.json`. They are listed in `.gitignore`.
