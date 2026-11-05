#!/usr/bin/env bash
# ==============================================================================
# AgentPulse SDK — PyPI Release Script
# ==============================================================================
set -e

echo "📦 Building agentpulse-sdk package distribution..."
cd "$(dirname "$0")"

rm -rf dist/ build/ *.egg-info

pip install --upgrade build twine
python3 -m build

echo ""
echo "✅ Build complete. Distribution files created in sdk/dist/:"
ls -lh dist/

echo ""
echo "To publish to PyPI:"
echo "  python3 -m twine upload dist/*"
echo ""
echo "Or to test on TestPyPI first:"
echo "  python3 -m twine upload --repository testpypi dist/*"

