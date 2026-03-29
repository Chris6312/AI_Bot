"""
Crypto Technical Analysis Service
Calculates RSI, MACD, Bollinger Bands, volume analysis, and other indicators from Kraken CLI data
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import BollingerBands
from ta.volume import VolumeWeightedAveragePrice

from app.services.kraken_service import kraken_service, TOP_15_PAIRS

logger = logging.getLogger(__name__)


class CryptoAnalyzer:
    """
    Advanced technical analysis for crypto pairs using Kraken CLI data
    
    Provides:
    - RSI (Relative Strength Index)
    - MACD (Moving Average Convergence Divergence)
    - Bollinger Bands
    - Volume analysis
    - 24h price changes
    - Momentum screening
    """
    
    def __init__(self):
        self.kraken = kraken_service
        
        # Default indicator periods
        self.rsi_period = 14
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9
        self.bb_period = 20
        self.bb_std = 2
    
    def _get_candles_df(self, pair: str, interval: int = 5, limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get OHLC candles as pandas DataFrame
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
            interval: Candle interval in minutes
            limit: Number of candles
        
        Returns:
            DataFrame with OHLC data or None if error
        """
        ohlcv_pair = TOP_15_PAIRS.get(pair)
        if not ohlcv_pair:
            logger.error(f"Unknown pair: {pair}")
            return None
        
        candles = self.kraken.get_ohlc(ohlcv_pair, interval=interval, limit=limit)
        
        if not candles or len(candles) < 20:  # Need minimum data
            logger.error(f"Insufficient data for {pair}: {len(candles) if candles else 0} candles")
            return None
        
        # Convert to DataFrame
        df = pd.DataFrame(candles)
        
        # Ensure numeric types
        df['open'] = pd.to_numeric(df['open'])
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        df['close'] = pd.to_numeric(df['close'])
        df['volume'] = pd.to_numeric(df['volume'])
        
        return df
    
    def calculate_rsi(self, pair: str, period: int = None, interval: int = 5) -> Optional[float]:
        """
        Calculate RSI (Relative Strength Index)
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
            period: RSI period (default 14)
            interval: Candle interval in minutes
        
        Returns:
            Current RSI value (0-100) or None if error
        """
        if period is None:
            period = self.rsi_period
        
        # Need extra candles for calculation
        limit = period * 3
        df = self._get_candles_df(pair, interval=interval, limit=limit)
        
        if df is None or len(df) < period:
            return None
        
        # Calculate RSI
        rsi_indicator = RSIIndicator(close=df['close'], window=period)
        rsi_values = rsi_indicator.rsi()
        
        # Return most recent RSI
        current_rsi = rsi_values.iloc[-1]
        
        return float(current_rsi) if not pd.isna(current_rsi) else None
    
    def calculate_macd(self, pair: str, interval: int = 5) -> Optional[Dict]:
        """
        Calculate MACD (Moving Average Convergence Divergence)
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
            interval: Candle interval in minutes
        
        Returns:
            Dict with MACD line, signal line, histogram, and signal
        """
        # Need enough candles for MACD calculation
        limit = max(self.macd_slow, 26) * 3  # 3x buffer
        df = self._get_candles_df(pair, interval=interval, limit=limit)
        
        if df is None or len(df) < self.macd_slow:
            return None
        
        # Calculate MACD
        macd_indicator = MACD(
            close=df['close'],
            window_fast=self.macd_fast,
            window_slow=self.macd_slow,
            window_sign=self.macd_signal
        )
        
        macd_line = macd_indicator.macd()
        signal_line = macd_indicator.macd_signal()
        histogram = macd_indicator.macd_diff()
        
        # Get current values
        current_macd = macd_line.iloc[-1]
        current_signal = signal_line.iloc[-1]
        current_histogram = histogram.iloc[-1]
        
        # Previous values for crossover detection
        prev_macd = macd_line.iloc[-2] if len(macd_line) > 1 else current_macd
        prev_signal = signal_line.iloc[-2] if len(signal_line) > 1 else current_signal
        
        # Detect crossovers
        bullish_crossover = prev_macd <= prev_signal and current_macd > current_signal
        bearish_crossover = prev_macd >= prev_signal and current_macd < current_signal
        
        # Determine signal
        if bullish_crossover:
            signal = "BULLISH_CROSS"
        elif bearish_crossover:
            signal = "BEARISH_CROSS"
        elif current_macd > current_signal:
            signal = "BULLISH"
        elif current_macd < current_signal:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"
        
        return {
            'macd': float(current_macd) if not pd.isna(current_macd) else 0,
            'signal': float(current_signal) if not pd.isna(current_signal) else 0,
            'histogram': float(current_histogram) if not pd.isna(current_histogram) else 0,
            'crossover': signal,
            'bullish_crossover': bullish_crossover,
            'bearish_crossover': bearish_crossover
        }
    
    def calculate_bollinger_bands(self, pair: str, period: int = None, std_dev: int = None, interval: int = 5) -> Optional[Dict]:
        """
        Calculate Bollinger Bands
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
            period: BB period (default 20)
            std_dev: Standard deviations (default 2)
            interval: Candle interval in minutes
        
        Returns:
            Dict with upper, middle, lower bands, %B, and bandwidth
        """
        if period is None:
            period = self.bb_period
        if std_dev is None:
            std_dev = self.bb_std
        
        limit = period * 3
        df = self._get_candles_df(pair, interval=interval, limit=limit)
        
        if df is None or len(df) < period:
            return None
        
        # Calculate Bollinger Bands
        bb = BollingerBands(
            close=df['close'],
            window=period,
            window_dev=std_dev
        )
        
        # Get band values
        upper_band = bb.bollinger_hband()
        middle_band = bb.bollinger_mavg()
        lower_band = bb.bollinger_lband()
        
        # Current values
        current_price = df['close'].iloc[-1]
        current_upper = upper_band.iloc[-1]
        current_middle = middle_band.iloc[-1]
        current_lower = lower_band.iloc[-1]
        
        # Calculate %B (position within bands)
        # %B = (Price - Lower) / (Upper - Lower)
        # %B > 1: Above upper band
        # %B = 0.5: At middle band
        # %B < 0: Below lower band
        band_width = current_upper - current_lower
        if band_width > 0:
            percent_b = (current_price - current_lower) / band_width
        else:
            percent_b = 0.5
        
        # Calculate Bandwidth (volatility indicator)
        # Bandwidth = (Upper - Lower) / Middle
        if current_middle > 0:
            bandwidth = band_width / current_middle
        else:
            bandwidth = 0
        
        # Determine position
        if percent_b > 1:
            position = "ABOVE_UPPER"
        elif percent_b > 0.8:
            position = "NEAR_UPPER"
        elif percent_b > 0.5:
            position = "UPPER_HALF"
        elif percent_b > 0.2:
            position = "LOWER_HALF"
        elif percent_b > 0:
            position = "NEAR_LOWER"
        else:
            position = "BELOW_LOWER"
        
        return {
            'upper_band': float(current_upper),
            'middle_band': float(current_middle),
            'lower_band': float(current_lower),
            'current_price': float(current_price),
            'percent_b': float(percent_b),
            'bandwidth': float(bandwidth),
            'position': position,
            'squeeze': bandwidth < 0.1,  # Bollinger Squeeze (low volatility)
            'overbought': percent_b > 1,  # Price above upper band
            'oversold': percent_b < 0  # Price below lower band
        }
    
    def calculate_volume_ratio(self, pair: str, lookback_periods: int = 20, interval: int = 5) -> Optional[float]:
        """
        Calculate current volume vs average volume ratio
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
            lookback_periods: Number of periods to average
            interval: Candle interval in minutes
        
        Returns:
            Volume ratio (current / average) or None if error
        """
        df = self._get_candles_df(pair, interval=interval, limit=lookback_periods + 1)
        
        if df is None or len(df) < 2:
            return None
        
        # Current volume (most recent candle)
        current_volume = df['volume'].iloc[-1]
        
        # Average volume (excluding current)
        avg_volume = df['volume'].iloc[:-1].mean()
        
        if avg_volume == 0:
            return None
        
        ratio = current_volume / avg_volume
        
        return float(ratio)
    
    def get_24h_change(self, pair: str) -> Optional[float]:
        """
        Calculate 24-hour price change percentage
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
        
        Returns:
            24h change percentage or None
        """
        ohlcv_pair = TOP_15_PAIRS.get(pair)
        if not ohlcv_pair:
            return None
        
        ticker = self.kraken.get_ticker(ohlcv_pair)
        
        if not ticker or 'c' not in ticker:
            return None
        
        current_price = float(ticker['c'][0])
        
        # Get opening price (24h ago = 288 5-min candles)
        df = self._get_candles_df(pair, interval=5, limit=288)
        
        if df is None or len(df) == 0:
            return None
        
        opening_price = df['open'].iloc[0]
        
        if opening_price == 0:
            return None
        
        change_pct = ((current_price - opening_price) / opening_price) * 100
        
        return float(change_pct)
    
    def analyze_pair(self, pair: str, interval: int = 5) -> Dict:
        """
        Complete technical analysis for a pair
        
        Args:
            pair: Display format (e.g., 'BTC/USD')
            interval: Candle interval in minutes
        
        Returns:
            Dict with all indicators and signals
        """
        ohlcv_pair = TOP_15_PAIRS.get(pair)
        
        # Get current price
        ticker = self.kraken.get_ticker(ohlcv_pair)
        current_price = float(ticker['c'][0]) if ticker and 'c' in ticker else 0
        
        # Calculate all indicators
        rsi = self.calculate_rsi(pair, interval=interval)
        macd = self.calculate_macd(pair, interval=interval)
        bb = self.calculate_bollinger_bands(pair, interval=interval)
        volume_ratio = self.calculate_volume_ratio(pair, interval=interval)
        change_24h = self.get_24h_change(pair)
        
        # Determine overall signals
        signals = []
        score = 0  # Composite score (-5 to +5)
        
        # RSI signals
        if rsi is not None:
            if rsi > 70:
                signals.append("RSI Overbought")
                score -= 1
            elif rsi < 30:
                signals.append("RSI Oversold")
                score += 1
            elif 50 <= rsi <= 70:
                signals.append("RSI Momentum")
                score += 1
        
        # MACD signals
        if macd:
            if macd['bullish_crossover']:
                signals.append("MACD Bullish Cross")
                score += 2
            elif macd['bearish_crossover']:
                signals.append("MACD Bearish Cross")
                score -= 2
            elif macd['crossover'] == "BULLISH":
                score += 0.5
            elif macd['crossover'] == "BEARISH":
                score -= 0.5
        
        # Bollinger Bands signals
        if bb:
            if bb['oversold']:
                signals.append("BB Oversold")
                score += 1
            elif bb['overbought']:
                signals.append("BB Overbought")
                score -= 1
            
            if bb['squeeze']:
                signals.append("BB Squeeze (Breakout Coming)")
        
        # Volume signals
        if volume_ratio and volume_ratio > 2.0:
            signals.append("Volume Spike")
            score += 1
        
        # 24h change signals
        if change_24h:
            if change_24h > 5:
                signals.append("Strong 24h Gain")
                score += 1
            elif change_24h < -5:
                signals.append("Strong 24h Loss")
                score -= 1
        
        # Overall recommendation
        if score >= 3:
            recommendation = "STRONG_BUY"
        elif score >= 1.5:
            recommendation = "BUY"
        elif score <= -3:
            recommendation = "STRONG_SELL"
        elif score <= -1.5:
            recommendation = "SELL"
        else:
            recommendation = "NEUTRAL"
        
        return {
            'pair': pair,
            'price': current_price,
            'change_24h': change_24h,
            
            # RSI
            'rsi': rsi,
            'rsi_overbought': rsi > 70 if rsi else False,
            'rsi_oversold': rsi < 30 if rsi else False,
            'rsi_momentum': 50 <= rsi <= 70 if rsi else False,
            
            # MACD
            'macd': macd,
            'macd_bullish': macd['crossover'] in ['BULLISH', 'BULLISH_CROSS'] if macd else False,
            'macd_bearish': macd['crossover'] in ['BEARISH', 'BEARISH_CROSS'] if macd else False,
            
            # Bollinger Bands
            'bollinger': bb,
            'bb_overbought': bb['overbought'] if bb else False,
            'bb_oversold': bb['oversold'] if bb else False,
            'bb_squeeze': bb['squeeze'] if bb else False,
            
            # Volume
            'volume_ratio': volume_ratio,
            'volume_spike': volume_ratio > 2.0 if volume_ratio else False,
            
            # Composite signals
            'signals': signals,
            'score': score,
            'recommendation': recommendation
        }
    
    def screen_for_momentum(
        self,
        min_change_24h: float = 5.0,
        min_volume_ratio: float = 1.5,
        rsi_min: float = 50,
        rsi_max: float = 70,
        require_macd_bullish: bool = False,
        require_bb_position: Optional[str] = None  # e.g., "LOWER_HALF"
    ) -> List[Dict]:
        """
        Screen all top 15 pairs for momentum signals
        
        Args:
            min_change_24h: Minimum 24h price change %
            min_volume_ratio: Minimum volume vs average
            rsi_min: Minimum RSI
            rsi_max: Maximum RSI
            require_macd_bullish: Require MACD bullish signal
            require_bb_position: Require specific BB position
        
        Returns:
            List of pairs with momentum, sorted by composite score
        """
        results = []
        
        for pair in TOP_15_PAIRS.keys():
            logger.info(f"Analyzing {pair}...")
            
            try:
                analysis = self.analyze_pair(pair)
                
                # Check required data
                if analysis['rsi'] is None or analysis['volume_ratio'] is None:
                    logger.warning(f"{pair}: Missing data, skipping")
                    continue
                
                # Apply filters
                passes_filters = True
                
                # 24h change filter
                if analysis['change_24h'] is None or analysis['change_24h'] < min_change_24h:
                    passes_filters = False
                
                # Volume filter
                if analysis['volume_ratio'] < min_volume_ratio:
                    passes_filters = False
                
                # RSI filter
                if not (rsi_min <= analysis['rsi'] <= rsi_max):
                    passes_filters = False
                
                # MACD filter
                if require_macd_bullish and not analysis['macd_bullish']:
                    passes_filters = False
                
                # Bollinger Bands position filter
                if require_bb_position and analysis['bollinger']:
                    if analysis['bollinger']['position'] != require_bb_position:
                        passes_filters = False
                
                if passes_filters:
                    results.append(analysis)
                    logger.info(f"✅ {pair} passed filters - Score: {analysis['score']:.1f}")
            
            except Exception as e:
                logger.error(f"Error analyzing {pair}: {e}", exc_info=True)
                continue
        
        # Sort by composite score (highest first)
        results.sort(key=lambda x: x['score'], reverse=True)
        
        return results
    
    def get_screening_summary(self, results: List[Dict]) -> str:
        """Format screening results for Discord display"""
        
        if not results:
            return "**No pairs meet momentum criteria.**"
        
        lines = [f"**🔍 Momentum Signals Detected ({len(results)} pairs)**\n"]
        
        for i, r in enumerate(results, 1):
            emoji = "🟢" if r['change_24h'] > 0 else "🔴"
            
            # Build signal indicators
            indicators = []
            if r['rsi_momentum']:
                indicators.append("RSI✓")
            if r['macd_bullish']:
                indicators.append("MACD✓")
            if r['volume_spike']:
                indicators.append("VOL✓")
            if r['bollinger'] and r['bollinger'].get('position') in ['LOWER_HALF', 'NEAR_LOWER']:
                indicators.append("BB✓")
            
            indicator_str = " ".join(indicators) if indicators else "No signals"
            
            lines.append(
                f"{emoji} **{i}. {r['pair']}** - ${r['price']:,.2f}\n"
                f"  • 24h: **{r['change_24h']:+.2f}%**\n"
                f"  • RSI: {r['rsi']:.1f} | Vol: {r['volume_ratio']:.1f}x\n"
                f"  • Score: {r['score']:.1f} | {r['recommendation']}\n"
                f"  • Signals: {indicator_str}\n"
            )
        
        return "\n".join(lines)
    
    def get_detailed_analysis(self, analysis: Dict) -> str:
        """Format detailed analysis for Discord display"""
        
        emoji = "🟢" if analysis.get('change_24h', 0) > 0 else "🔴"
        
        # Build detailed response
        response = f"{emoji} **{analysis['pair']} - Detailed Analysis**\n\n"
        
        # Price & 24h Change
        response += f"**Price:** ${analysis['price']:,.2f}\n"
        response += f"**24h Change:** {analysis.get('change_24h', 0):+.2f}%\n\n"
        
        # RSI
        if analysis['rsi']:
            rsi_status = "Overbought" if analysis['rsi'] > 70 else "Oversold" if analysis['rsi'] < 30 else "Neutral"
            response += f"**RSI (14):** {analysis['rsi']:.1f} - {rsi_status}\n"
        
        # MACD
        if analysis['macd']:
            macd = analysis['macd']
            response += f"**MACD:** {macd['macd']:.4f}\n"
            response += f"**Signal:** {macd['signal']:.4f}\n"
            response += f"**Histogram:** {macd['histogram']:.4f}\n"
            response += f"**Status:** {macd['crossover']}\n"
        
        response += "\n"
        
        # Bollinger Bands
        if analysis['bollinger']:
            bb = analysis['bollinger']
            response += f"**Bollinger Bands:**\n"
            response += f"  Upper: ${bb['upper_band']:,.2f}\n"
            response += f"  Middle: ${bb['middle_band']:,.2f}\n"
            response += f"  Lower: ${bb['lower_band']:,.2f}\n"
            response += f"  %B: {bb['percent_b']:.2f} ({bb['position']})\n"
            if bb['squeeze']:
                response += f"  ⚠️ **Squeeze detected - breakout imminent**\n"
        
        response += "\n"
        
        # Volume
        if analysis['volume_ratio']:
            response += f"**Volume:** {analysis['volume_ratio']:.1f}x average"
            if analysis['volume_spike']:
                response += " 🔥 **SPIKE**"
            response += "\n\n"
        
        # Signals
        if analysis['signals']:
            response += f"**Active Signals:**\n"
            for signal in analysis['signals']:
                response += f"  • {signal}\n"
            response += "\n"
        
        # Recommendation
        rec_emoji = {
            'STRONG_BUY': '🟢🟢',
            'BUY': '🟢',
            'NEUTRAL': '⚪',
            'SELL': '🔴',
            'STRONG_SELL': '🔴🔴'
        }
        
        response += f"**Composite Score:** {analysis['score']:.1f}/5\n"
        response += f"**Recommendation:** {rec_emoji.get(analysis['recommendation'], '⚪')} **{analysis['recommendation']}**"
        
        return response


# Global instance
crypto_analyzer = CryptoAnalyzer()