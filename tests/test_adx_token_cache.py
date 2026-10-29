"""Offline checks for local ADX token reuse; no CLI or cloud requests."""

from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from unittest.mock import Mock, patch

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.data._token_providers import AzureIdentityTokenCredentialProvider

# Import without constructing a configured global ADX client.
with patch.dict(os.environ, {"ADX_CLUSTER_URI": "", "ADX_QUERY_URI": "", "ADX_DATABASE": ""}), \
     patch("logging.error"):
    from chatbot import adx_client as adx


class AdxTokenCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.clock = patch.object(adx.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.token = AccessToken("offline-placeholder", 5000)
        self.source = Mock()
        self.source.get_token.return_value = self.token
        self.cache = adx._CachedTokenCredential(self.source)

    def test_1000_requests_reuse_one_token(self):
        for _ in range(1000):
            self.assertIs(self.cache.get_token("adx-scope"), self.token)
        self.source.get_token.assert_called_once_with("adx-scope")

    def test_installed_kusto_sdk_uses_cached_credential(self):
        connection = KustoConnectionStringBuilder.with_azure_token_credential(
            "https://offline.kusto.windows.net", self.cache,
        )
        self.assertIs(connection.azure_credential, self.cache)
        provider = AzureIdentityTokenCredentialProvider(connection.data_source, credential=self.cache)
        provider._scopes = ["adx-scope"]
        # Skip cloud metadata discovery; exercise the real SDK token path.
        with patch.object(provider, "_init_once"):
            for _ in range(1000):
                self.assertEqual(provider.get_token()["access_token"], self.token.token)
        self.source.get_token.assert_called_once_with("adx-scope")

    def test_refreshes_five_minutes_before_expiry(self):
        self.cache.get_token("adx-scope")
        self.now = 4699
        self.assertIs(self.cache.get_token("adx-scope"), self.token)
        refreshed = AccessToken("offline-refreshed", 9000)
        self.source.get_token.return_value = refreshed
        self.now = 4700
        self.assertIs(self.cache.get_token("adx-scope"), refreshed)
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_expired_cached_token_is_not_reused(self):
        self.cache.get_token("adx-scope")
        self.now = 5001
        refreshed = AccessToken("offline-refreshed", 9000)
        self.source.get_token.return_value = refreshed
        self.assertIs(self.cache.get_token("adx-scope"), refreshed)
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_different_scopes_do_not_share_tokens(self):
        self.cache.get_token("scope-a")
        self.cache.get_token("scope-b")
        self.cache.get_token("scope-a")
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_tenant_and_other_options_are_isolated_and_forwarded(self):
        for _ in range(2):
            self.cache.get_token("adx-scope", tenant_id="tenant-a")
            self.cache.get_token("adx-scope", tenant_id="tenant-b")
            self.cache.get_token("adx-scope", tenant_id="tenant-a", enable_cae=True)
        self.assertEqual(self.source.get_token.call_count, 3)
        self.source.get_token.assert_any_call("adx-scope", tenant_id="tenant-b")
        self.source.get_token.assert_any_call("adx-scope", tenant_id="tenant-a", enable_cae=True)

    def test_claims_challenge_bypasses_cached_token(self):
        self.cache.get_token("adx-scope")
        for _ in range(2):
            self.cache.get_token("adx-scope", claims="offline-challenge")
        self.assertEqual(self.source.get_token.call_count, 3)
        self.source.get_token.assert_called_with("adx-scope", claims="offline-challenge")

    def test_unhashable_options_bypass_cache(self):
        for _ in range(2):
            self.cache.get_token("adx-scope", options={"future-option": True})
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_threads_do_not_duplicate_initial_acquisition(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: self.cache.get_token("adx-scope"), range(32)))
        self.assertTrue(all(token is self.token for token in tokens))
        self.source.get_token.assert_called_once()

    def test_refresh_failure_is_not_cached_or_masked(self):
        self.cache.get_token("adx-scope")
        self.now = 4700
        refreshed = AccessToken("offline-refreshed", 9000)
        self.source.get_token.side_effect = [RuntimeError("offline CLI failure"), refreshed]
        with self.assertRaisesRegex(RuntimeError, "offline CLI failure"):
            self.cache.get_token("adx-scope")
        self.assertIs(self.cache.get_token("adx-scope"), refreshed)
        self.assertEqual(self.source.get_token.call_count, 3)

    def test_newly_returned_expired_token_is_rejected(self):
        self.source.get_token.return_value = AccessToken("offline-expired", 999)
        with self.assertRaisesRegex(ClientAuthenticationError, "expired token"):
            self.cache.get_token("adx-scope")
        self.source.get_token.return_value = self.token
        self.assertIs(self.cache.get_token("adx-scope"), self.token)
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_short_lived_token_is_briefly_reused(self):
        short = AccessToken("offline-short-lived", 1060)
        self.source.get_token.return_value = short
        self.cache.get_token("adx-scope")
        self.now = 1029
        self.assertIs(self.cache.get_token("adx-scope"), short)
        self.source.get_token.assert_called_once()
        self.now = 1030
        self.source.get_token.return_value = self.token
        self.assertIs(self.cache.get_token("adx-scope"), self.token)
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_expiry_is_checked_after_slow_acquisition(self):
        def slow_acquisition(*args, **kwargs):
            self.now = 1200
            return AccessToken("offline-expired-during-acquisition", 1100)
        self.source.get_token.side_effect = slow_acquisition
        with self.assertRaises(ClientAuthenticationError):
            self.cache.get_token("adx-scope")

    def test_worker_instances_keep_independent_caches(self):
        another = adx._CachedTokenCredential(self.source)
        self.cache.get_token("adx-scope")
        another.get_token("adx-scope")
        self.assertEqual(self.source.get_token.call_count, 2)

    def test_close_clears_memory_and_closes_underlying_credential(self):
        self.cache.get_token("adx-scope")
        self.cache.close()
        self.assertFalse(self.cache._tokens)
        self.source.close.assert_called_once_with()

    def test_local_factory_keeps_120_second_timeout(self):
        with patch.object(adx, "_running_in_azure_function", return_value=False), \
             patch.object(adx, "DefaultAzureCredential", return_value=self.source) as constructor:
            cached = adx._build_adx_credential()
        self.assertIsInstance(cached, adx._CachedTokenCredential)
        constructor.assert_called_once_with(exclude_environment_credential=True, process_timeout=120)

    def test_managed_identity_is_not_wrapped_or_changed(self):
        with patch.object(adx, "_running_in_azure_function", return_value=True), \
             patch.object(adx, "ManagedIdentityCredential", return_value=self.source) as managed, \
             patch.object(adx, "DefaultAzureCredential") as local:
            self.assertIs(adx._build_adx_credential(), self.source)
        managed.assert_called_once_with()
        local.assert_not_called()


if __name__ == "__main__":
    unittest.main()
