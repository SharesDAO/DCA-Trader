#!/bin/bash
# Liquidation and withdrawal script
# This script will:
# 1. Liquidate all positions (sell all stocks)
# 2. Wait for confirmations
# 3. Sweep all USDC back to vault

set -e

echo "=================================="
echo "DCA Bot Liquidation & Withdrawal"
echo "=================================="
echo ""

# Step 1: Liquidate all positions
echo "Step 1: Liquidating all positions..."
python -m src.main --liquidate

if [ $? -ne 0 ]; then
    echo "❌ Liquidation failed"
    exit 1
fi

echo ""
echo "✅ Liquidation orders placed"
echo ""
echo "⏳ Waiting for sell orders to confirm..."
echo "   You need to wait for the blockchain to process the orders."
echo "   This typically takes a few minutes."
echo ""
read -p "Press Enter once all sell orders are confirmed (check bot logs)..."

# Step 2: Sweep all USDC to vault
echo ""
echo "Step 2: Sweeping USDC to vault..."
python -m src.main --sweep

if [ $? -ne 0 ]; then
    echo "❌ Sweep failed"
    exit 1
fi

echo ""
echo "=================================="
echo "✅ Liquidation Complete!"
echo "=================================="
echo "All funds have been returned to the vault."
echo ""
