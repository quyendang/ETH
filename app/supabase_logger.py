from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class EthbotSupabaseLogger:
    def __init__(self, url: str | None, key: str | None) -> None:
        self._enabled = bool(url and key)
        self._client = None
        if self._enabled:
            try:
                from supabase import create_client
                self._client = create_client(url, key)
            except Exception as exc:
                logger.warning('Supabase init failed: %s', exc)
                self._enabled = False

    def is_enabled(self) -> bool:
        return self._enabled and self._client is not None

    def log_signal(
        self,
        symbol: str,
        action: str,
        price: float,
        support: float,
        resistance: float,
        reason: str = '',
        as_of: datetime | None = None,
    ) -> None:
        if not self.is_enabled():
            return
        now = datetime.now(timezone.utc)
        try:
            self._client.table('signals').insert({
                'symbol': symbol.upper(),
                'action': action,
                'confidence': 'medium',
                'buy_score': 0,
                'sell_score': 0,
                'price': price,
                'support': support,
                'resistance': resistance,
                'invalidation': 0.0,
                'sent_at': now.isoformat(),
                'as_of': (as_of or now).isoformat(),
                'bot_source': 'ethbot',
            }).execute()
        except Exception as exc:
            logger.warning('Supabase log_signal failed for %s: %s', symbol, exc)
