#!/bin/bash
# macOS: double-click to open File Sorter in your browser (runs offline).
cd "$(dirname "$0")" || exit 1
exec python3 file_sorter_app.py
