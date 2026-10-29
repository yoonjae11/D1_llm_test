"""Idempotent factories for Azure Table and Queue resources.

The working ZIP created Storage resources lazily. This preserves that behavior
for new Storage accounts while remaining compatible with least-privilege
deployments where resources are provisioned ahead of time.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_table_client(connection_string: str, table_name: str) -> Any:
    if not connection_string:
        raise RuntimeError("Azure Storage connection string is missing.")

    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableClient

    client = TableClient.from_connection_string(connection_string, table_name=table_name)
    try:
        client.create_table()
    except ResourceExistsError:
        pass
    except Exception as exc:  # Existing resources may be usable without create permission.
        logger.debug("Table ensure skipped table=%s error_type=%s", table_name, type(exc).__name__)
    return client


def create_queue_client(connection_string: str, queue_name: str) -> Any:
    if not connection_string:
        raise RuntimeError("Azure Storage connection string is missing.")

    from azure.core.exceptions import ResourceExistsError
    from azure.storage.queue import QueueClient

    client = QueueClient.from_connection_string(connection_string, queue_name)
    try:
        client.create_queue()
    except ResourceExistsError:
        pass
    except Exception as exc:  # Existing resources may be usable without create permission.
        logger.debug("Queue ensure skipped queue=%s error_type=%s", queue_name, type(exc).__name__)
    return client


__all__ = ["create_queue_client", "create_table_client"]
