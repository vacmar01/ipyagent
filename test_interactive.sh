#!/bin/bash
# Test script to run ipycodex with pi backend interactively
# Uses dev version from this folder

cd "$(dirname "$0")"

# Activate the dev venv
source .venv/bin/activate

# Set the provider and model for testing
export IPYAI_PROVIDER=openai-codex
export IPYAI_MODEL=gpt-5.3-codex

# Ensure config has the provider
echo "Checking config..."
mkdir -p ~/.config/ipycodex
cat >~/.config/ipycodex/config.json <<'EOF'
{
  "model": "kimi-k2.5",
  "provider": "opencode-go",
  "completion_model": "gpt-5.4-mini",
  "think": "l",
  "search": "l",
  "code_theme": "monokai",
  "log_exact": false,
  "prompt_mode": false
}
EOF

echo ""
echo "Starting ipycodex with pi backend (dev version)..."
echo "Python: $(which python)"
echo ""

# Run ipycodex from this venv (installed editable from local repo)
echo "ipycodex: $(which ipycodex)"
ipycodex "$@"
