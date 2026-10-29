import logging
import os
import random
import threading
import time

from azure.core.exceptions import ClientAuthenticationError
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential


def _exception_chain_text(exc: Exception, max_depth: int = 6) -> str:
    parts = []
    seen = set()
    current = exc
    depth = 0
    while current is not None and depth < max_depth and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text:
            parts.append(text)
        current = current.__cause__ or current.__context__
        depth += 1
    return " | ".join(parts)


def _normalize_ca_bundle_path(value: str) -> str:
    raw = (value or "").strip().strip("\"").strip("'")
    if not raw:
        return ""

    raw = os.path.expanduser(raw)

    # Convert Git-Bash style path (/c/Users/...) to Windows path if needed.
    if os.name == "nt" and raw.startswith("/") and len(raw) > 3 and raw[1].isalpha() and raw[2] == "/":
        tail = raw[3:].replace("/", "\\")
        raw = f"{raw[1].upper()}:\\{tail}"

    return raw


def _is_tls_cert_error(lowered_error_text: str) -> bool:
    return (
        "certificate verify failed" in lowered_error_text
        or "ssl: certificate_verify_failed" in lowered_error_text
    )


def _running_in_azure_function() -> bool:
    return any(
        os.environ.get(key)
        for key in ("IDENTITY_ENDPOINT", "MSI_ENDPOINT", "WEBSITE_INSTANCE_ID", "WEBSITE_SITE_NAME")
    )


class _CachedTokenCredential:
    """Reuse local tokens in this process; never persist or log bearer tokens.

    Refresh five minutes before expiry. If the CLI returns a short-lived token,
    briefly reuse it rather than starting a new CLI process for every query.
    Scopes and all credential options form the cache key; claims challenges
    always go directly to the underlying credential.
    """

    def __init__(self, credential):
        self._credential = credential
        self._tokens = {}
        self._lock = threading.Lock()

    def get_token(self, *scopes, **kwargs):
        if kwargs.get("claims"):
            return self._credential.get_token(*scopes, **kwargs)
        try:
            key = (scopes, tuple(sorted(kwargs.items())))
            hash(key)
        except TypeError:
            return self._credential.get_token(*scopes, **kwargs)

        # Serialize initial acquisition/refresh so threads cannot start
        # duplicate CLI processes for the same token.
        with self._lock:
            cached = self._tokens.get(key)
            now = time.time()
            if cached is not None:
                token, refresh_at = cached
                if now < refresh_at and now < token.expires_on:
                    return token

            self._tokens.pop(key, None)
            # Several local evaluation processes starting together can make a
            # CLI token call fail transiently; retry briefly before giving up.
            for attempt in range(3):
                try:
                    token = self._credential.get_token(*scopes, **kwargs)
                    break
                except ClientAuthenticationError:
                    if attempt == 2:
                        raise
                    time.sleep(3.0 + random.random() * 5.0)
            now = time.time()  # CLI acquisition can take up to 120 seconds.
            remaining = token.expires_on - now
            if remaining <= 0:
                raise ClientAuthenticationError("Local ADX credential returned an expired token.")
            refresh_at = max(token.expires_on - 300, now + min(30, remaining / 2))
            self._tokens[key] = (token, refresh_at)
            return token

    def close(self):
        with self._lock:
            self._tokens.clear()
            self._credential.close()


def _build_adx_credential():
    if _running_in_azure_function():
        logging.info("Chatbot ADX authentication: using Function App managed identity.")
        return ManagedIdentityCredential()

    logging.info(
        "Chatbot ADX authentication: using DefaultAzureCredential for local development "
        "(environment credential excluded)."
    )
    return _CachedTokenCredential(
        DefaultAzureCredential(exclude_environment_credential=True, process_timeout=120)
    )


class ADXManager:
    _instance = None

    def __new__(cls):
        # Singleton pattern: Create only one connection object while the app is running.
        if cls._instance is None:
            cls._instance = super(ADXManager, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        self.client = None
        self.db = None
        self.cluster = None
        self.credential = None
        self._tls_fallback_attempted = False

        try:
            self.cluster = os.environ.get("ADX_CLUSTER_URI") or os.environ.get("ADX_QUERY_URI")
            self.db = os.environ.get("ADX_DATABASE")

            if not all([self.cluster, self.db]):
                logging.error("ADX environment variables are missing. Please check ADX_CLUSTER_URI/ADX_QUERY_URI and ADX_DATABASE.")
                return

            self._normalize_existing_ca_env()
            self.credential = _build_adx_credential()
            self._rebuild_client()
            logging.info("ADX Client Initialized. Target DB: %s", self.db)

        except Exception as exc:
            logging.error("ADX Client Initialization Failed: %s", exc)

    def _normalize_existing_ca_env(self):
        for key in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
            current = os.environ.get(key)
            if not current:
                continue

            normalized = _normalize_ca_bundle_path(current)
            if normalized and normalized != current:
                os.environ[key] = normalized

            final_path = os.environ.get(key)
            if final_path and not os.path.exists(final_path):
                logging.warning("%s points to a missing path: %s", key, final_path)

    def _rebuild_client(self):
        if self.credential is None:
            self.credential = _build_adx_credential()
        kcsb = KustoConnectionStringBuilder.with_azure_token_credential(self.cluster, self.credential)
        self.client = KustoClient(kcsb)

    def _activate_certifi_tls_fallback(self) -> bool:
        if self._tls_fallback_attempted:
            return False
        self._tls_fallback_attempted = True

        try:
            import certifi

            certifi_path = certifi.where()
            os.environ["REQUESTS_CA_BUNDLE"] = certifi_path
            os.environ["SSL_CERT_FILE"] = certifi_path
            self._rebuild_client()
            logging.info("Applied TLS fallback with certifi bundle: %s", certifi_path)
            return True
        except Exception as exc:
            logging.error("Failed to apply TLS fallback with certifi: %s", exc)
            return False

    def execute_query(self, query: str, suppress_error_log: bool = False):
        """
        Executes a KQL query and returns the result as a list of dictionaries.
        """
        if not self.client:
            return "Error: ADX Client is not initialized."

        transient_markers = [
            "failed to process network request",
            "timed out",
            "temporarily unavailable",
            "connection",
            "eof",
            "ssl",
            "429",
            "503",
        ]
        max_retries = 2
        backoff_seconds = 0.8
        # Server throttling (429) clears once concurrent load drops, so it gets
        # a longer jittered backoff than other transient failures.
        throttle_markers = ("429", "throttled", "too many requests")
        max_throttle_retries = 6

        def retry_delay(lowered: str, attempt: int) -> float | None:
            if any(marker in lowered for marker in throttle_markers):
                if attempt < max_throttle_retries:
                    return min(30.0, 2.0 * (2 ** attempt)) * (1.0 + random.random() * 0.5)
                return None
            if any(marker in lowered for marker in transient_markers) and attempt < max_retries:
                return backoff_seconds * (2 ** attempt)
            return None

        for attempt in range(max(max_retries, max_throttle_retries) + 1):
            try:
                response = self.client.execute(self.db, query)

                result = []
                if response.primary_results:
                    for row in response.primary_results[0]:
                        result.append(row.to_dict())
                return result

            except KustoServiceError as exc:
                msg = str(exc)
                chain_text = _exception_chain_text(exc)
                lowered = f"{msg} {chain_text}".lower()

                if _is_tls_cert_error(lowered) and self._activate_certifi_tls_fallback():
                    continue

                sleep_sec = retry_delay(lowered, attempt)
                if sleep_sec is not None:
                    logging.warning(
                        "Transient Kusto error on attempt %d. Retrying in %.1fs.",
                        attempt + 1,
                        sleep_sec,
                    )
                    time.sleep(sleep_sec)
                    continue

                if suppress_error_log:
                    logging.debug("Kusto Service Error (suppressed): %s", exc)
                else:
                    logging.error("Kusto Service Error: %s", exc)

                if _is_tls_cert_error(lowered):
                    return (
                        f"Error (Kusto): {msg} "
                        "(TLS certificate verification failed. Check trusted root/intermediate CA settings.)"
                    )
                return f"Error (Kusto): {msg}"

            except Exception as exc:
                msg = str(exc)
                chain_text = _exception_chain_text(exc)
                lowered = f"{msg} {chain_text}".lower()

                if _is_tls_cert_error(lowered) and self._activate_certifi_tls_fallback():
                    continue

                sleep_sec = retry_delay(lowered, attempt)
                if sleep_sec is not None:
                    logging.warning(
                        "Transient query error on attempt %d. Retrying in %.1fs.",
                        attempt + 1,
                        sleep_sec,
                    )
                    time.sleep(sleep_sec)
                    continue

                if suppress_error_log:
                    logging.debug("Query Error (suppressed): %s", exc)
                else:
                    logging.error("Query Error: %s", exc)

                if _is_tls_cert_error(lowered):
                    return (
                        f"Error: {msg} "
                        "(TLS certificate verification failed. Check trusted root/intermediate CA settings.)"
                    )
                return f"Error: {msg}"


# Create an object to be imported externally
adx_manager = ADXManager()
