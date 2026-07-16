# Eris Deployment Checklist

Hand this to whoever deploys Argus on the Eris server after you push the Splunk LDAP changes.

## After `git pull`

### 1. Install new dependency

```bash
cd /opt/devops-agent   # or your APP_DIR
pip install -r requirements.txt
```

New package: `cryptography` (encrypts Splunk LDAP passwords at rest).

### 2. Set `ARGUS_CREDENTIALS_KEY` in `.env` (one-time, critical)

Splunk passwords are encrypted in `data/.splunk_vault/`. The server needs a master key.

**If upgrading an existing deployment**, add this to `.env` only if missing:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output into `.env`:

```
ARGUS_CREDENTIALS_KEY=<paste-generated-key-here>
```

**Important:**
- Generate once per server. Do not change after users have saved Splunk credentials (existing vault entries become unreadable).
- Never commit this key to git.
- Keep a secure backup of this key with your ops team.

Or re-run `bash deploy/setup_server.sh` — it auto-adds the key if missing.

### 3. Create vault directory (optional — app auto-creates on first save)

```bash
mkdir -p data/.splunk_vault
chmod 700 data/.splunk_vault
```

### 4. Remove old global Splunk credentials from `.env`

Delete these lines if present (no longer used):

```
SPLUNK_USERNAME=...
SPLUNK_PASSWORD=...
```

Splunk auth is now **per-user** via the dashboard.

### 5. Restart Streamlit workers

```bash
# Stop existing screen/session, then:
source .env && NUM_WORKERS=3 bash deploy/start_server.sh
```

Ensure nginx is still running with `deploy/nginx.conf` (sticky sessions required).

### 6. Verify persistent data paths

On Eris, `.env` should point at the persistent volume:

```
ARGUS_DATA_DIR=/opt/argus/data
ARGUS_REPOS_DIR=/opt/argus/repos
```

The vault lives at `$ARGUS_DATA_DIR/.splunk_vault/`.

---

## What end users do (not the deployer)

1. Open Argus in the browser.
2. Go to **Repo Settings → Customer Information**.
3. Enter **LDAP ID** (name before `@adobe.com`, e.g. `vanssharma`) and **LDAP password**.
4. Click **Save & Test Connection**.
5. Add customers in **Add / Edit Customer** as before.

Credentials persist until the user clicks **Clear Splunk credentials**.

---

## What was removed / changed

| Before | After |
|--------|-------|
| `SPLUNK_USERNAME` / `SPLUNK_PASSWORD` in `.env` | Per-user LDAP in UI |
| Shared Splunk account for all users | Each user uses their own Adobe LDAP |
| Plaintext Splunk creds in env | Fernet-encrypted in `data/.splunk_vault/` |

Git passwords are unchanged — still in `data/.secrets.json` per customer.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "ARGUS_CREDENTIALS_KEY is not set" | Add key to `.env`, restart app |
| "Splunk authentication failed" | User checks LDAP ID (no @adobe.com) and password |
| Live Splunk works but stops after key rotation | Restore old `ARGUS_CREDENTIALS_KEY` or users re-save passwords |
| Stale pipeline data | User clears cache via sidebar Refresh, or delete `data/cache/splunk_cache_{program_id}.pkl` |
