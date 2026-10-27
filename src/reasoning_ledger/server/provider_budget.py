"""Read-only, coalesced OpenRouter daily-key spending; never request admission."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .openrouter import ProviderError, microusd
from .provider_keys import active_key


def daily_spend(data):
    """Project only money and the daily reset; provider identity/credential fields stay private."""
    spent = microusd(data["usage_daily"])
    if "include_byok_in_limit" in data and type(data["include_byok_in_limit"]) is not bool:
        raise ProviderError("key_status_unavailable")
    if data.get("include_byok_in_limit") is True:
        spent += microusd(data["byok_usage_daily"])
    if data.get("limit_reset") != "daily" or data.get("limit") is None:
        return {"status": "no_daily_limit", "spent_microusd": spent, "limit_microusd": None, "remaining_microusd": None}
    limit = microusd(data["limit"])
    # Provider remaining credits can be negative after an in-flight overrun.
    remaining = microusd(max(Decimal(0), Decimal(str(data["limit_remaining"]))))
    if remaining > limit:
        raise ProviderError("key_status_unavailable")
    return {"status": "ready", "spent_microusd": spent, "limit_microusd": limit, "remaining_microusd": remaining}


class ProviderBudget:
    def __init__(self, store, provider, *, clock=time.monotonic, now=time.time):
        self.store, self.provider, self.clock, self.now = store, provider, clock, now
        self.cache, self.locks = {}, {}

    def _selection(self, user, organization):
        with self.store.transaction() as db:
            self.store._require(db, user, organization)
            row = active_key(db, organization)
            return dict(row) if row else None

    @staticmethod
    def unavailable(status="unavailable"):
        return {"status": status, "spent_microusd": None, "limit_microusd": None,
                "remaining_microusd": None, "checked_at": None, "resets_at": None}

    async def read(self, user, organization):
        self._selection(user, organization)  # Authorize before allocating an organization lock.
        async with self.locks.setdefault(organization, asyncio.Lock()):
            selected = self._selection(user, organization)
            if selected is None:
                return self.unavailable("no_key")
            now = datetime.fromtimestamp(self.now(), UTC)
            identity = (selected["id"], now.date())
            cached = self.cache.get(organization)
            if cached and cached[0] == identity and cached[1] > self.clock():
                return dict(cached[2])
            key = self.store._box.open(selected["ciphertext"], organization_id=organization,
                                       provider="openrouter", key_id=selected["id"])
            try:
                data = await self.provider.key_usage(key)
                result = daily_spend(data)
                observed = datetime.fromtimestamp(self.now(), UTC)
                if observed.date() != now.date():
                    raise ProviderError("key_status_unavailable")
                result.update(checked_at=int(observed.timestamp()), resets_at=int(
                    (observed.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()))
            except (ProviderError, KeyError, TypeError, ValueError, ArithmeticError):
                result = self.unavailable()
            # A revoked membership or a rotation during the lookup cannot publish old-key metadata.
            latest = self._selection(user, organization)
            if latest is None or latest["id"] != selected["id"]:
                return self.unavailable()
            ttl = 5 if result["status"] == "unavailable" else 30
            self.cache[organization] = (identity, self.clock() + ttl, result)
            return dict(result)
