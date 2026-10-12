# Policy, approvals and consequential actions

Implementation: `src/omca/policy/`. Schema validation never establishes permission (§14): every
operation is allowlisted, its arguments are validated against a restricted schema, and a classifier
inspects the target, destination and current observation.

## Categories and default treatment (§15)

| Category | Examples | Verdict |
|---|---|---|
| `READ` | search API, page retrieval, screenshots, observation, image fetch, wait | Allow within task and resource scope |
| `NAVIGATION` | links with a real `href`, tabs, `<summary>`, disclosure buttons (`aria-expanded`) outside forms, scrolling, navigation keys | Allow |
| `DRAFT` | typing into ordinary text fields, checkboxes/selects, local email/calendar previews | Allow (no external effect) |
| `CREATE_OUTPUT` | new report in the reports folder, new workbook copy | Allow; never overwrites |
| `CONSEQUENTIAL` | form submission (any submit control inside a form, Enter in a form field, Ctrl+Enter, Ctrl+S), send, purchase, delete, publish, share, invite, permission change, overwrite | **Approval** of the exact action |
| `UNKNOWN_MUTATION` | buttons with no recognisable consequence, canvas clicks, app keyboard shortcuts outside text fields, toggles, raw desktop input | **Approval** (or a narrow standing rule) until a reviewed adapter exists |
| `FORBIDDEN` | credential entry or export, unexpected recipients, `javascript:`/`file:`/`data:` URLs, private hosts not allowed, unknown operations | **Deny**; never inferred approval |

Element names come from page content and are untrusted: they can only make a classification *stricter*
(e.g. a link named "Delete account" is consequential); a label like "Next" on a submit button does not make
it navigation.

## Approval binding

An approval binds: owner, task, action id, operation, arguments (minus ephemeral element refs), target
fingerprint, destination, preconditions (page origin + element fingerprint/role/name, or file/draft
hashes), policy version, expiry (15 min) and a random nonce. The binding is HMAC-signed with the approval
signing key and the nonce is consumed in one database transaction. Before execution the coordinator
re-observes, re-resolves the element by fingerprint and recomputes the preconditions; any change in
inputs, destination, state, owner, policy version, signature or expiry refuses the action
(`INVALIDATED`/`EXPIRED`) and the owner is told. Approvers are authenticated separately from ordinary bot
access (`owner.approver_user_ids`).

While waiting for a decision the task releases the VM's UI lease (other tasks may use the desktop); the
revalidation step is what makes that safe.

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

Every rule must name an operation and at least one scope (origin, recipient, path prefix or element
pattern). Rules never cover `FORBIDDEN` actions. Recurring schedules never grant new permissions.

## Recipients

Sends and invitations may only target `policy.contacts`. Anything else is `FORBIDDEN` - the owner is not
even asked, so a compromised page or model cannot obtain an approval prompt for an attacker's address.

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
