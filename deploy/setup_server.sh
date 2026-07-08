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
mkdir -p data/predictions data/cache/assessments data/cache/logs reports

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
    cat > .env << 'ENV'
# Splunk
SPLUNK_USERNAME=your_splunk_username
SPLUNK_PASSWORD=your_splunk_password
SPLUNK_EARLIEST=-30d

# Azure OpenAI (LLM)
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_KEY=your_key
AZURE_OPENAI_DEPLOYMENT=gpt-4.1-nano
LLM_PROVIDER=azure_openai

# Azure File Share (for logs)
AZURE_STORAGE_ACCOUNT=your_account
AZURE_STORAGE_KEY=your_key

# Where git repos are cloned on this server
# Developers add customers via the UI — repos clone here automatically
REPOS_BASE_DIR=/opt/repos
ENV
    echo "  → Created .env template. Fill in your credentials."
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
echo "[6/6] Open the app → go to 'Repo Settings' → add your first customer."
