#!/bin/bash
# Linux/Mac: create venv, install CPU-only PyTorch (small download), then the rest
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
echo "Done. Start the server with ./run.sh"
