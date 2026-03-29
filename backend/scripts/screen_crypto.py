#!/usr/bin/env python3
"""
Standalone crypto momentum screening script
Can be run directly or called from AI
"""

import sys
import os
import json

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.crypto_analyzer import crypto_analyzer


def main():
    """Run crypto momentum screening"""
    
    print("🔍 Screening Kraken top 30 pairs for momentum...\n")
    
    # Screen with configurable criteria
    results = crypto_analyzer.screen_for_momentum(
        min_change_24h=3.0,       # 3%+ gain
        min_volume_ratio=1.2,     # Lower volume threshold
        rsi_min=45,               # Wider RSI range
        rsi_max=75
    )
    
    if not results:
        print("❌ No momentum signals detected.")
        print("\nCriteria:")
        print("  • 24h gain: >3%")
        print("  • Volume: >1.5x average")
        print("  • RSI: 50-70")
        return
    
    # Print summary
    print(f"✅ Found {len(results)} momentum signals:\n")
    
    for i, r in enumerate(results, 1):
        print(f"{i}. {r['pair']}")
        print(f"   Price: ${r['price']:,.2f}")
        print(f"   24h Change: {r['change_24h']:+.2f}%")
        print(f"   RSI: {r['rsi']:.1f}")
        print(f"   Volume Ratio: {r['volume_ratio']:.1f}x")
        print()
    
    # Generate JSON for top 3
    top_3 = results[:3]
    candidates = [{"pair": r['pair']} for r in top_3]
    
    decision = {
        "type": "CRYPTO_SCREENING",
        "candidates": candidates,
        "reasoning": f"Top {len(top_3)} momentum plays with RSI 50-70, volume spike, >3% gain"
    }
    
    print("\n📋 JSON for Discord bot:\n")
    print(json.dumps(decision, indent=2))


if __name__ == "__main__":
    main()