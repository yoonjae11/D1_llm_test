from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from runtime_environment import load_local_environment
from queue_contracts import (
    METRIC_AVAILABILITY_QUEUE,
    SYSTEM_PHONE_INGEST_EVENT_QUEUE,
    SYSTEM_STEPS_RETRY_QUEUE,
    WITHINGS_WEBHOOK_QUEUE,
)

try:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.data.tables import TableClient
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
    from azure.storage.queue import QueueClient
except Exception:  # pragma: no cover - operator utility reports this at runtime.
    ResourceNotFoundError = Exception
    TableClient = None
    DefaultAzureCredential = None
    ManagedIdentityCredential = None
    KustoClient = None
    KustoConnectionStringBuilder = None
    QueueClient = None


TABLES = [
    "ChatbotConversationState",
    "ChatbotUserProfile",
    "ChatbotMetricAvailability",
    "SystemInitiatedDailyTriggerState",
]

QUEUES = [
    METRIC_AVAILABILITY_QUEUE,
    SYSTEM_PHONE_INGEST_EVENT_QUEUE,
    SYSTEM_STEPS_RETRY_QUEUE,
    WITHINGS_WEBHOOK_QUEUE,
]

ADX_TABLE_MAPPINGS = [
    ("UserMetricMadBaselineSnapshot", "UserMetricMadBaselineSnapshotJsonMapping"),
    ("AlertCandidate", "AlertCandidateJsonMapping"),
]


def _repo_root() -> Path:
    return REPOSITORY_ROOT


def _load_local_settings() -> None:
    """Compatibility entry point for the shared local-environment loader."""

    load_local_environment(_repo_root())


def _running_in_azure_function() -> bool:
    return any(os.environ.get(key) for key in ("IDENTITY_ENDPOINT", "MSI_ENDPOINT", "WEBSITE_INSTANCE_ID"))


def _credential():
    if DefaultAzureCredential is None or ManagedIdentityCredential is None:
        raise RuntimeError("Azure identity SDK is unavailable. Install requirements.txt first.")
    if _running_in_azure_function():
        return ManagedIdentityCredential()
    return DefaultAzureCredential(exclude_environment_credential=True)


def _check_table(connection_string: str, table_name: str) -> str:
    if TableClient is None:
        return "Azure data tables SDK is unavailable. Install requirements.txt first."
    client = TableClient.from_connection_string(connection_string, table_name=table_name)
    try:
        pages = client.query_entities("PartitionKey eq '__verify__'", results_per_page=1).by_page()
        next(pages, [])
    except ResourceNotFoundError:
        return f"missing table: {table_name}"
    except StopIteration:
        return ""
    except Exception as exc:
        text = str(exc)
        if "TableNotFound" in text or "ResourceNotFound" in text:
            return f"missing table: {table_name}"
        return f"table check failed: {table_name}: {exc}"
    return ""


def _check_queue(connection_string: str, queue_name: str) -> str:
    if QueueClient is None:
        return "Azure storage queue SDK is unavailable. Install requirements.txt first."
    client = QueueClient.from_connection_string(connection_string, queue_name)
    try:
        client.get_queue_properties()
    except ResourceNotFoundError:
        return f"missing queue: {queue_name}"
    except Exception as exc:
        text = str(exc)
        if "QueueNotFound" in text or "ResourceNotFound" in text:
            return f"missing queue: {queue_name}"
        return f"queue check failed: {queue_name}: {exc}"
    return ""


def _check_user_profile(connection_string: str, table_name: str, app_user_id: str) -> str:
    if not app_user_id:
        return ""
    if TableClient is None:
        return "Azure data tables SDK is unavailable. Install requirements.txt first."
    client = TableClient.from_connection_string(connection_string, table_name=table_name)
    try:
        client.get_entity(partition_key="user", row_key=app_user_id)
    except ResourceNotFoundError:
        return f"missing ChatbotUserProfile seed row: {app_user_id}"
    except Exception as exc:
        text = str(exc)
        if "ResourceNotFound" in text or "TableNotFound" in text:
            return f"missing ChatbotUserProfile seed row or table: {app_user_id}"
        return f"user profile check failed: {app_user_id}: {exc}"
    return ""


def _kusto_client() -> tuple[KustoClient, str]:
    if KustoClient is None or KustoConnectionStringBuilder is None:
        raise RuntimeError("Azure Kusto SDK is unavailable. Install requirements.txt first.")
    cluster = os.environ.get("ADX_CLUSTER_URI") or os.environ.get("ADX_QUERY_URI")
    database = os.environ.get("ADX_DATABASE")
    if not cluster or not database:
        raise RuntimeError("ADX_CLUSTER_URI/ADX_QUERY_URI or ADX_DATABASE is missing")
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(cluster, _credential())
    return KustoClient(kcsb), database


def _rows(response) -> list:
    if not response.primary_results:
        return []
    return [row.to_dict() for row in response.primary_results[0]]


def _check_adx() -> List[str]:
    errors: List[str] = []
    client, database = _kusto_client()
    for table, mapping in ADX_TABLE_MAPPINGS:
        try:
            client.execute_mgmt(database, f".show table {table} schema as json")
        except Exception as exc:
            errors.append(f"missing or inaccessible ADX table: {table}: {exc}")
            continue
        try:
            rows = _rows(client.execute_mgmt(database, f".show table {table} ingestion json mappings"))
            names = {str(row.get("Name") or row.get("MappingName") or "") for row in rows}
            if mapping not in names:
                errors.append(f"missing ADX JSON ingestion mapping: {table}.{mapping}")
        except Exception as exc:
            errors.append(f"ADX mapping check failed: {table}.{mapping}: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify pre-created chatbot operational resources.")
    parser.add_argument("--app-user-id", default="kist_8401", help="Optional ChatbotUserProfile seed row to verify.")
    parser.add_argument("--skip-adx", action="store_true", help="Skip ADX table/mapping checks.")
    args = parser.parse_args()

    _load_local_settings()
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    errors: List[str] = []
    if not connection_string:
        errors.append("AzureWebJobsStorage is missing")
    else:
        for table_name in TABLES:
            error = _check_table(connection_string, table_name)
            if error:
                errors.append(error)
        for queue_name in QUEUES:
            error = _check_queue(connection_string, queue_name)
            if error:
                errors.append(error)
        error = _check_user_profile(connection_string, "ChatbotUserProfile", args.app_user_id)
        if error:
            errors.append(error)
    if not args.skip_adx:
        try:
            errors.extend(_check_adx())
        except Exception as exc:
            errors.append(f"ADX verification failed: {exc}")

    if errors:
        errors = list(dict.fromkeys(errors))
        print("Resource verification failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("All chatbot operational resources are present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
