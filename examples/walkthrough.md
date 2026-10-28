# API walkthrough

This is the output of [`walkthrough.sh`](walkthrough.sh), run against a server
started on an empty database, followed by the output of
[`sdk_example.py`](sdk_example.py) against the same server. API key secrets are
replaced by `<secret>`; ids, salts, timestamps and ETags change on every run.

```bash
export FLAGS_ADMIN_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
FLAGS_DATABASE=/tmp/walkthrough.db python -m flagservice.server --port 8765 &
BASE=http://127.0.0.1:8765 ./examples/walkthrough.sh
```

## Create a project and two environments

```console
$ curl -X POST "$BASE/v1/projects" -H "Authorization: Bearer $FLAGS_ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"key": "shop", "name": "Web shop"}'
HTTP/1.1 201 Created
location: /v1/projects/shop
{
  "key": "shop",
  "name": "Web shop",
  "created_at": "2026-10-28T08:13:33.190Z",
  "updated_at": "2026-10-28T08:13:33.190Z"
}
```

## List the environments (each has a public id)

```console
$ curl "$BASE/v1/projects/shop/environments" -H "Authorization: Bearer $FLAGS_ADMIN_KEY"
HTTP/1.1 200 OK
[
  {
    "id": "env_de7cced74453",
    "project": "shop",
    "key": "production",
    "name": "production",
    "created_at": "2026-10-28T08:13:33.266Z",
    "updated_at": "2026-10-28T08:13:33.266Z"
  },
  {
    "id": "env_dc1852c6815c",
    "project": "shop",
    "key": "staging",
    "name": "staging",
    "created_at": "2026-10-28T08:13:33.250Z",
    "updated_at": "2026-10-28T08:13:33.250Z"
  }
]
```

## Create a server key for production (the token is shown only here)

```console
$ curl -X POST "$BASE/v1/projects/shop/environments/production/keys" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"kind": "server", "name": "checkout backend"}'
HTTP/1.1 201 Created
{
  "id": "key_67436f25537e",
  "kind": "server",
  "name": "checkout backend",
  "environment": "production",
  "created_at": "2026-10-28T08:13:33.368Z",
  "revoked_at": null,
  "token": "srv.env_de7cced74453.key_67436f25537e.<secret>"
}
```

## Create an SDK key for production

```console
$ curl -X POST "$BASE/v1/projects/shop/environments/production/keys" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"kind": "sdk", "name": "web app"}'
HTTP/1.1 201 Created
{
  "id": "key_2d32425f15c9",
  "kind": "sdk",
  "name": "web app",
  "environment": "production",
  "created_at": "2026-10-28T08:13:33.478Z",
  "revoked_at": null,
  "token": "sdk.env_de7cced74453.key_2d32425f15c9.<secret>"
}
```

## Create a boolean flag (variants default to off and on)

```console
$ curl -X POST "$BASE/v1/projects/shop/flags" -H "Authorization: Bearer $FLAGS_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key": "new-checkout", "type": "boolean", "description": "One-page checkout"}'
HTTP/1.1 201 Created
etag: "1-1"
location: /v1/projects/shop/flags/new-checkout
{
  "key": "new-checkout",
  "type": "boolean",
  "description": "One-page checkout",
  "salt": "34776a3d8336e22f",
  "version": 1,
  "variants": [
    {
      "key": "off",
      "value": false
    },
    {
      "key": "on",
      "value": true
    }
  ],
  "environments": {
    "production": {
      "enabled": false,
      "default_variant": "off",
      "rules": []
    },
    "staging": {
      "enabled": false,
      "default_variant": "off",
      "rules": []
    }
  },
  "created_at": "2026-10-28T08:13:33.603Z",
  "updated_at": "2026-10-28T08:13:33.603Z"
}
```

## Turn it on in production: staff always, 10% of everyone else (needs If-Match)

```console
$ curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/production" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -H "If-Match: $ETAG" -d '{
    "enabled": true,
    "default_variant": "off",
    "rules": [
      {"conditions": [{"attribute": "email", "operator": "contains", "value": "@shop.example"}],
       "variant": "on"},
      {"split": [{"variant": "on", "weight": 10}, {"variant": "off", "weight": 90}]}
    ]
  }'
HTTP/1.1 200 OK
etag: "1-2"
{
  "key": "new-checkout",
  "type": "boolean",
  "description": "One-page checkout",
  "salt": "34776a3d8336e22f",
  "version": 2,
  "variants": [
    {
      "key": "off",
      "value": false
    },
    {
      "key": "on",
      "value": true
    }
  ],
  "environments": {
    "production": {
      "enabled": true,
      "default_variant": "off",
      "rules": [
        {
          "conditions": [
            {
              "attribute": "email",
              "operator": "contains",
              "value": "@shop.example"
            }
          ],
          "variant": "on"
        },
        {
          "conditions": [],
          "split": [
            {
              "variant": "on",
              "weight": 10.0
            },
            {
              "variant": "off",
              "weight": 90.0
            }
          ]
        }
      ]
    },
    "staging": {
      "enabled": false,
      "default_variant": "off",
      "rules": []
    }
  },
  "created_at": "2026-10-28T08:13:33.603Z",
  "updated_at": "2026-10-28T08:13:33.708Z"
}
```

## A second writer still holding the old ETag gets 412 and the current version

```console
$ curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/production" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -H "If-Match: $ETAG" -d '{"enabled": false, "default_variant": "off"}'
HTTP/1.1 412 Precondition Failed
etag: "1-2"
{
  "detail": "the flag has changed since it was read",
  "current_version": 2,
  "current_etag": "\"1-2\""
}
```

## Without If-Match the server answers 428

```console
$ curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/production" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"enabled": false, "default_variant": "off"}'
HTTP/1.1 428 Precondition Required
{
  "detail": "this change needs an If-Match header with the flag's current ETag"
}
```

## With the current ETag, a rule that serves an unknown variant is refused with 422

```console
$ curl -X PUT "$BASE/v1/projects/shop/flags/new-checkout/environments/staging" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY" -H "Content-Type: application/json" \
  -H "If-Match: $ETAG" \
  -d '{"enabled": true, "default_variant": "off", "rules": [{"variant": "maybe"}]}'
HTTP/1.1 422 Unprocessable Content
{
  "detail": [
    {
      "type": "value_error",
      "loc": [
        "body",
        "rules",
        0,
        "variant"
      ],
      "msg": "unknown variant 'maybe'; the flag defines: off, on"
    }
  ]
}
```

## Evaluate on the server: a staff member matches the first rule

```console
$ curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"flag": "new-checkout", "context": {"key": "user-17", "email": "ana@shop.example"}}'
HTTP/1.1 200 OK
{
  "flag": "new-checkout",
  "variant": "on",
  "value": true,
  "reason": "TARGETING_MATCH",
  "rule_index": 0,
  "error": null
}
```

## Anyone else is bucketed by key into the 10% split

```console
$ curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"flag": "new-checkout", "context": {"key": "user-42", "email": "bo@mail.example"}}'
HTTP/1.1 200 OK
{
  "flag": "new-checkout",
  "variant": "off",
  "value": false,
  "reason": "SPLIT",
  "rule_index": 1,
  "error": null
}
```

## Without a key the split cannot place the context, so the default is served

```console
$ curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SERVER_KEY" \
  -H "Content-Type: application/json" -d '{"flag": "new-checkout", "context": {}}'
HTTP/1.1 200 OK
{
  "flag": "new-checkout",
  "variant": "off",
  "value": false,
  "reason": "DEFAULT",
  "rule_index": null,
  "error": null
}
```

## An SDK key cannot call the evaluation endpoint

```console
$ curl -X POST "$BASE/v1/evaluate" -H "Authorization: Bearer $SDK_KEY" \
  -H "Content-Type: application/json" -d '{"flag": "new-checkout", "context": {}}'
HTTP/1.1 403 Forbidden
{
  "detail": "this endpoint needs a server key, not an SDK key"
}
```

## The SDK downloads the whole ruleset of its environment

```console
$ curl "$BASE/v1/environments/$ENV_ID/config" -H "Authorization: Bearer $SDK_KEY"
HTTP/1.1 200 OK
etag: "bcabcc010292fe75729e7a99707c5998"
cache-control: no-cache
{
  "environment": {
    "id": "env_de7cced74453",
    "key": "production",
    "project": "shop"
  },
  "flags": {
    "new-checkout": {
      "default_variant": "off",
      "enabled": true,
      "key": "new-checkout",
      "rules": [
        {
          "conditions": [
            {
              "attribute": "email",
              "operator": "contains",
              "value": "@shop.example"
            }
          ],
          "variant": "on"
        },
        {
          "conditions": [],
          "split": [
            {
              "variant": "on",
              "weight": 10.0
            },
            {
              "variant": "off",
              "weight": 90.0
            }
          ]
        }
      ],
      "salt": "34776a3d8336e22f",
      "type": "boolean",
      "variants": [
        {
          "key": "off",
          "value": false
        },
        {
          "key": "on",
          "value": true
        }
      ],
      "version": 2
    }
  }
}
```

## and polls with If-None-Match; nothing changed, so 304 and no body

```console
$ curl "$BASE/v1/environments/$ENV_ID/config" -H "Authorization: Bearer $SDK_KEY" \
  -H "If-None-Match: $CONFIG_ETAG"
HTTP/1.1 304 Not Modified
etag: "bcabcc010292fe75729e7a99707c5998"
cache-control: no-cache
```

## The audit log, newest first, one entry per page

```console
$ curl "$BASE/v1/projects/shop/audit?flag=new-checkout&limit=1" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY"
HTTP/1.1 200 OK
{
  "items": [
    {
      "id": 7,
      "at": "2026-10-28T08:13:33.709Z",
      "actor": "admin",
      "action": "flag.environment_updated",
      "flag": "new-checkout",
      "environment": "production",
      "before": {
        "key": "new-checkout",
        "type": "boolean",
        "description": "One-page checkout",
        "salt": "34776a3d8336e22f",
        "version": 1,
        "variants": [
          {
            "key": "off",
            "value": false
          },
          {
            "key": "on",
            "value": true
          }
        ],
        "environments": {
          "production": {
            "enabled": false,
            "default_variant": "off",
            "rules": []
          },
          "staging": {
            "enabled": false,
            "default_variant": "off",
            "rules": []
          }
        },
        "created_at": "2026-10-28T08:13:33.603Z",
        "updated_at": "2026-10-28T08:13:33.603Z"
      },
      "after": {
        "key": "new-checkout",
        "type": "boolean",
        "description": "One-page checkout",
        "salt": "34776a3d8336e22f",
        "version": 2,
        "variants": [
          {
            "key": "off",
            "value": false
          },
          {
            "key": "on",
            "value": true
          }
        ],
        "environments": {
          "production": {
            "enabled": true,
            "default_variant": "off",
            "rules": [
              {
                "conditions": [
                  {
                    "attribute": "email",
                    "operator": "contains",
                    "value": "@shop.example"
                  }
                ],
                "variant": "on"
              },
              {
                "conditions": [],
                "split": [
                  {
                    "variant": "on",
                    "weight": 10.0
                  },
                  {
                    "variant": "off",
                    "weight": 90.0
                  }
                ]
              }
            ]
          },
          "staging": {
            "enabled": false,
            "default_variant": "off",
            "rules": []
          }
        },
        "created_at": "2026-10-28T08:13:33.603Z",
        "updated_at": "2026-10-28T08:13:33.708Z"
      }
    }
  ],
  "next_cursor": "YXVkaXQ6Nw"
}
```

## The next page, through the opaque cursor

```console
$ curl "$BASE/v1/projects/shop/audit?flag=new-checkout&limit=1&cursor=$CURSOR" \
  -H "Authorization: Bearer $FLAGS_ADMIN_KEY"
HTTP/1.1 200 OK
{
  "items": [
    {
      "id": 6,
      "at": "2026-10-28T08:13:33.604Z",
      "actor": "admin",
      "action": "flag.created",
      "flag": "new-checkout",
      "environment": null,
      "before": null,
      "after": {
        "key": "new-checkout",
        "type": "boolean",
        "description": "One-page checkout",
        "salt": "34776a3d8336e22f",
        "version": 1,
        "variants": [
          {
            "key": "off",
            "value": false
          },
          {
            "key": "on",
            "value": true
          }
        ],
        "environments": {
          "production": {
            "enabled": false,
            "default_variant": "off",
            "rules": []
          },
          "staging": {
            "enabled": false,
            "default_variant": "off",
            "rules": []
          }
        },
        "created_at": "2026-10-28T08:13:33.603Z",
        "updated_at": "2026-10-28T08:13:33.603Z"
      }
    }
  ],
  "next_cursor": null
}
```

## The same flag from Python

`sdk_example.py` loads the production config once with an SDK key (created like
the one above) and evaluates locally, with no request per flag check:

```console
$ FLAGS_SDK_KEY=sdk.env_... python examples/sdk_example.py http://127.0.0.1:8765
{'key': 'user-17', 'email': 'ana@shop.example'}    on   TARGETING_MATCH rule=0
{'key': 'user-42', 'email': 'bo@mail.example'}     off  SPLIT rule=1
{}                                                 off  DEFAULT rule=None
bool_value(new-checkout): True
bool_value(no-such-flag): False
string_value(new-checkout): n/a
1015 of 10000 generated users are in the 10% rollout
```
