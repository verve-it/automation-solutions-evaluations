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

Azure Functions does not promise that. Flex Consumption is unlikely to
scale out for one replay, but "unlikely" is not what a gate rests on -- and a
replay is not a sequential client: the ops agent sends up to nine calls at
once. The floor for `maximumInstanceCount` on that plan is 40, so
pinning the app to one instance is not available either. Keeping state
outside the process makes the question moot and survives an instance recycle
mid-run as a bonus, which is what makes `/summary` trustworthy afterwards.

Concurrency
-----------
Calls within one MCP session are NOT sequential. The ops agent fans out, and
its recordings show up to nine MCP calls in flight at once. The ETag check
makes a lost update loud rather than silent; the hosted server
(functions/replay-mcp/server.py) serialises a session's calls within an
instance and, when another instance wins the race, reloads and re-applies the
call. Only a race lost on every retry reaches the agent as a 409.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from urllib.parse import quote


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


class SasBlobStore:
    """Blob state over plain HTTPS, with a container SAS. No SDK, no build.

    `azure-storage-blob` is not importable in a custom handler. Oryx installs
    it into `.python_packages/lib/site-packages`, which the Functions *Python
    worker* adds to `sys.path` -- but a custom handler is `python server.py`
    and gets none of that setup. The handler logged exactly that:

        ModuleNotFoundError: No module named 'azure'

    The blob REST API needs no SDK. Signing it would, so the signature is
    minted once at deploy time as a container SAS and handed over as a URL;
    everything here is urllib and json. That also takes `--build-remote` out
    of the deployment: with nothing to install, there is nothing to install
    wrongly.

    The SAS is a secret, container-scoped and time-limited -- narrower than
    the account key it replaces.
    """

    TIMEOUT = 15

    def __init__(self, container_url):
        base, _, query = container_url.partition("?")
        self._base = base.rstrip("/")
        self._query = query
        if not self._query:
            raise ValueError("REPLAY_STATE_SAS carries no SAS token")
        self._probe()

    def _url(self, key):
        return f"{self._base}/{quote(key, safe='')}.json?{self._query}"

    def _probe(self):
        """A 404 is the success case: reachable, and the SAS authenticates.

        Finding out on the first tools/call of a replay instead would make a
        configuration mistake look like a mid-run failure of the agent.
        """
        try:
            self._request("GET", self._url("__probe__"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise

    def _request(self, method, url, body=None, headers=None):
        request = urllib.request.Request(url, data=body, method=method,
                                         headers=headers or {})
        return urllib.request.urlopen(request, timeout=self.TIMEOUT)

    def load(self, key):
        try:
            with self._request("GET", self._url(key)) as response:
                payload = json.loads(response.read() or b"{}")
                return payload, response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, None
            raise

    def save(self, key, state, version):
        body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        headers = {"x-ms-blob-type": "BlockBlob",
                   "Content-Type": "application/json"}
        # The conditional header is the whole point: two instances answering
        # one replay means the ordering this gate depends on is already
        # broken, and a silent last-writer-wins would hide it.
        headers["If-Match" if version else "If-None-Match"] = version or "*"
        try:
            with self._request("PUT", self._url(key), body, headers) as resp:
                return resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code in (409, 412):
                raise Conflict(key) from exc
            raise

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

    # Bounded on purpose. This runs at start-up, and a custom handler that
    # takes too long to answer is a function app the host gives up on -- the
    # symptom is a 502 that says nothing about storage. Better to fail the
    # probe in seconds and degrade than to hang and look dead.
    PROBE = {"retry_total": 1, "connection_timeout": 5, "read_timeout": 10}

    def __init__(self, account_url=None, container=None, credential=None,
                 connection_string=None):
        from azure.storage.blob import BlobServiceClient

        if connection_string:
            self._service = BlobServiceClient.from_connection_string(
                connection_string, **self.PROBE)
        else:
            self._service = BlobServiceClient(
                account_url, credential=credential or _credential(),
                **self.PROBE)
        self._container = self._service.get_container_client(container)
        try:
            self._container.create_container()
        except Exception:
            # Already there, or this identity may write blobs without being
            # allowed to create containers. Neither is a problem; the probe
            # below decides.
            pass

        # Probe once, here, rather than discovering the truth on the first
        # tools/call of a replay. Construction that succeeds against storage
        # nobody can reach turns a configuration mistake into a mid-run 500
        # that reads like the agent failed.
        self._container.get_container_properties()

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


def _credential():
    """The narrowest credential that can work here.

    `DefaultAzureCredential` walks a chain that includes the CLI, PowerShell,
    VS Code and a broker, none of which exist in a function app. Walking it to
    failure took 37 seconds in testing -- long enough on its own to make the
    host give up on the handler. Inside Azure the answer is the managed
    identity and Bicep names it in AZURE_CLIENT_ID, so ask for that directly
    and keep the chain for everywhere else.
    """
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        from azure.identity import ManagedIdentityCredential
        return ManagedIdentityCredential(client_id=client_id)
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


class LazyStore:
    """Resolve the backend on first use, never at start-up.

    A custom handler that has not bound its port yet is a 502, and the host
    does not wait long. Nothing about reaching a storage account belongs on
    that path: a slow credential, a firewall, a typo in a container name --
    each turns into an app that looks dead rather than one that says what is
    wrong.

    So the socket opens immediately and the first tools/call pays for the
    backend, once. Health reports which one it got.
    """

    def __init__(self, factory):
        self._factory = factory
        self._store = None
        self._lock = threading.Lock()

    def _resolve(self):
        if self._store is None:
            with self._lock:
                if self._store is None:
                    self._store = self._factory()
        return self._store

    def load(self, key):
        return self._resolve().load(key)

    def save(self, key, state, version):
        return self._resolve().save(key, state, version)

    def close(self):
        if self._store is not None:
            self._store.close()

    @property
    def backend(self):
        return type(self._store).__name__ if self._store else "unresolved"


try:                                    # only importable where the SDK is
    from azure.core import MatchConditions as _MC
    _MATCH_ETAG = _MC.IfNotModified
except Exception:                       # pragma: no cover - local runs
    _MATCH_ETAG = None


def open_store(account_url=None, container=None, connection_string=None,
               sas_url=None):
    """A BlobStore when told where to put things, a MemoryStore otherwise.

    Deliberately not an error when unconfigured: `func start` on a laptop
    should work with no Azure at all, and the hosted deployment sets what it
    needs in Bicep.

    It is also not an error when configured and unreachable. A missing SDK or
    a storage account that will not answer is a degraded replay -- ordering
    holds only while one instance serves the run -- but a server that refuses
    to start is a 502, which says nothing and gates nothing. Warn loudly,
    carry on, and let the health endpoint report which backend is live so the
    verifier can say so too.
    """
    if not (sas_url or (container and (account_url or connection_string))):
        return MemoryStore()

    def resolve():
        try:
            if sas_url:
                return SasBlobStore(sas_url)
            return BlobStore(account_url, container,
                             connection_string=connection_string)
        except Exception as exc:
            print(f"WARNING  replay state could not use blob storage: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            print("WARNING  falling back to in-process state. A replay stays "
                  "ordered only while one instance serves the whole run, and "
                  "the journal does not survive a restart.", flush=True)
            return MemoryStore()

    return LazyStore(resolve)
