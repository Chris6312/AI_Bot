import requests
import asyncio
from typing import Dict, Optional, List
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class TradierClient:
    def __init__(self):
        self.api_key = settings.TRADIER_API_KEY
        self.account_id = settings.TRADIER_ACCOUNT_ID
        self.base_url = settings.TRADIER_BASE_URL
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Accept': 'application/json'
        }
    
    def get_account_sync(self) -> Dict:
        """Get account information (sync)"""
        url = f"{self.base_url}/accounts/{self.account_id}/balances"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()
    
    async def get_account_async(self) -> Dict:
        """Get account information (async)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_account_sync)
    
    def get_quote_sync(self, ticker: str) -> Dict:
        """Get quote for ticker (sync)"""
        url = f"{self.base_url}/markets/quotes"
        params = {'symbols': ticker}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()
        quotes = data.get('quotes', {}).get('quote', {})
        return quotes if isinstance(quotes, dict) else quotes[0] if quotes else {}
    
    async def get_quote_async(self, ticker: str) -> Dict:
        """Get quote for ticker (async)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_quote_sync, ticker)
    
    def place_order_sync(self, ticker: str, qty: int, side: str) -> Dict:
        """Place market order (sync)"""
        url = f"{self.base_url}/accounts/{self.account_id}/orders"
        data = {
            'class': 'equity',
            'symbol': ticker,
            'side': side,
            'quantity': qty,
            'type': 'market',
            'duration': 'day'
        }
        response = requests.post(url, headers=self.headers, data=data)
        response.raise_for_status()
        logger.info(f"Order placed: {side} {qty} {ticker}")
        return response.json()
    
    async def place_order_async(self, ticker: str, qty: int, side: str) -> Dict:
        """Place market order (async)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.place_order_sync, ticker, qty, side)
    
    def get_positions_sync(self) -> List[Dict]:
        """Get open positions (sync)"""
        url = f"{self.base_url}/accounts/{self.account_id}/positions"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        positions = data.get('positions', {}).get('position', [])
        return positions if isinstance(positions, list) else [positions] if positions else []
    
    async def get_positions_async(self) -> List[Dict]:
        """Get open positions (async)"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_positions_sync)
