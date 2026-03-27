#!/bin/bash
cd "$(dirname "$0")"

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
  echo "Setting up virtual environment..."
  if ! python3 -m venv venv 2>/dev/null; then
    echo "python3-venv not found. Installing it now (requires sudo)..."
    sudo apt install -y python3-venv
    python3 -m venv venv
  fi
  venv/bin/pip install -q -r requirements.txt
  echo "Done."
fi

echo "Starting Budget Tracker at http://localhost:5000"
venv/bin/python app.py
