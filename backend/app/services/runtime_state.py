from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Literal

from app.core.config import settings

TradingMode = Literal['PAPER', 'LIVE']
CryptoMode = Literal['PAPER']

logger = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    running: bool = True
    stock_mode: TradingMode = 'PAPER'
    crypto_mode: CryptoMode = 'PAPER'
    safety_require_market_hours: bool = True
    last_heartbeat: str = ''


class RuntimeStateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _default(self) -> RuntimeState:
        default_stock_mode = settings.BOT_DEFAULT_STOCK_MODE.upper()
        if default_stock_mode not in {'PAPER', 'LIVE'}:
            default_stock_mode = 'PAPER'
        if default_stock_mode == 'LIVE' and not settings.tradier_live_ready:
            default_stock_mode = 'PAPER'
        if default_stock_mode == 'PAPER' and not settings.tradier_paper_ready and settings.tradier_live_ready:
            default_stock_mode = 'LIVE'

        state = RuntimeState(
            running=True,
            stock_mode=default_stock_mode,  # type: ignore[arg-type]
            crypto_mode='PAPER',
            safety_require_market_hours=settings.SAFETY_REQUIRE_MARKET_HOURS,
            last_heartbeat=self._now_iso(),
        )
        return state

    def _load(self) -> RuntimeState:
        if not self.path.exists():
            state = self._default()
            self._save(state)
            return state
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except Exception:
            state = self._default()
            self._save(state)
            return state

        state = self._default()
        state.running = bool(payload.get('running', state.running))
        stock_mode = str(payload.get('stock_mode', state.stock_mode)).upper()
        if stock_mode in {'PAPER', 'LIVE'}:
            state.stock_mode = stock_mode  # type: ignore[assignment]
        state.safety_require_market_hours = bool(
            payload.get('safety_require_market_hours', state.safety_require_market_hours)
        )
        state.last_heartbeat = str(payload.get('last_heartbeat', state.last_heartbeat))

        if state.stock_mode == 'LIVE' and not settings.tradier_live_ready and settings.tradier_paper_ready:
            state.stock_mode = 'PAPER'
        if state.stock_mode == 'PAPER' and not settings.tradier_paper_ready and settings.tradier_live_ready:
            state.stock_mode = 'LIVE'
        return state

    def _save(self, state: RuntimeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(state), indent=2)
        try:
            self.path.write_text(payload, encoding='utf-8')
            return
        except OSError as exc:
            fallback_path = Path(tempfile.gettempdir()) / 'ai_bot_runtime_state.json'
            logger.warning('Runtime state write failed at %s, falling back to %s: %s', self.path, fallback_path, exc)
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            fallback_path.write_text(payload, encoding='utf-8')
            self.path = fallback_path

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get(self) -> RuntimeState:
        with self._lock:
            return RuntimeState(**asdict(self._state))

    def touch(self) -> RuntimeState:
        with self._lock:
            self._state.last_heartbeat = self._now_iso()
            self._save(self._state)
            return RuntimeState(**asdict(self._state))

    def set_running(self, enabled: bool) -> RuntimeState:
        with self._lock:
            self._state.running = enabled
            self._state.last_heartbeat = self._now_iso()
            self._save(self._state)
            return RuntimeState(**asdict(self._state))

    def set_stock_mode(self, mode: TradingMode) -> RuntimeState:
        if mode == 'LIVE' and not settings.tradier_live_ready:
            raise ValueError('Tradier live credentials are not configured.')
        if mode == 'PAPER' and not settings.tradier_paper_ready:
            raise ValueError('Tradier paper credentials are not configured.')
        with self._lock:
            self._state.stock_mode = mode
            self._state.last_heartbeat = self._now_iso()
            self._save(self._state)
            return RuntimeState(**asdict(self._state))

    def set_safety_require_market_hours(self, enabled: bool) -> RuntimeState:
        with self._lock:
            self._state.safety_require_market_hours = enabled
            self._state.last_heartbeat = self._now_iso()
            self._save(self._state)
            return RuntimeState(**asdict(self._state))


runtime_state = RuntimeStateStore(settings.runtime_state_path)
