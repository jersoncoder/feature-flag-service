# HTTP API

The server also publishes an OpenAPI document at `/openapi.json` and an
interactive page at `/docs`. This page covers what the schema cannot show:
credentials, preconditions, caching and the error format.
[`examples/walkthrough.md`](../examples/walkthrough.md) shows real requests and
responses.

## Credentials

Every endpoint except `/healthz` needs `Authorization: Bearer <token>`. There
are three kinds of token, and each reaches only its own endpoints:

| token      | where it comes from                         | reaches                                 |
|------------|---------------------------------------------|-----------------------------------------|
| admin key  | the `FLAGS_ADMIN_KEY` environment variable  | everything under `/v1/projects`         |
| server key | `POST .../environments/{env}/keys`, `"kind": "server"` | `POST /v1/evaluate`          |
| SDK key    | `POST .../environments/{env}/keys`, `"kind": "sdk"`    | `GET /v1/environments/{id}/config` |

A missing, unknown or revoked token gets **401** with `WWW-Authenticate:
Bearer`; a valid token of the wrong kind gets **403**. Server and SDK keys
belong to one environment and look like
`sdk.env_5a1f0c3e9b2d.key_83c2e4f6a0b1.<43 random characters>`: kind,
environment id, key id and secret. The environment id lets the SDK find its
config URL from the key alone; the key id is how the server finds the record
and what the audit log shows. The token is returned once, by the request that
creates it; the server stores only its SHA-256 and compares hashes with
`hmac.compare_digest`. A key is revoked with `DELETE .../keys/{key_id}`, and
deleting an environment deletes its keys.

## Endpoints

Management (admin key). `{project}`, `{env}` and `{flag}` are keys: lowercase
letters, digits, `_` and `-`, up to 64 characters.

| method   | path                                                  | notes                                              |
|----------|-------------------------------------------------------|----------------------------------------------------|
| `POST`   | `/v1/projects`                                        | 201; 409 if the key exists                         |
| `GET`    | `/v1/projects`                                        |                                                    |
| `GET`    | `/v1/projects/{project}`                              |                                                    |
| `PATCH`  | `/v1/projects/{project}`                              | rename                                             |
| `DELETE` | `/v1/projects/{project}`                              | 204; 409 while it has environments or flags        |
| `POST`   | `/v1/projects/{project}/environments`                 | 201; gives every flag settings for it (see below)  |
| `GET`    | `/v1/projects/{project}/environments`                 |                                                    |
| `GET`    | `/v1/projects/{project}/environments/{env}`           | includes the environment's public `id`             |
| `PATCH`  | `/v1/projects/{project}/environments/{env}`           | rename                                             |
| `DELETE` | `/v1/projects/{project}/environments/{env}`           | 204; deletes its keys and every flag's settings    |
| `POST`   | `/v1/projects/{project}/environments/{env}/keys`      | 201; `{"kind": "server" or "sdk", "name": ...}`   |
| `GET`    | `/v1/projects/{project}/environments/{env}/keys`      | without tokens                                     |
| `DELETE` | `/v1/projects/{project}/environments/{env}/keys/{id}` | 204; revoking twice is not an error                |
| `POST`   | `/v1/projects/{project}/flags`                        | 201 with `ETag`                                    |
| `GET`    | `/v1/projects/{project}/flags`                        |                                                    |
| `GET`    | `/v1/projects/{project}/flags/{flag}`                 | `ETag`                                             |
| `PUT`    | `/v1/projects/{project}/flags/{flag}`                 | `If-Match`; replaces description and variants      |
| `PUT`    | `/v1/projects/{project}/flags/{flag}/environments/{env}` | `If-Match`; replaces switch, default and rules  |
| `DELETE` | `/v1/projects/{project}/flags/{flag}`                 | `If-Match`; 204                                    |
| `GET`    | `/v1/projects/{project}/audit`                        | `flag`, `limit` (1 to 200, default 50), `cursor`   |

Runtime:

| method | path                                  | key    | notes                                       |
|--------|---------------------------------------|--------|---------------------------------------------|
| `POST` | `/v1/evaluate`                        | server | `{"flag": ..., "context": {...}}`           |
| `GET`  | `/v1/environments/{id}/config`        | SDK    | `ETag`, `If-None-Match`, 304                |
| `GET`  | `/healthz`                            | none   | runs `SELECT 1` on the database             |

`POST /v1/evaluate` answers 200 for any flag key, like the SDK: a missing flag
is `"reason": "FLAG_NOT_FOUND"`, not a 404. The `{id}` of the config endpoint is
the environment's public id (`env_...`); an SDK key of another environment gets
403.

## Flags

A flag has a key and a type (`boolean`, `string`, `number` or `json`), both
fixed at creation, a description, a list of variants and, for every
environment of its project, a settings object:

```json
{
  "key": "button-color",
  "type": "string",
  "variants": [{"key": "blue", "value": "#1d4ed8"}, {"key": "green", "value": "#15803d"}],
  "environments": {
    "production": {
      "enabled": true,
      "default_variant": "blue",
      "rules": [
        {"conditions": [{"attribute": "plan", "operator": "in", "value": ["pro", "team"]}],
         "variant": "green"},
        {"split": [{"variant": "green", "weight": 10}, {"variant": "blue", "weight": 90}]}
      ]
    }
  }
}
```

`variants` may be left out for a boolean flag, which then gets `off` (false)
and `on` (true). Environments left out of `environments` at creation start
switched off, serving the first variant, with no rules. The rule language is
described in [evaluation.md](evaluation.md).

## Optimistic concurrency

Every flag has a `version`, starting at 1, and every response that returns a
flag carries it as a strong ETag, `"<flag id>-<version>"`. The flag id is never
reused, so a flag deleted and created again under the same key does not accept
ETags of its predecessor.

`PUT` and `DELETE` on a flag need `If-Match` with the current ETag:

* no `If-Match`: **428 Precondition Required**. `If-Match: *` is refused the
  same way, because it would turn the update into a blind write;
* an ETag that is not the current one (weak ETags never match): **412
  Precondition Failed**, with the current version in the body and the current
  ETag in the `ETag` header:

  ```json
  {"detail": "the flag has changed since it was read", "current_version": 2,
   "current_etag": "\"1-2\""}
  ```

* a header that cannot be parsed as a list of entity tags: **400**.

Each write runs in one SQLite `BEGIN IMMEDIATE` transaction that compares the
ETag, writes, increments the version and appends the audit entry, so of two
writers holding the same ETag exactly one succeeds.

The version is per flag, not per environment: it changes with any change to
the flag, including its settings in any environment, and when an environment
is added to or removed from the project (the flag's representation lists the
settings of every environment).

## The SDK config

`GET /v1/environments/{id}/config` returns every flag of the environment in the
form the SDK evaluates:

```json
{"environment": {"id": "env_...", "key": "production", "project": "shop"},
 "flags": {"new-checkout": {"key": "new-checkout", "type": "boolean",
   "salt": "...", "version": 2, "variants": [...], "enabled": true,
   "default_variant": "off", "rules": [...]}}}
```

The body is serialised deterministically (sorted keys, no whitespace) and its
ETag is a hash of the body, so it changes exactly when the content does. A
request with `If-None-Match` naming the current ETag (weak comparison, `*`
allowed) gets **304** with no body. Responses carry `Cache-Control: no-cache`.

## Errors

Errors have a `detail` field. For 422 it is a list in FastAPI's format, whether
the problem was found while parsing the body or while checking it against
stored state:

```json
{"detail": [{"type": "value_error", "loc": ["body", "rules", 0, "variant"],
             "msg": "unknown variant 'maybe'; the flag defines: off, on"}]}
```

What gives 422: malformed keys, unknown fields, a value that does not match
the flag type, duplicate variant keys, a variant used twice in one split, split
weights that do not add up to 100 or are not multiples of 0.01, a rule with both
or neither of `variant` and `split`, a condition value of the wrong kind for its
operator (or an invalid SemVer), a rule or default naming an unknown variant,
settings for an environment the project does not have, and removing a variant
that some environment still uses. A duplicate project, environment or flag key
is a 409.

Checks run in this order: credentials (401, 403), `If-Match` (428, 400), body
structure (422), existence (404, 409), version (412), then the checks against
stored state (422). A refused request changes nothing and writes no audit entry.

## Audit log

Every change is recorded in the same transaction as the change itself: actor
(`admin`, since only the admin key can change anything), time, action, flag and
environment keys where relevant, and the full JSON of the resource before and
after. API key entries never contain the token.

`GET /v1/projects/{project}/audit` lists entries newest first, optionally only
those of one flag. Pages are cut by entry id, not by offset, so a page never
repeats or skips entries while new ones arrive. Pass `next_cursor` back as
`cursor` for the next page; it is null on the last one. The cursor is opaque:
clients should not build or parse it.

Actions: `project.created`, `project.updated`, `project.deleted`,
`environment.created`, `environment.updated`, `environment.deleted`,
`api_key.created`, `api_key.revoked`, `flag.created`, `flag.updated`,
`flag.environment_updated`, `flag.environment_added`,
`flag.environment_removed`, `flag.deleted`.
