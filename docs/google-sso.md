# Google SSO with CloudFront callbacks

Local implementation, 2026-09-29. Not deployed or live-tested. Ahmed controls
publishing and CloudFormation updates; agent AWS access remains read-only.

## Google client configuration

Keep the URLs from the agreed Google OAuth client screenshot:

| Google setting | Dev value |
|---|---|
| Authorized JavaScript origin | `https://d1s4ipj2j7cyx.cloudfront.net` |
| Authorized redirect URI | `https://d1s4ipj2j7cyx.cloudfront.net/oauth2/idpresponse` |
| Client ID | `946982280728-5ma8bt4h3ct1pihscgd8sa955puqlbae.apps.googleusercontent.com` |

Cognito's web client callback/logout is the CloudFront application root with
trailing slash. The Google callback above is a different endpoint.
Use the organization-owned Google project and Internal audience for Workspace.
The production Workspace launcher is separate, deferred work.

## Flow implemented

The web button selects the Cognito OIDC provider `GoogleWorkspace`. Cognito
redirects through `/oauth2/google/authorize` on CloudFront. The adapter sends
Google the agreed CloudFront redirect URI. Google's response reaches
`/oauth2/idpresponse` on CloudFront and is forwarded to Cognito's fixed callback.
Cognito exchanges the code through `/oauth2/google/token`; the adapter supplies
the same CloudFront redirect URI to Google. Cognito validates the Google tokens
and issues the LMA session. The adapter does not mint tokens or store users.

All bridge endpoints are uncached. Client secret is server-side only and not
logged. Runtime lookup of the application URL from the existing SSM settings
avoids a CloudFormation dependency cycle.

This uses manually configured OIDC endpoints, not Cognito's built-in Google
provider. Deployment validation must confirm Cognito keeps those endpoints and
the full Google flow works; local tests cannot establish that. Do not enable
production SSO-only before that live validation succeeds.

There are three new Lambda functions: the domain/admin guard, the app-client
callback updater, and this OAuth bridge, plus an HTTP API. The bridge invokes
only during sign-in; it does not invoke a summary model.

## Flags and account transition

| Parameter | Normal dev | Temporary dev SSO test | Planned prod |
|---|---|---|---|
| `SsoPoolMode` | `LEGACY` | `ACTIVE` | `ACTIVE` |
| `EnableGoogleSso` | `false` | `true` | `true` |
| `PasswordSignInEnabled` | `true` | `true` | `false` |

`PROVISION` creates the new SSO-compatible pool without switching application
authentication. `ACTIVE` selects the new pool consistently across downstream
stacks. `LEGACY` is the initial state before the new pool exists.

The old required email attribute is immutable, so the implementation adds a
separate pool instead of replacing the old pool. The original pool remains;
the new pool and nested stack have retention policies. Ahmed selected fresh
Google identities/re-invites, not password migration or automatic email linking.

Configure `GoogleAdminEmail` explicitly: the dev example currently uses
`ismail.icanovic@allops.co`, matching the existing AdminEmail. Only this exact
verified company Google identity is automatically added to Admin. Change it
locally if another administrator is intended. Production placeholders must be
filled before deployment.

The guard checks the exact normalized `allops.co` domain and verified email on
first registration and subsequent token generation. SSO-only also rejects
native-user tokens and disables native login paths. Password self-signup remains
closed. Dev administrators can invite password users into the new pool.

The template-created native administrator and its group attachment are retained
when password login is turned off, not deleted. After an SSO-only transition,
turning passwords back on can require reconciling/importing these retained CFN
resources before an update; do not treat that as an unreviewed one-click rollback.
The PROVISION rollback below is intended for the dev test with passwords enabled.

New identities have new Cognito sub IDs. Users must sign in again; old passwords
do not transfer. Historical meetings remain in existing storage and are visible
under current company-wide RBAC, but old VP ownership/user-specific settings do
not automatically transfer. Existing MCP client consumers must use the newly
selected client credentials when the pool changes. Old MCP client is retained.

## Secret and deployment preparation

1. Preserve your existing untracked deployment parameter file and its current
   values; do not replace it wholesale with the redacted committed example.
2. Add the seven SSO parameters from `deploy/params/dev.json` to that local file.
   Enter the Google secret as `GoogleOAuthClientSecret` in an untracked
   `*.local.json` file using your editor. Do not paste it into chat or shell
   history. Check `git check-ignore <local-file>` before entering secrets.
3. Keep secrets out of staged files. `NoEcho` masks the CloudFormation parameter
   but is not a substitute for restricting access to deployment files and Lambda
   configuration. Google client ID is public, not a secret.
4. First rollout: `SsoPoolMode=PROVISION`, `EnableGoogleSso=false`,
   `PasswordSignInEnabled=true`. Keep every other deployed setting unchanged.
5. Stage the new source files before `lma publish`; its source bundle uses
   `git ls-files`. Review git diff, commit/publish yourself.
6. Create and inspect a change set including nested stacks before execution.
   Confirm no existing user pool or meeting-storage resource is removed or
   replaced, and arrange backups of important transcripts before proceeding.
7. After the provision stage succeeds, test rollout is `ACTIVE`, Google `true`,
   passwords `true`, with the client ID, real secret and intended admin email.
   Review this second change set, including MCP client changes, before execution.

For rollback after provisioning, use `PROVISION` with Google `false` and
passwords `true`: application authentication returns to the legacy pool while
the new pool stays managed. Do not revert to `LEGACY` after provisioning: its
condition removes/retains the nested stack, orphaning it; re-enabling could create
another pool. Switching off Google alone while remaining `ACTIVE` does not
restore legacy users; arrange password access in the new pool first.

No existing S3/DynamoDB/KMS resource definitions were changed, and no AWS
resources were modified during implementation. Retain policies are not backups.
Existing 90-day transcript/audio/meeting expiration and
`EnableDataRetentionOnDelete=false` remain unchanged; important data is not
guaranteed permanent. Any retention-policy changes require a separate decision.

## Verification

Local checks: `tests/test_google_sso.py` covers URL rewriting, client
authentication, state/nonce preservation, safe failures, domain/admin guards,
callback lifecycle, retained users/resources and unchanged data-resource
definitions against the tracked baseline. UI tests cover enabled/disabled and
SSO-only modes. Run UI tests/build/lint and CFN lint before publishing.

After Ahmed deploys, verify:

- Cognito `GoogleWorkspace` provider has the CloudFront authorize/token URLs.
- First and repeat company Google login; reject non-company/unverified email.
- Intended Google administrator gets Admin access; dev password login works.
- Return to requested meeting, logout and re-login, historical transcripts.
- Rebuilt Chrome extension Hosted UI login, existing chromiumapp callback and
  tokens. Reinstall the updated extension. This is not yet live-tested.
- Old VP ownership implications and rollback access. Live cross-user VNC testing
  remains explicitly deferred; existing ownership checks were not removed.

Production remains deferred. Validate dev first, supply production Google URLs
and credentials, and only then consider SSO-only. Desktop capture SSO is deferred.
