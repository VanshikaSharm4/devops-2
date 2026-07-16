#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# DevOps Intelligence Platform — Server Setup Script
# Run once on the Adobe internal server
# ─────────────────────────────────────────────────────────────────────────────
set -e

APP_DIR="/opt/devops-agent"
PYTHON="python3"
PORT=8501

echo "=== DevOps Intelligence Platform — Server Setup ==="
echo ""

# 1. Clone or update the project
if [ ! -d "$APP_DIR/.git" ]; then
    echo "[1/6] Cloning project..."
    git clone <YOUR_INTERNAL_REPO_URL> "$APP_DIR"
else
    echo "[1/6] Updating project..."
    cd "$APP_DIR" && git pull
fi

cd "$APP_DIR"

# 2. Install Python dependencies
echo "[2/6] Installing dependencies..."
$PYTHON -m pip install -r requirements.txt --quiet

# 3. Create data directories
echo "[3/6] Creating data directories..."
mkdir -p data/predictions data/cache/assessments data/cache/logs data/.splunk_vault reports
chmod 700 data/.splunk_vault 2>/dev/null || true

# 4. Set up config files (copy examples if not present)
echo "[4/6] Setting up config..."
if [ ! -f "data/customer_config.json" ]; then
    echo '{}' > data/customer_config.json
fi
if [ ! -f "data/.secrets.json" ]; then
    echo '{}' > data/.secrets.json
fi

# 5. Copy .env template
if [ ! -f ".env" ]; then
    _CRED_KEY=$($PYTHON -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "")
    cat > .env << ENV
# Credential encryption — required for Splunk LDAP vault (data/.splunk_vault/)
ARGUS_CREDENTIALS_KEY=${_CRED_KEY}

# Splunk query window (optional)
SPLUNK_EARLIEST=-14d

# Azure OpenAI (LLM)
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_KEY=your_key
AZURE_OPENAI_DEPLOYMENT=gpt-4.1-nano
LLM_PROVIDER=azure_openai

# Azure File Share (for logs)
AZURE_STORAGE_ACCOUNT=your_account
AZURE_STORAGE_KEY=your_key

# Persistent data paths (Eris production)
ARGUS_DATA_DIR=/opt/argus/data
ARGUS_REPOS_DIR=/opt/argus/repos
REPOS_BASE_DIR=/opt/argus/repos
ENV
    echo "  → Created .env template. Fill in LLM and Azure credentials."
    echo "  → ARGUS_CREDENTIALS_KEY was auto-generated for Splunk vault encryption."
fi

# Ensure ARGUS_CREDENTIALS_KEY exists in .env
if [ -f ".env" ] && ! grep -q "^ARGUS_CREDENTIALS_KEY=.\+" .env 2>/dev/null; then
    _CRED_KEY=$($PYTHON -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "" >> .env
    echo "ARGUS_CREDENTIALS_KEY=${_CRED_KEY}" >> .env
    echo "  → Added ARGUS_CREDENTIALS_KEY to .env"
fi

mkdir -p "$APP_DIR/logs"

echo "[5/6] Setup complete."
echo ""
echo "=== For 1-3 users (development) ==="
echo "  cd $APP_DIR"
echo "  screen -S devops-agent"
echo "  source .env && streamlit run dashboard/app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true"
echo "  [Ctrl+A, D to detach]"
echo ""
echo "=== For 10-20 concurrent users (Eris production) ==="
echo "  # Install nginx first: apt-get install nginx"
echo "  cd $APP_DIR"
echo "  screen -S devops-agent"
echo "  source .env && NUM_WORKERS=3 bash deploy/start_server.sh"
echo "  [Ctrl+A, D to detach]"
echo "  # Then start nginx:"
echo "  sudo nginx -c $APP_DIR/deploy/nginx.conf"
echo ""
echo "=== Access URL ==="
echo "  http://$(hostname -I | awk '{print $1}'):$PORT"
echo ""
echo "[6/6] Open the app → Repo Settings → Customer Information (Splunk LDAP), then add customers."
