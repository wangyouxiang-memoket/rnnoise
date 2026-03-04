#!/bin/bash
set -e

echo "=========================================="
echo "RNNoise Test Suite"
echo "=========================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if server is running
echo "Checking server status..."
if docker ps | grep -q rnnoise-server; then
    echo -e "${GREEN}✓${NC} Server is running"
else
    echo -e "${YELLOW}⚠${NC}  Server not running. Start with:"
    echo "   docker run -d --name rnnoise-server -p 8005:8005 rnnoise:local"
    echo ""
fi

# Run tests inside Docker container
echo ""
echo "=========================================="
echo "Running Audio Conversion Tests"
echo "=========================================="
docker exec rnnoise-server python3 /app/server/test_audio_conversion.py
AUDIO_TEST_RESULT=$?

echo ""
echo "=========================================="
echo "Running API Tests"
echo "=========================================="
docker exec rnnoise-server python3 /app/server/test_api.py
API_TEST_RESULT=$?

# Summary
echo ""
echo "=========================================="
echo "FINAL SUMMARY"
echo "=========================================="

if [ $AUDIO_TEST_RESULT -eq 0 ]; then
    echo -e "${GREEN}✓ PASS${NC} - Audio Conversion Tests"
else
    echo -e "${RED}✗ FAIL${NC} - Audio Conversion Tests"
fi

if [ $API_TEST_RESULT -eq 0 ]; then
    echo -e "${GREEN}✓ PASS${NC} - API Tests"
else
    echo -e "${RED}✗ FAIL${NC} - API Tests"
fi

echo ""

if [ $AUDIO_TEST_RESULT -eq 0 ] && [ $API_TEST_RESULT -eq 0 ]; then
    echo -e "${GREEN}🎉 All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}⚠️  Some tests failed${NC}"
    exit 1
fi
