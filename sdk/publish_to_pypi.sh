#!/usr/bin/env bash
# ==============================================================================
# Brenducate SDK — PyPI Release Script
# ==============================================================================
set -e

echo "📦 Building brenducate-sdk package distribution..."
cd "$(dirname "$0")"

rm -rf dist/ build/ *.egg-info

python3 -m pip install --upgrade build twine
python3 -m build

echo ""
echo "✅ Build complete. Distribution files created in sdk/dist/:"
ls -lh dist/

echo ""
echo "To publish to PyPI:"
echo "  python3 -m twine upload dist/*"
echo ""
echo "When prompted for credentials:"
echo "  Username: __token__"
echo "  Password: <paste your pypi-... token>"
