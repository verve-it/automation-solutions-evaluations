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

Backends, all stdlib
--------------------
  IdentityBlobStore  the function app's managed identity: a token from the
                     platform's identity endpoint, a bearer header on the
                     blob REST API. No key, no SAS, nothing that expires.
  SasBlobStore       a container SAS minted at deploy time, for a deployment
                     that could not assign the identity a role. It expires;
                     health says when.
  MemoryStore        in-process, and only when no store is configured. Right
                     for a laptop; hosted, verify.py fails it.

A configured store that cannot be reached is never replaced by in-process
state. The server still starts -- a handler that will not start is a 502,
which says nothing -- but every MCP call is answered with an error naming the
cause, and the store is tried again a few seconds later. It used to fall back
to a MemoryStore and keep it for the life of the instance, so one failed
token request on one instance of a scaled-out app gave that instance its own
cursor: the agent was answered from the wrong place in the recording.

There is no SDK here, on purpose. `azure-storage-blob` and `azure-identity`
are not importable in a custom handler -- Oryx installs them where only the
Functions *Python worker* looks -- which is how a 50-call replay once
journalled 3. The SDK-based store that remained here was reached only by the
identity mode, where it could not import, and fell back to in-process state
without a word: the default deployment was silently the broken one.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlencode

# Backends that survive an instance recycle and are shared across instances.
# Anything else, hosted, means the ordering the gate rests on is not held.
DURABLE = ("IdentityBlobStore", "SasBlobStore")


class Conflict(Exception):
    """Another writer advanced this session's state first."""


class Unavailable(Exception):
    """The configured store cannot be used right now.

    Answered as an error, never from in-process state: a replay answered from
    a cursor that other instances cannot see is a plausible wrong answer.
    """


def sas_expiry(container_url):
    """The `se=` of a SAS URL, as written, or None."""
    query = container_url.partition("?")[2]
    return (parse_qs(query).get("se") or [None])[0]


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

    detail = {}


class _RestBlobStore:
    """One block blob per session over the blob REST API, guarded by its ETag.

    Subclasses say only how a request is authorised. Loading returns the
    ETag; saving is conditional on it (If-Match), or on the blob not existing
    yet (If-None-Match: *), and a 409 or 412 is a Conflict -- another writer
    got there first, which the server answers by reloading and re-applying.
    """

    TIMEOUT = 15
    GET_ATTEMPTS = 3
    TRANSIENT = (408, 429, 500, 502, 503, 504)

    def __init__(self, container_url):
        self._base = container_url.rstrip("/")
        self._probe()

    # --- what a subclass provides
    def _query(self):
        return ""

    def _auth_headers(self):
        return {}

    # --- the REST API
    def _url(self, key):
        query = self._query()
        return (f"{self._base}/{quote(key, safe='')}.json"
                + (f"?{query}" if query else ""))

    def _request(self, method, url, body=None, headers=None):
        """One request. A GET that meets a transient error is retried, as the
        SDK this replaces did; a PUT is not -- one that committed and lost
        its response would be applied twice, and the ETag would call the
        second a conflict with ourselves."""
        attempts = self.GET_ATTEMPTS if method == "GET" else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            all_headers = dict(self._auth_headers())
            all_headers.update(headers or {})
            request = urllib.request.Request(url, data=body, method=method,
                                             headers=all_headers)
            try:
                return urllib.request.urlopen(request, timeout=self.TIMEOUT)
            except urllib.error.HTTPError as exc:
                if last or exc.code not in self.TRANSIENT:
                    raise
            except (urllib.error.URLError, OSError):
                if last:
                    raise
            time.sleep(0.2 * 2 ** attempt)

    def _probe(self):
        """A 404 for the blob is the success case: reachable, credential works.

        Not any 404: a missing *container* is one too, and every save would
        then fail. The error code says which.

        Finding out on the first tools/call of a replay instead would make a
        configuration mistake look like a mid-run failure of the agent.
        """
        try:
            self._request("GET", self._url("__probe__"))
        except urllib.error.HTTPError as exc:
            code = exc.headers.get("x-ms-error-code") if exc.headers else None
            if exc.code != 404 or code == "ContainerNotFound":
                raise

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
        headers["If-Match" if version else "If-None-Match"] = version or "*"
        try:
            return self._put(key, body, headers)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            # No response: the write may or may not have landed. Seen on a
            # freshly started instance -- `save failed: URLError: <urlopen
            # error timed out>` -- with the next call 3 s later fine, and it
            # failed a whole gate run. A blind retry could apply the call
            # twice, so read the blob back and decide from what is there.
            return self._settle_lost_put(key, body, headers, version, exc)

    def _put(self, key, body, headers):
        try:
            with self._request("PUT", self._url(key), body, headers) as resp:
                return resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code in (409, 412):
                raise Conflict(key) from exc
            raise

    def _settle_lost_put(self, key, body, headers, version, cause):
        """After a PUT that got no response:

        * the blob holds exactly what we sent -> it landed; return its ETag;
        * the blob is unchanged from the version we wrote against (or still
          absent, for a create) -> it did not land; write again, still
          conditional, so a writer in between is a Conflict, not lost;
        * anything else -> another writer won; Conflict, which the server
          answers by reloading and re-applying, as for any lost race.
        """
        try:
            with self._request("GET", self._url(key)) as response:
                current, etag = response.read(), response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise cause
            current, etag = None, None
        except (urllib.error.URLError, OSError):
            raise cause
        if current == body:
            return etag
        if (version is None and current is None) or (version and etag == version):
            return self._put(key, body, headers)
        raise Conflict(key) from cause

    def close(self):
        pass

    detail = {}


class SasBlobStore(_RestBlobStore):
    """Blob state with a container SAS, for a deployment that could not give
    its identity a storage role.

    The SAS is a secret: container-scoped, read/write, and time-limited -- it
    expires (`se=`), after which every call is a 403 and every MCP call an
    error. `detail` carries the expiry so the health endpoint, and verify.py,
    can say so before it happens rather than after.
    """

    def __init__(self, container_url):
        base, _, query = container_url.partition("?")
        if not query:
            raise ValueError("REPLAY_STATE_SAS carries no SAS token")
        self._sas = query
        self.detail = {"auth": "container SAS",
                       "expires": sas_expiry(container_url)}
        super().__init__(base)

    def _query(self):
        return self._sas


class IdentityBlobStore(_RestBlobStore):
    """Blob state as the function app's managed identity. No secret at all.

    App Service and Functions give a process a local token endpoint in
    IDENTITY_ENDPOINT, and a per-process value in IDENTITY_HEADER that the
    request must echo (it is what stops a server-side request forgery from
    minting tokens). The protocol, from Microsoft's managed-identity REST
    reference:

        GET {IDENTITY_ENDPOINT}?resource=https://storage.azure.com/
            &api-version=2019-08-01&client_id=<user-assigned identity>
        X-IDENTITY-HEADER: {IDENTITY_HEADER}
        -> {"access_token": ..., "expires_on": <unix seconds>, ...}

    The token then rides as a bearer header on the blob REST API, which for
    OAuth needs x-ms-version 2017-11-09 or later. Tokens are cached and
    refreshed five minutes before they expire; a refresh that fails is
    retried, and while the cached token is still valid it is used rather than
    failing the call. The SDK this replaces retried too.

    A role assigned directly to the identity takes up to about ten minutes to
    take effect (Azure RBAC troubleshooting; the ~24 hours in the managed
    identity docs is for *group* membership, which this does not use). Until
    it does the probe is a 403 and the store is Unavailable; it is retried,
    so nothing needs restarting once the role arrives.
    """

    RESOURCE = "https://storage.azure.com/"
    TOKEN_API = "2019-08-01"
    BLOB_API = "2021-08-06"
    REFRESH_MARGIN = 300
    TOKEN_ATTEMPTS = 3
    REFRESH_BACKOFF = 30

    def __init__(self, account_url, container, client_id=None, env=None):
        env = os.environ if env is None else env
        self._endpoint = env.get("IDENTITY_ENDPOINT")
        self._secret = env.get("IDENTITY_HEADER")
        if not (self._endpoint and self._secret):
            absent = [k for k in ("IDENTITY_ENDPOINT", "IDENTITY_HEADER")
                      if not env.get(k)]
            raise RuntimeError(
                f"no managed-identity endpoint in this process "
                f"({', '.join(absent)} unset). The platform sets both for an "
                "app with a managed identity; without them there is no token "
                "to ask for.")
        self._client_id = client_id
        self._token, self._expires = None, 0.0
        self._refresh_after = 0.0
        self._token_lock = threading.Lock()
        self.detail = {"auth": "managed identity",
                       "client_id": client_id or "system-assigned"}
        super().__init__(f"{account_url.rstrip('/')}/{container}")

    def _bearer(self):
        with self._token_lock:
            now = time.time()
            if self._token and now < self._expires - self.REFRESH_MARGIN:
                return self._token
            # A refresh failed moments ago and the token still works: do not
            # make every call wait out the endpoint again.
            if self._token and now < min(self._refresh_after, self._expires):
                return self._token
            try:
                payload = self._fetch_token()
            except Exception as exc:
                if self._token and now < self._expires:
                    self._refresh_after = time.time() + self.REFRESH_BACKOFF
                    print(f"WARNING  token refresh failed ({type(exc).__name__}: "
                          f"{exc}); using the cached token, valid for "
                          f"{int(self._expires - now)} s more", flush=True)
                    return self._token
                raise
            self._token = payload["access_token"]
            self._expires = float(payload.get("expires_on") or 0)
            return self._token

    def _fetch_token(self):
        params = {"resource": self.RESOURCE, "api-version": self.TOKEN_API}
        if self._client_id:
            params["client_id"] = self._client_id
        request = urllib.request.Request(
            f"{self._endpoint}?{urlencode(params)}",
            headers={"X-IDENTITY-HEADER": self._secret})
        for attempt in range(self.TOKEN_ATTEMPTS):
            last = attempt == self.TOKEN_ATTEMPTS - 1
            try:
                with urllib.request.urlopen(request, timeout=self.TIMEOUT) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as exc:
                if last or exc.code not in self.TRANSIENT:
                    raise
            except (urllib.error.URLError, OSError):
                if last:
                    raise
            time.sleep(0.2 * 2 ** attempt)

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self._bearer()}",
                "x-ms-version": self.BLOB_API}


class LazyStore:
    """Resolve the configured store on first use, never at start-up.

    A custom handler that has not bound its port yet is a 502, and the host
    does not wait long. Nothing about reaching a storage account belongs on
    that path: a slow token, a firewall, a typo in a container name -- each
    turns into an app that looks dead rather than one that says what is
    wrong.

    So the socket opens immediately and the first call pays for the store.
    A failure is NOT kept: it is remembered with its cause, every call until
    the next attempt raises Unavailable, and the store is tried again after
    RETRY_AFTER seconds. A failure after resolution -- a token that will not
    refresh, a SAS that expired mid-life -- is Unavailable too, for that call.
    Health reports which store is in use, or what went wrong.
    """

    RETRY_AFTER = 5.0

    def __init__(self, factory, wanted=None, detail=None):
        self._factory = factory
        self._wanted = wanted or "configured store"
        self._intended = dict(detail or {})
        self._store = None
        self._error = None
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def _resolve(self):
        if self._store is not None:
            return self._store
        with self._lock:
            if self._store is not None:
                return self._store
            now = time.monotonic()
            if self._error and now < self._retry_at:
                raise Unavailable(self._error)
            try:
                self._store = self._factory()
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                # From when the attempt FAILED: a slow failure (a 15 s
                # timeout) timed from its start has an expired window, and
                # every queued call would wait out an attempt of its own.
                self._retry_at = time.monotonic() + self.RETRY_AFTER
                print(f"WARNING  replay state ({self._wanted}) unavailable: "
                      f"{self._error.rstrip('.')}. Every MCP call is answered "
                      f"with an error until it works; retrying in "
                      f"{self.RETRY_AFTER:g} s.", flush=True)
                raise Unavailable(self._error) from exc
            self._error = None
            return self._store

    def _use(self, method, *args):
        store = self._resolve()
        try:
            result = getattr(store, method)(*args)
        except Conflict:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if error != self._error:
                print(f"WARNING  replay state ({self._wanted}) {method} "
                      f"failed: {error}. The call is answered with an error.",
                      flush=True)
            self._error = error
            raise Unavailable(error) from exc
        if self._error:
            print(f"WARNING  replay state ({self._wanted}) working again",
                  flush=True)
        self._error = None
        return result

    def load(self, key):
        return self._use("load", key)

    def save(self, key, state, version):
        return self._use("save", key, state, version)

    def close(self):
        if self._store is not None:
            self._store.close()

    @property
    def backend(self):
        if self._store is not None:
            return type(self._store).__name__
        return "unavailable" if self._error else "unresolved"

    @property
    def detail(self):
        if self._store is not None:
            detail = dict(getattr(self._store, "detail", {}) or {})
        else:
            detail = dict(self._intended, wanted=self._wanted)
        if self._error:
            detail["error"] = self._error
        return detail

    def resolve(self):
        """Resolve now, for /health?resolve=1. Never raises; says what it got.

        A store that resolved and has failed since is asked again, so health
        does not report a working store from a success that is long gone."""
        try:
            if self._store is not None and self._error:
                self._use("load", "__health__")
            else:
                self._resolve()
        except Unavailable:
            pass
        return self.backend


def open_store(account_url=None, container=None, sas_url=None,
               client_id=None):
    """The durable store the configuration names, or a MemoryStore.

    Precedence: a SAS when one is configured (a deployment that could not
    assign roles), else the managed identity when an account and container
    are named, else in-process for a local run.

    Configured means durable or nothing: a store that cannot be reached
    raises Unavailable on use, and the server answers with an error. It is
    never swapped for in-process state.
    """
    if sas_url:
        return LazyStore(lambda: SasBlobStore(sas_url), "SasBlobStore",
                         {"auth": "container SAS",
                          "expires": sas_expiry(sas_url)})
    if account_url and container:
        return LazyStore(
            lambda: IdentityBlobStore(account_url, container, client_id),
            "IdentityBlobStore",
            {"auth": "managed identity",
             "client_id": client_id or "system-assigned"})
    return MemoryStore()
