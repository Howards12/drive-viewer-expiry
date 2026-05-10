# Google Drive folder access — time-limited reader & writer

This tool sets **`expirationTime`** on **reader** (view) and **writer** (edit) permissions for a **folder and every file and subfolder inside it**, so those access levels end automatically after the configured duration (default **24 hours**).

**Commenter**, **fileOrganizer**, and other roles are left unchanged. **Owners** are never modified.

## Requirements

- A Google account where the Drive API allows **`expirationTime`** on permissions (often **Google Workspace**; consumer accounts may reject some updates).
- A **Google Cloud** project with **Google Drive API** enabled.
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

## Behaviour notes

- **Recursive listing** uses `files.list` with `'parent' in parents` for each folder; **Shared drives** are supported via `supportsAllDrives` / `includeItemsFromAllDrives`.
- **Inherited permissions** on children: the API may allow or reject updating a given row; failures are printed as `FAIL` with the HTTP error.
- **Public / anyone** shares are skipped unless you pass **`--include-link`**.

## Security

Do not commit `credentials/client_secret.json` or `credentials/token.json`. They are listed in `.gitignore`.
