# Evaluation semantics

Everything here is implemented once, in `src/flagservice/core/`, and used by
both the server (`POST /v1/evaluate`) and the SDK (`FlagClient`).

## Inputs and result

`evaluate(flag, context)` takes one flag's configuration in one environment and
a context: a mapping from attribute names to values, such as
`{"key": "user-42", "country": "DE", "app_version": "2.3.0"}`. It returns:

| field        | meaning                                                         |
|--------------|-----------------------------------------------------------------|
| `variant`    | the key of the variant served, or null                          |
| `value`      | that variant's value, or null                                   |
| `reason`     | why (table below)                                               |
| `rule_index` | position of the rule that matched, or null                      |
| `error`      | a message when `reason` is `ERROR`, otherwise null              |

It never raises. A corrupted config or a context that is not a mapping gives
`ERROR`.

## Order of evaluation

1. No flag with that key in the environment: `FLAG_NOT_FOUND`, no value.
2. The flag is switched off in the environment: `DISABLED`, no value. Callers
   use their own default; the SDK getters do this for you.
3. Rules are tried in order. A rule matches when **all** its conditions hold (a
   rule with no conditions matches everyone). The first matching rule decides:
   * a rule with `variant` serves that variant: `TARGETING_MATCH`;
   * a rule with `split` places the context in a bucket and serves the variant
     that owns the bucket: `SPLIT`. If the context has no usable value for the
     rule's bucketing attribute, the rule is skipped and evaluation goes on with
     the next rule, so anonymous traffic never joins a rollout by accident.
4. No rule matched: the environment's `default_variant` is served: `DEFAULT`.

A rule or default that names a variant the flag does not define gives `ERROR`.
The server refuses such configs on write (422), so this only happens with a
corrupted database or a hand-edited config.

## Operators

All comparisons are case-sensitive and type-strict. A condition on an attribute
the context does not have never matches, **including `neq` and `not_in`**, and
neither does a condition on an attribute whose value is a list, an object or
null. Context attributes are therefore flat scalars: strings, numbers and
booleans.

| operator                    | condition value                 | matches when the attribute ...                         |
|-----------------------------|---------------------------------|--------------------------------------------------------|
| `eq`                        | string, number or boolean       | equals the value                                       |
| `neq`                       | string, number or boolean       | is a scalar that does not equal the value              |
| `in`                        | list of up to 1000 scalars      | equals one of them                                     |
| `not_in`                    | list of up to 1000 scalars      | is a scalar that equals none of them                   |
| `contains`                  | string                          | is a string that contains the value                    |
| `starts_with`               | string                          | is a string that starts with the value                 |
| `lt`, `lte`, `gt`, `gte`    | finite number                   | is a number (not a boolean) that compares as stated    |
| `semver_eq`, `semver_lt`, `semver_lte`, `semver_gt`, `semver_gte` | SemVer 2.0.0 string | is a valid SemVer string that compares as stated |

Equality is type-strict: `1` equals `1.0`, but `"1"` does not equal `1` and
`true` does not equal `1` (Python's `True == 1` is deliberately not followed).
`NaN` never satisfies a numeric comparison.

SemVer comparison follows the precedence rules of
[semver.org](https://semver.org) section 11: `1.0.0-alpha < 1.0.0-alpha.1 <
1.0.0-beta < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0`, numeric parts compare as
numbers (`1.10.0 > 1.9.0`) and build metadata is ignored, so `1.2.3+a`
`semver_eq` `1.2.3`. Strings that are not strict SemVer (`1.2`, `v1.2.3`,
`01.2.3`) never match; the server refuses them as condition values.

## Percentage splits

A split lists variants with weights in percent, with a resolution of 0.01, that
must add up to exactly 100:

```json
{"split": [{"variant": "on", "weight": 12.5}, {"variant": "off", "weight": 87.5}],
 "bucket_by": "key"}
```

`bucket_by` names the attribute to bucket on (`key` when omitted). Bucketing
by an organisation id instead of a user id, for example, gives everyone in an
organisation the same variant.

### Bucketing

The context is mapped to one of 10,000 buckets:

```
digest = SHA-256( UTF-8( flag_key + ":" + salt + ":" + value ) )
bucket = (first 8 bytes of digest, as a big-endian unsigned integer) mod 10000
```

* `salt` is 16 random hex characters chosen when the flag is created. It never
  changes, and it makes two flags with the same rollout percentage pick
  independent sets of users.
* `value` is the bucketing attribute: a string as it is, an integer in decimal
  (`42` and `"42"` share a bucket). Booleans, floats, empty strings and missing
  attributes cannot be bucketed, which skips the rule as described above.
* Flag keys and salts cannot contain `:`, so the hashed string is unambiguous.
* `2**64` is not a multiple of 10,000, so buckets 0 to 1615 have one more
  preimage than the others out of about 1.8e15: a bias near 5e-16.

The split's entries take consecutive bucket ranges in the order they are
listed, starting at 0, with one bucket per 0.01%: for the example above, `on`
owns buckets 0 to 1249 and `off` owns 1250 to 9999.

Reference values, checked by `tests/test_bucketing.py` (another implementation
of the formula must reproduce them):

| flag key       | salt               | value    | bucket |
|----------------|--------------------|----------|--------|
| `new-checkout` | `5f2c8a91d3e4b607` | `user-1` | 4900   |
| `new-checkout` | `5f2c8a91d3e4b607` | `user-2` | 9013   |
| `dark-mode`    | `00`               | `42`     | 4975   |

### Consequences

* **Stable.** The same flag and value always land in the same bucket, in every
  process and on every machine, whatever other attributes the context has. The
  salt belongs to the flag, so a user in the first 10% in staging is also in the
  first 10% in production, and in every rule of the flag that buckets on the
  same attribute.
* **Monotonic rollouts.** Raising a variant's weight keeps everyone who already
  had it when the extra weight comes only from entries on one side of it,
  because one end of its bucket range stays put. With two variants that is
  always the case: going from `on 10 / off 90` to `on 20 / off 80` keeps all of
  the first 10% and adds 10% more. With three or more variants, taking weight
  from entries on both sides moves the range and reassigns some users.
* **What reassigns users.** Reordering split entries, changing `bucket_by`, and
  deleting and recreating the flag (a new salt).

## In the SDK

`FlagClient.evaluate(flag_key, context)` returns the full result. The typed
getters `bool_value`, `string_value`, `number_value` and `json_value` take
`(flag_key, context, default)` and return the default whenever the result is
not one of `TARGETING_MATCH`, `SPLIT` or `DEFAULT`, when the flag's type does not
match the getter (`bool_value` on a `string` flag), before the first config has
loaded, and on any unexpected exception. They never raise. `number_value`
returns a `float`; `json_value` returns a deep copy, so mutating it cannot
change the cached config.

The SDK parses each flag of the published config on its own. A flag it cannot
read, for instance one that uses an operator added in a newer server, evaluates
to `ERROR` (so the getters return the default) while the other flags keep
working.
