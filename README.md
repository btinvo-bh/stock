# BT INVO

BT INVO is a local inventory manager backed by SQLite. It tracks suppliers, products, stock receipts and issues, purchase orders, current balances, low-stock alerts, and date/category reports.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`. The database is created as `bt_invo.sqlite3` in the project directory. Set `BT_INVO_DATABASE` to choose another database path, `BT_INVO_SECRET_KEY` to provide a session key, or `PORT` to choose another port.

When hosting behind an HTTPS reverse proxy, set `BT_INVO_PUBLIC_URL` to the app's public origin, for example `https://inventory.example.com` (no trailing slash), and restart the app. Google Drive displays the matching `/api/drive/callback` URI in Settings; add that exact URI to the Google OAuth Web client. Without this setting, the app uses the forwarded host and HTTPS scheme from the proxy.

## Deploy on Render

This app uses SQLite, so deploy it on a service with a persistent disk; a serverless filesystem is not suitable for the live inventory database. The included `render.yaml` configures a single Render web service, a persistent disk at `/var/data`, Gunicorn, and generated master-password and session-key secrets.

1. Keep the Git repository **private** if it contains `bt_invo.sqlite3`; that database includes inventory, account, and audit data. Include the database in the repository only if you want Render to import its current contents on first startup. The startup script copies it to the persistent disk once, sets the `jitesh` master password to Render's generated `BT_INVO_MASTER_PASSWORD`, and disables other existing accounts until the master recreates them. If the file is not included, the app starts with a clean database and creates the configured master account.
2. In Render, create a Blueprint from the private repository and apply `render.yaml`. The persistent disk requires a paid web-service plan; keep the service at one instance because the SQLite file is on a single attached disk.
3. After deployment, find the generated `BT_INVO_MASTER_PASSWORD` under the service's Environment settings and sign in as `jitesh`. Change this generated password after signing in. Do not publish or commit generated secrets.
4. For Google Drive, use the HTTPS callback URL shown in Settings and add it to the Google OAuth Web client. OAuth credentials and their encryption key are stored under `/var/data/instance` so they survive deploys.
5. Keep regular off-host backups. The persistent disk protects data across deploys and restarts, but is not itself a backup.

## First-time account setup

The app does not create accounts with published default passwords. Before the first start with a new, empty database, configure `BT_INVO_MASTER_USERNAME` and `BT_INVO_MASTER_PASSWORD` in the server environment. The bootstrap password must be at least 12 characters. The first master account is created once; changing these environment variables later does not change existing accounts. If the database already contains accounts, those accounts are retained and will be required to change their passwords before using the app after this security update. Master and administrator users can add staff accounts; only the master user can grant the master role. Staff can record stock movements and orders but cannot edit products or supplier records.

## Inventory rules and settings

- The item catalogue includes CHOCOLATE, CAKE, FLOWERS, COFFE, CLEANING, and GENERAL.
- Product units are kg, gram, pieces, and box. Initial compositions are 1 × 5 kg, 1 × 0.5 gram, 1 × 1 piece, and 6 × 6 = 36 pieces. The master/admin can define additional units and per-item reorder levels in Settings.
- Stock entry quantity is entered in packages. BT INVO stores both package quantity and converted base-unit quantity. The dashboard reports product counts and never combines unlike units.
- A stock receipt records price per package in the configured currency (initially INR) and can include a supplier invoice number. New receipts default to the latest purchase price. Currency can be changed before the first stock transaction, then is locked to protect historical totals.
- A stock issue cannot exceed current available quantity. Each item's base unit and package conversion become fixed after it has stock history; create a new item if its packaging or dimension changes.
- Purchase orders progress from open to partially received to received. Receipts can be linked to an order, partial delivery is supported, and an open/partial order can be cancelled.
- Low-stock alerts compare current base-unit quantity with each item's reorder level (default 5). Reports include opening balance, receipts, issues, corrections, and closing balance for a selected period.
- Administrators can correct counts with a reason and note. Adjustments are kept separately from receipts/issues and appear in reports and audit history.
- Item records with movement history are archived instead of deleted so reports remain consistent. Settings provide category/unit configuration, audit history, CSV exports, and a consistent SQLite backup download.
- Reports can be printed from the browser (choose Save as PDF in the print dialog) or downloaded directly as a filtered PDF.
- Reports use A4 landscape pages with black-and-white print styling. Master/admin users can configure the shop name, address, telephone, mobile number, and PNG/JPEG logo under Settings & data; those details appear in report headers with a subtle “Powered by JC007” watermark in the footer.
- Users can change their own password in Settings. Master/admin account changes, stock changes, and inventory setup changes are recorded in the audit history.
- The master account can configure Google Drive in Settings & data:
	1. In Google Cloud Console, create/select a project and enable the Google Drive API.
	2. Configure the OAuth consent screen and add the account that will own the backup as a test user if the app is still in testing.
	3. Create an OAuth client ID of type Web application. Copy the authorized redirect URI shown in BT INVO into the client’s Authorized redirect URIs, then download its JSON file.
	4. In BT INVO, upload that JSON and choose Connect Google Drive. Sign in to the backup-owning Google account and approve access.
- Drive sync uses Google’s private app-data folder, not a visible file in My Drive. SQLite remains the working database; successful changes queue a consistent snapshot, and a failed upload retries about once a minute while local work remains available. A master can request a manual sync or download the Drive backup.
- Drive sync backs up one BT INVO server’s database; it does not merge separate databases from multiple servers. OAuth tokens are encrypted with a local key in the ignored `instance/` directory and are omitted from cloud snapshots. Reconnect Drive if that key is lost.