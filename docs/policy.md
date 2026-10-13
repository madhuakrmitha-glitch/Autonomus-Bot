# Policy, approvals and consequential actions

Implementation: `src/omca/policy/`. Schema validation never establishes permission (§14): every
operation is allowlisted, its arguments are validated against a restricted schema, and a classifier
inspects the target, destination and current observation.

## Categories and default treatment (§15)

| Category | Examples | Verdict |
|---|---|---|
| `READ` | search API, page retrieval, screenshots, observation, image fetch, wait | Allow within task and resource scope |
| `NAVIGATION` | links to another page, tabs, `<summary>`, disclosure buttons (`aria-expanded`) outside forms, scrolling, navigation keys | Allow |
| `DRAFT` | typing into ordinary text fields, checkboxes/selects, local email/calendar previews | Allow (no external effect) |
| `CREATE_OUTPUT` | new report in the reports folder, new workbook copy | Allow; never overwrites |
| `CONSEQUENTIAL` | form submission (any submit control inside a form, Enter in a form field, Ctrl+Enter, Ctrl+S), send, purchase, delete, publish, share, invite, permission change, overwrite | **Approval** of the exact action |
| `UNKNOWN_MUTATION` | buttons with no recognisable consequence, same-page links (`#...`, which a script handles), canvas clicks, app keyboard shortcuts outside text fields, toggles, an image fetch *with the session's cookies* from a state-changing URL, raw desktop input | **Approval** (or a narrow standing rule) until a reviewed adapter exists |
| `FORBIDDEN` | credential entry or export, unexpected recipients, `javascript:`/`file:`/`data:` URLs, private hosts not allowed, unknown operations | **Deny**; never inferred approval |

Element names come from page content and are untrusted: they can only make a classification *stricter*
(e.g. a link named "Delete account" is consequential); a label like "Next" on a submit button does not make
it navigation.

## Approval binding

An approval binds: owner, task, action id, operation, arguments (minus ephemeral element refs), target
fingerprint, destination, preconditions (page origin and URL + element fingerprint/role/name, or
file/draft hashes), policy version, expiry (15 min) and a random nonce. The binding is HMAC-signed with
the approval signing key and the nonce is consumed in one database transaction. Before execution the
coordinator re-observes the live page, re-resolves the element by fingerprint (for a key press: the
element that has focus now; for a coordinate click: the element under the point now), classifies the
action again against that live state and recomputes the preconditions. A live verdict of *deny*, a
different destination, or any change in inputs, state, owner, policy version, signature or expiry refuses
the action (`INVALIDATED`/`EXPIRED`) and the owner is told. The worker checks the target once more
immediately before acting (focused element, element under the point, element fingerprint). Approvers are
authenticated separately from ordinary bot access (`owner.approver_user_ids`).

An action may only use artifacts the task produced or the owner attached to it: a draft from another
task cannot be sent by this one. The email approval shows the recipients, subject and body exactly as
drafted.

While waiting for a decision the task releases the VM's UI lease (other tasks may use the desktop); the
revalidation step is what makes that safe.

## Hands-off browsing and saved logins

Two opt-in settings remove the prompts for browser work:

```yaml
policy:
  auto_approve_browser: true      # consequential / unknown-effect browser.* actions run without approval
browser:
  credentials:
    - {id: github, origin: https://github.com, username_ref: "keyring:omca/github-user", password_ref: "keyring:omca/github-pass"}
```

* `auto_approve_browser` only turns `REQUIRE_APPROVAL` into `ALLOW` for `browser.*` operations. `FORBIDDEN`
  verdicts (private hosts, `javascript:` URLs, unknown operations, credential export) still deny, and email,
  messages, file deletes and permission changes still ask.
* The model never receives a password. It types `{{cred:ID.password}}`; the gate allows that only into a
  password field on the credential's exact origin, and the worker substitutes the secret from its own secret
  store against the live page origin. Placeholders, not values, are what the journal and approvals record.
  Literal text typed into a credential field is still `FORBIDDEN`.
* With the Browser Use engine the same credentials are handed to the agent as `sensitive_data` keyed by
  origin (`<secret>ID_username</secret>`, `<secret>ID_password</secret>`), and `browser.agent.require_approval:
  false` removes the per-run prompt.
* Trade-off: with auto-approval a prompt-injected page can make the agent click or submit on any site,
  and a saved login is usable by the agent on its own origin. Use a dedicated browser profile and accounts
  with limited rights.

## Standing rules

Narrow pre-authorisations for repeated, low-risk mutations, e.g. dismissing one site's cookie banner:

```yaml
policy:
  standing_rules:
    - id: docs-cookies
      description: Dismiss the cookie banner on the docs site
      operation: browser.click
      origin: https://docs.example.com
      element_name_pattern: "Accept( all)? cookies"
      expires_at: 2026-12-31T00:00:00Z
      max_uses: 50
```

Every rule must name an operation and at least one scope: an origin, a recipient or a path prefix. An
element pattern only narrows a rule further, because page content chooses element names. Path prefixes
are compared after normalisation, and paths containing `..` never match. Rules never cover `FORBIDDEN`
actions. Recurring schedules never grant new permissions.

## Recipients

Sends and invitations may only target `policy.contacts`. Anything else is `FORBIDDEN` - the owner is not
even asked, so a compromised page or model cannot obtain an approval prompt for an attacker's address.
The worker sends with exactly the approved addresses as the SMTP envelope and refuses a draft that carries
`Cc`, `Bcc` or `Resent-*` recipients.

## Network destinations

The assistant does not reach the owner's private network unless allowed: hosts in
`search.allowed_private_hosts` (coordinator fetches) and origins in a browser attachment's
`allowed_private_origins` (that worker's browser and media downloads). Private hosts are recognised in
every spelling (`127.1`, `2130706433`, `0x7f.0.0.1`, IPv4-mapped IPv6, `*.localhost`, `*.local`,
single-label names), and URLs with embedded credentials or backslashes are refused. Fetchers also resolve
names and refuse ones that point to private addresses, and they follow redirects themselves, checking
every hop. Browser task pages refuse requests to private hosts, and the landing URL is checked after every
action because the browser follows HTTP redirects itself.

## Prompt injection (AT15)

Page text, images, transcripts and files are wrapped as `<untrusted_source>` data in prompts. They cannot
change routes, budgets, permissions or recipients because none of those are reachable from model output:
model routes and budgets are configuration, recipients are checked against the contact list, and every
action passes the deterministic gate. Statements that read like instructions to the assistant are marked
*disputed* in reports with a limitation note, never presented as findings.

## Retries

Default action retries are limited to two, only for read-only operations, after re-observation. Permission
denials, expired approvals, budget refusals, unsupported capabilities and uncertain side effects are never
retried automatically (§8).
