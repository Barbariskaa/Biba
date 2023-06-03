#!/bin/bash
python -m venv venv
source venv/bin/activate # Команда на случай если ругается на venv: sudo apt install python3.10-venv
pip install -r requirements.txt
python main.py
