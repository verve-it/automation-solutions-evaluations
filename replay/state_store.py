#!/usr/bin/env python3
"""
state_store.py — where a replay's cursor and journal live between calls.

Locally this is a dictionary and the file is nearly pointless. Hosted, it is
the difference between a gate and a coin flip.

Why it exists
-------------
A cassette replays in order: `cw_get_ticket 805392` returns five different
results across one orchestration because the agents mutate the ticket as they
go, so the queue cursor *is* the fidelity of the replay. Held in a process,
that cursor is correct exactly as long as every call of a run reaches the
same process.

Azure Functions does not promise that. Flex Consumption will not scale out a
single sequential client in practice, but "in practice" is not what a gate
rests on — and the floor for `maximumInstanceCount` on that plan is 40, so
pinning the app to one instance is not available either. Keeping state
outside the process makes the question moot and survives an instance recycle
mid-run as a bonus, which is what makes `/summary` trustworthy afterwards.

Concurrency
-----------
Calls within one MCP session are sequential, so the ETag check is not there
to arbitrate a race between two agent turns. It is there to make a lost
update *loud*: two instances both answering one session means the replay is
already wrong, and returning a conflict says so instead of silently
double-advancing the cursor.
"""

from __future__ import annotations

import json
import threading


class Conflict(Exception):
    """Another writer advanced this session's state. The replay is unsound."""


class MemoryStore:
    """In-process. The default, and the whole story for a local run."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def load(self, key):
        with self._lock:
            entry = self._data.get(key)
        if entry is None:
            return None, None
        state, version = entry
        return json.loads(json.dumps(state)), version

    def save(self, key, state, version):
        with self._lock:
            current = self._data.get(key)
            if current is not None and current[1] != version:
                raise Conflict(key)
            nxt = 1 if current is None else current[1] + 1
            self._data[key] = (json.loads(json.dumps(state)), nxt)
            return nxt

    def close(self):
        pass


class BlobStore:
    """One block blob per session, guarded by its ETag.

    Two ways in. `DefaultAzureCredential` against an account URL is the one to
    want: in Azure that is the function app's managed identity and there is no
    key anywhere to leak. A connection string is the fallback for a
    subscription where nobody can assign the role -- see `storageAuth` in
    infra/main.bicep. The store does not care which; everything above it is
    identical.

    The container is created on first use because a replay should not need a
    provisioning step of its own.
    """

    def __init__(self, account_url=None, container=None, credential=None,
                 connection_string=None):
        from azure.storage.blob import BlobServiceClient

        if connection_string:
            self._service = BlobServiceClient.from_connection_string(
                connection_string)
        else:
            from azure.identity import DefaultAzureCredential
            self._service = BlobServiceClient(
                account_url, credential=credential or DefaultAzureCredential())
        self._container = self._service.get_container_client(container)
        try:
            self._container.create_container()
        except Exception:
            # Already there, or the identity may write blobs but not create
            # containers. Either way the next call reports the real problem.
            pass

    def _blob(self, key):
        return self._container.get_blob_client(f"{key}.json")

    def load(self, key):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            downloaded = self._blob(key).download_blob()
        except ResourceNotFoundError:
            return None, None
        payload = json.loads(downloaded.readall() or b"{}")
        return payload, downloaded.properties.etag

    def save(self, key, state, version):
        from azure.core.exceptions import ResourceModifiedError
        from azure.storage.blob import ContentSettings
        body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        settings = ContentSettings(content_type="application/json")
        try:
            if version is None:
                result = self._blob(key).upload_blob(
                    body, overwrite=False, content_settings=settings)
            else:
                result = self._blob(key).upload_blob(
                    body, overwrite=True, content_settings=settings,
                    etag=version, match_condition=_MATCH_ETAG)
        except ResourceModifiedError as exc:
            raise Conflict(key) from exc
        except Exception as exc:
            # A blob that already exists when we believed it did not is the
            # same failure wearing a different exception class.
            if "ConditionNotMet" in str(exc) or "BlobAlreadyExists" in str(exc):
                raise Conflict(key) from exc
            raise
        return result.get("etag")

    def close(self):
        self._service.close()


try:                                    # only importable where the SDK is
    from azure.core import MatchConditions as _MC
    _MATCH_ETAG = _MC.IfNotModified
except Exception:                       # pragma: no cover - local runs
    _MATCH_ETAG = None


def open_store(account_url=None, container=None, connection_string=None):
    """A BlobStore when told where to put things, a MemoryStore otherwise.

    Deliberately not an error when unconfigured: `func start` on a laptop
    should work with no Azure at all, and the hosted deployment sets what it
    needs in Bicep.
    """
    if container and (account_url or connection_string):
        return BlobStore(account_url, container,
                         connection_string=connection_string)
    return MemoryStore()
