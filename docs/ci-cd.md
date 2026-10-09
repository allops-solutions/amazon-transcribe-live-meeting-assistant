---
title: "allOps CI/CD"
---

Copyright allOps. Project code is licensed under the MIT License.

# allOps CI/CD

## Scope and deployment order

The maintained branch is `allops-main`. CI tests pull requests and pushes without
AWS credentials. Deployment is a separate, manually dispatched GitHub Actions
workflow. The first production deployment must use this workflow, rather than a
separate laptop publish/deploy. Keep the existing `LMA-Dev` until production has
passed functional acceptance. No workflow deletes that existing environment.

Targets are fixed in `deploy/ci/deploy.py`:

| Environment | AWS account | Stack | Cleanup |
|---|---|---|---|
| production | 009853297978 | LMA | Never automatic |
| dev | 135755363077 | LMA-CI-&lt;workflow run ID&gt; | Run-owned temporary stack only |

Region is `us-east-1`. Kimi remains the summary model. No runtime model migration
or changes to scheduling behavior are part of this pipeline.

## One-time bootstrap — requires administrator approval

### Current production preparation status (2026-10-09)

Production deployment remains disabled. PR9's required checks passed and it was
merged into `allops-main`; this does not activate a deployment role or deploy LMA.

For the approved initial release, ACS, ElevenLabs, Simli, Tavily and Zoom SDK
credentials are intentionally empty. Teams/Zoom browser-based joining remains
available; third-party voice/avatar/web-search integrations are disabled.
Google Meet, Google SSO, Amazon Transcribe, Kimi summaries, Sonnet assistant and
document/transcript knowledge bases remain selected.

Enter the production Google secret directly into Secrets Manager's
`lma/ci/production/parameters` JSON under `GoogleOAuthClientSecret`, preserving
existing entries. Never put this value in Git, a command-line argument, logs or
chat. The public client ID is already in `prod.json`.

Run the read-only, secret-safe configuration check using production SSO:

```bash
AWS_PROFILE=default AWS_PAGER='' .venv/bin/python deploy/ci/production_config.py --check-models
```

It prints only missing parameter names/reasons and profile counts, never values.
Production deployment preflight enforces the same initial-release configuration;
a missing Google secret or unexpected integration/model override fails closed.
An ACTIVE profile is metadata validation, **not** a successful model inference.

The draft runtime ceiling now supports exact knowledge-base, vector-index,
state-machine and scheduler-group identities plus approved profile/model ARNs.
`--include-model-resources` reads the exact destinations of the three approved
profiles and adds the local Titan embedding model. It invokes no model. The
combined synthetic fixture fits the managed-policy limit after consolidating
redundant S3 ownership Denies and compacting only protective Deny ARN patterns.
The separate account-local PassRole Allow remains. All controller protections remain.
The negated [StringNotEqualsIfExists condition](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_condition_operators.html)
in a Deny rejects both foreign and missing owner context; a separate Null Deny
would repeat the same restriction. No Allow scope was broadened to fit.
The real complete inventory may still require capability-specific ceilings before
activation. Oversized policies fail instead of dropping scopes or protections.

The production secret-safe check now passes: the user entered the required Google
secret directly in Secrets Manager, and no secret value was displayed. Profile
availability and actual inference are separate from this configuration check.
Four tiny live inference probes succeeded in production under the administrator
session: Sonnet4.6, KimiK3, Haiku4.5 and Titan embeddings. This verifies account
model access, not application-role permissions or application functionality.

`permission_bootstrap.py` and `permission-bootstrap.yaml` prepare an **inactive**
administrator-owned first-CREATE reconciler. The template defaults both the
controller and policy writes off. Code must be a reviewed immutable object version
in separate administrator storage, never the editable application release prefix.
The function accepts no authority-bearing invocation input and has no application
invocation permission. Reserved concurrency 1 serializes that controller's calls;
administrators must also avoid concurrent policy edits because IAM has no policy
compare-and-swap API.

It pins one root and one administrator policy, inspects every creation-history
page, rejects imports/updates/failures/rollbacks, recomputes scopes from AWS, and
compares the entire previous document against the administrator initial digest
or a reconstructed exact prior subset. Repeated identical reconciliation is a
no-op. Access Analyzer findings, truncation, scope drift, or policy-size/version
limits stop before a write. It never deletes policy versions to make room.
At IAM's [five-version limit](https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreatePolicyVersion.html),
an administrator must review history/capacity. This is deliberately fail-closed,
not a complete autonomous first-deployment controller yet.

Runtime and provisioning drafts explicitly deny application access to the
`LMA-Isolation-*` controller/code/logs and administrator storage/configuration.
These draft protections have not been attached to AWS roles. Runtime permission
compatibility, capability-policy sizing, non-IAM provisioning/network controls,
first-CREATE dependency barriers and activation remain blockers. Do not deploy
this component or declare production ready from its unit tests.

The read-only collector also has an initial-creation inspection mode:

```bash
AWS_PROFILE=default AWS_PAGER='' .venv/bin/python deploy/ci/generated_resources.py \
  --account 009853297978 --creating-root-arn '<exact new LMA stack ARN>'
```

This accepts only that exact root in `CREATE_IN_PROGRESS` and new nested stacks
in creation, reports only completed resource IDs, and marks the inventory partial.
It cannot approve an existing/updating stack, unfinished IDs or imported records.
It **does not attach/update IAM policies or by itself solve first deployment**.
Imported historical resources still require separate administrator origin review;
current stack membership alone is not proof of historical creation.

The MicroVM launcher's unrestricted `DescribeTaskDefinition` call is not used by
the selected Fargate path. Other runtime and provisioning permissions, including
custom-resource setup operations, networking and the first-deployment permission
bootstrap, remain unfinished. Do not enable the deployment role or claim the
application ready from the configuration report or IAM simulations.

Service scoping follows the official [Bedrock authorization table](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock.html),
[S3 Vectors table](https://docs.aws.amazon.com/service-authorization/latest/reference/list_s3vectors.html),
and [Scheduler table](https://docs.aws.amazon.com/service-authorization/latest/reference/list_scheduler.html).
`RetrieveAndGenerate` is not ARN-scoped like `Retrieve`; its dependent permissions
must be reviewed/verified before it is enabled in the ceiling. It remains denied.

These files alone do not activate CI/CD. Commit and push them only after review.
The following setup creates paid AWS infrastructure and GitHub configuration;
it is not performed by running local tests.

1. Create GitHub environments `production` and `dev`. Restrict deployment branches
   to `allops-main`. Required PR and production environment reviews were removed
   with Ahmed/Ismail's authorization on 2026-10-08; required CI, strict branch
   freshness, admin enforcement and force-push/deletion protections remain.
   Review gates may be restored if the team wants independent approval later.
   Protect `allops-main` and inspect workflow changes: this is a public
   repository with a privileged Docker runner. Never run pull-request code on it.
2. Once the workflows are on the default branch, run **LMA inspect OIDC identity**
   for each environment. Copy its exact `sub` claim into `GitHubOidcSubject`.
   The job prints only audience and subject, not its token. Do not guess whether
   GitHub uses the classic or immutable-ID subject format.
3. In each target AWS account, authorize a GitHub App CodeConnections connection
   for this repository only. Complete the connection's browser authorization.
   Use its ARN as `GitHubConnectionArn`; never store a personal access token in Git.
4. Review and provision a CloudFormation service role separately. The existing
   `iam-roles/cloudformation-management/LMA-Cloudformation-Service-Role.yaml` is
   a starting point, not a claim of least privilege. It has broad infrastructure
   permissions and must be reviewed by the administrator. It needs access to
   the release artifact bucket and nested application provisioning. Supply its
   ARN as `CloudFormationServiceRoleArn`.
5. Create an AWS Secrets Manager secret containing a JSON object mapping parameter
   names to string values. Supply its ARN as `ConfigurationSecretArn`. Resolve
   every `TODO_` / `FILL_IN_FROM_SECRET_STORE` value in `deploy/params/prod.json`
   or `dev.json`. Disabled optional providers may use empty strings where their
   parameter allows it. Required admin emails and enabled Google OAuth credentials
   must have real values. The document bucket is optional (empty disables that S3
   source). Use the approved production OAuth credentials; do not copy dev secrets
   into production without approval.
   Do not commit this JSON or include it in logs or GitHub artifacts. If encrypted
   with a customer-managed KMS key, supply `ConfigurationKmsKeyArn` and authorize
   the deployment role in that key policy.
6. Review and deploy `deploy/ci/bootstrap.yaml` in the selected account/region.
   First use `EnableDeploymentRole=false` (the default) for runner-only bootstrap.
   This creates build infrastructure without an OIDC deployment role. After
   the production identity job passes, enable `EnableAuthenticationRole=true`
   with its observed `GitHubOidcSubject` to validate AWS authentication separately.
   This role can list only the artifact bucket and describe only the configuration
   secret; it cannot read secret values, write objects or deploy resources.
   Set the production GitHub variable `LMA_AUTH_ROLE_ARN` from its output and run
   **LMA bootstrap validation** with `validate_aws=true`.
   Keep `EnableDeploymentRole=false` during this validation.
   For a full build test before deployment is enabled, set `EnablePublishRole=true`,
   configure `LMA_PUBLISH_ROLE_ARN` from `PublishRoleArn`, and dispatch that workflow
   with `publish_build=true`. This separate role writes only `build-validation/*`
   in the CI artifact bucket, never `releases/*`, and can validate templates.
   It cannot read configuration secrets, pass roles or create/execute changesets.
   Optional artifact KMS access is restricted to the configured key through S3.
   The workflow builds/publishes privately and checks the uploaded main template.
   This incurs CodeBuild/S3 costs but does not deploy any LMA application resources.
   After
   verifying the exact subject and reviewing the service role, update it with
   `EnableDeploymentRole=true` and both required identity/service-role inputs.
   Confirm the environment matches that account. Reuse an existing GitHub OIDC
   provider with `ExistingOidcProviderArn` if present. Optional IAM permissions
   boundaries apply to all new roles. Check that the webhook was created and
   receives `WORKFLOW_JOB_QUEUED` events.
   If supplying `CustomerManagedEncryptionKeyArn` for artifacts/logs, authorize
   CloudWatch Logs and artifact decryption by the CloudFormation service role in
   that key policy. Without it, artifacts use SSE-S3 and logs use AWS-managed
   encryption at rest.
7. Set these **environment variables** in each GitHub environment (not repository
   secrets; these values are identifiers, never actual credentials):

   | GitHub variable | Value |
   |---|---|
   | LMA_DEPLOY_ROLE_ARN | Bootstrap DeploymentRoleArn output |
   | LMA_AUTH_ROLE_ARN | AuthenticationRoleArn output for metadata-only validation |
   | LMA_PUBLISH_ROLE_ARN | PublishRoleArn output for optional build-only validation |
   | LMA_ARTIFACT_BUCKET | Bootstrap ArtifactBucket output |
   | LMA_CONFIG_SECRET_ARN | Configuration secret ARN |
   | LMA_CFN_ROLE_ARN | Reviewed CloudFormation service role ARN |

The bootstrap creates a private, encrypted, versioned artifact bucket, retained
180-day runner logs, a Docker-enabled ephemeral CodeBuild runner, and an OIDC
deployment role. The runner service role itself has only log and GitHub connection
permissions; deployment credentials are assumed separately. Production has no
DeleteStack permission. Dev deletion is limited to CI stack names with the managed
tag, with a second exact run-ID/ownership check in Python.

## Running a release

### Stronger isolation rollout (not yet activated)

Ahmed selected stronger isolation on 2026-10-08. All application templates now
accept and forward the optional `PermissionsBoundaryArn`; explicit IAM roles
and SAM-generated function roles receive it when configured. Empty defaults
preserve the existing dev behavior. Coverage tests prevent future omissions.

This is coverage groundwork, **not an enforced isolation policy**. A production
boundary and scoped provisioning policy still need definition, IAM simulation,
SAM-transformed-template checks and deployment/runtime validation. Provisioning
must require the exact approved boundary when creating application roles and
must not let deployed code remove it or modify its policy. Service-linked roles,
custom-resource permissions and resource-policy grants need separate handling.
Do not enable the deployment role merely because boundary coverage tests pass.

`deploy/ci/iam-guardrails.yaml` adds an **inactive deny-only overlay** for the
future CloudFormation role. It requires the exact application boundary on role
creation/replacement, prohibits boundary removal, protects isolation policies
and CI roles (which otherwise match `LMA-*`), denies IAM outside the application
namespace, and prohibits provisioner role assumption. It grants no permissions,
creates no role and attaches itself to nothing. Service-linked-role creation is
also blocked by this overlay; required service-linked roles must be handled in a
separate administrator-controlled bootstrap. It is not yet a complete provisioning
policy or runtime boundary and does not constrain non-IAM services.

Run the read-only AWS test with
`AWS_PROFILE=default .venv/bin/python deploy/ci/simulate_iam_guardrails.py --account 009853297978`.
The simulator intentionally combines a hypothetical broad Allow with the overlay
to test its explicit Denies. That Allow is never attached or deployed. This test
does not establish resource-policy/session containment or runtime compatibility.

CI runs the official AWS SAM translator (pinned to 1.113.0) and verifies boundary
coverage on the resulting IAM roles. Local artifact URIs are replaced with
in-memory S3 stand-ins for this offline test; it does not build code, upload
artifacts, resolve runtime permissions or deploy a stack.

Production preflight/deploy now also requires the environment variable
`LMA_APPLICATION_BOUNDARY_ARN`, pointing to an approved account-local policy in
`/lma/isolation/`. The merged `PermissionsBoundaryArn` must match it exactly;
configuration-secret overrides cannot clear or substitute it. Keep the variable
unset until the actual runtime boundary is defined and validated. Dev remains
optional-boundary. This code check supplements, but never replaces, IAM enforcement.

`deploy/ci/runtime_boundary.py` now generates an **inactive, incomplete runtime
ceiling** for named LMA resources. It explicitly denies unreviewed actions and
access outside the reviewed namespaces, limits artifacts to read-only release
access, and denies cross-account S3 access. The dedicated document bucket is an
optional exact-name scope. Broad service action ceilings on LMA-named resources
are maximum permissions, not grants: each role's existing identity policies must
still authorize its own work. PassRole is limited to `/lma/application/LMA-*`;
IAM mutation and STS role assumption remain denied. KMS data operations and
AppSync data operations, Cognito user/group reads and CloudFront invalidations
can be scoped to exact, verified stack resource IDs. ECS task launches require
an exact task-definition revision AND an approved destination cluster. Task
reads/stops/tagging are confined to tasks under verified clusters; listing tasks
requires explicit `ecs:cluster` context. Missing cluster context is denied.
Account-wide `DescribeTaskDefinition` reads are deliberately not enabled because
AWS provides no definition-ID resource/condition scope for that operation.
EC2, Bedrock and other unreviewed services remain denied.
Do not activate it: it intentionally cannot run the full application yet.
Generated-ID resources, model access, provisioning controls and runtime
compatibility must be addressed before activation. A passing simulator does not
validate the behavior of deployed resource policies, AWS service principals or
service-linked roles. No production IAM policy is created by this generator.

`--include-generated-resources` performs a read-only inventory of the existing,
stable `LMA` root and all nested stacks/pages. It checks account, region, parent
and root ownership before including exact KMS/AppSync/Cognito/CloudFront/ECS IDs. Missing or unstable
stacks fail closed. This is not a first-deployment bootstrap solution: custom
resources may need those permissions before the initial stack is complete.

When a boundary is configured, all explicit and SAM-generated application roles,
managed policies and instance profiles use `/lma/application/`. The provisioning
IAM deny overlay rejects legacy root-path roles as well as unrelated IAM objects.
Empty boundary defaults preserve existing dev paths. Changing an existing IAM
resource's path requires replacement; do not apply this to an existing bounded
deployment without a migration plan, particularly for explicitly named roles.

The read-only test is:
`AWS_PROFILE=default .venv/bin/python deploy/ci/runtime_boundary.py --account 009853297978 --artifact-bucket allops-lma-ci-009853297978-production-us-east-1 --simulate`.
Add `--documents-bucket allops-lma-documents-009853297978-us-east-1` to validate
the optional document scope. The generator checks the 6,144-character managed
policy cap and omits diagnostic statement IDs if longer partition names require
it; never drops permission rules to fit the limit.

The compact boundary's non-IAM Allow is always paired with an exhaustive
`DenyUnreviewedActions` and explicit resource/account/cluster Denies. IAM is
excluded from that Allow and receives only path-scoped PassRole. Removing or
weakening those Denies would change the ceiling's safety properties. Oversized
inventories fail rather than silently omit resources or broaden them to '*'.

### Pinned deployment requests (prepared, not activated)

The future deployment identity can create application change sets only with the
configured CloudFormation service role and a private artifact-bucket URL under
`releases/*/lma-main.yaml`. Explicit Denies reject missing/wrong roles, inline
templates and other source buckets/prefixes. The reviewed AWS SAM transform has
a separate transform-only authorization; this does not authorize other stacks.
Deployment preflight requires the role's account-local `/lma/isolation/LMA-*`
path. The role remains separately administered, not application-controlled.

Read-only regression command:
`AWS_PROFILE=default .venv/bin/python deploy/ci/simulate_release_guards.py --account 009853297978`.
This checks hypothetical request semantics, not a live CloudFormation deployment.
First-deployment bootstrap ordering and the non-IAM provisioning service role
are still incomplete. Do not enable deployment based on these simulations alone.

### Optional production documents and OAuth credentials

The Google OAuth client ID is public configuration. Its matching client secret
belongs in the production configuration secret, never committed or pasted into
chat. Configure the production Google callback URL once the domain is known.

`BedrockKnowledgeBaseS3BucketName` is optional for an existing document bucket.
Alternatively, `CreateDocumentSourceBucket=true` creates a dedicated, encrypted,
versioned, public-access-blocked, retained, HTTPS-only bucket in the application
stack and wires it into the knowledge base and assistant. This requires the
create-KB assistant mode and an empty existing-bucket parameter. Production
parameters now select this option, approved by Ahmed for the first deployment;
dev defaults remain unchanged. No bucket has been created yet. Once deployed,
documents can be added without redeploying the application. With the flag false
and an empty existing-bucket parameter, only the S3 document source is disabled,
not transcript search.

This repository uses S3 Vectors, not a continuously provisioned OpenSearch
cluster, for the knowledge base. It is nevertheless usage-billed: S3 storage and
requests, vector storage/indexing/search, embedding-model calls, assistant-model
calls, plus the sync Lambda/schedule's activity. An empty source is not a promise
of zero AWS charges. See [S3 pricing](https://aws.amazon.com/s3/pricing/) and
[Bedrock pricing](https://aws.amazon.com/bedrock/pricing/). An empty bucket has no
stored document payload or document embedding/query usage; sync activity and
other assistant/transcript usage are separate. Production deployment remains a
separate step, not performed by changing these parameters.

Before the first release, **LMA bootstrap validation** checks CodeBuild's runner,
repository checkout and Docker daemon without deployment credentials by default.
`validate_aws=true` also checks metadata-only AWS authentication;
`publish_build=true` enables the separate private build-only test described above.
Neither option deploys the application. A passing build test does not prove
CloudFormation permissions or production runtime behavior.

Open Actions → **LMA deployment** → Run workflow. Select `allops-main`, choose
the environment, and type `DEPLOY LMA`. Checks must pass before deployment can
start. No separate environment reviewer is currently required, but the manual
`DEPLOY LMA` confirmation and main-branch restriction remain.

The pipeline validates account/configuration before building, installs the local
SDK/CLI, builds from the selected commit, publishes privately under
`releases/<SHA>/<run ID>/<attempt>`, and executes a CloudFormation change set.
Deployment parameters are loaded from Secrets Manager in memory and not printed.
Logs show changed resource names and replacement flags. Production termination
protection is enabled after successful creation/update. CloudFormation parameters
still exist in AWS: ensure sensitive application parameters use `NoEcho` and limit
access to the service role/configuration secret.

Dev runs create a new temporary stack and remove it after infrastructure validation,
including failed deployments where ownership can be verified. They do not keep a
stack around for manual meeting tests. Cleanup can fail or be interrupted by job
cancellation/credential expiry; do not assume `always()` guarantees deletion.
Inspect the exact `LMA-CI-<run ID>` stack and its tags before manually retrying
cleanup. Artifact storage, retained logs, external document buckets/configuration
and any resources with explicit retention policies are not automatically removed.

## What passing means

Local unit tests prove guard logic, not a deployed pipeline. A successful workflow
proves build and CloudFormation deployment, not end-to-end meeting acceptance.
Before production acceptance, verify Google SSO callback URLs/credentials, user
login, Bedrock model access, a real VP lifecycle and meeting transcription/summary.
Reconfigure Quick's LMA connection to production deliberately, then verify an
unattended run. Keep current dev until these checks pass. No existing-dev teardown
is included here.

## Upstream updates

Maintain this fork independently. Port selected upstream features in reviewed
branches, adapting them to allOps changes and running CI; do not wholesale sync
or rebase onto upstream. Successful checks alone do not authorize production
deployment.

## Local verification (no AWS calls)

```sh
AWS_PROFILE=default .venv/bin/python -m unittest discover -s deploy/ci/tests -v
.venv/bin/cfn-lint --non-zero-exit-code error deploy/ci/bootstrap.yaml
```

  Runner reference: [AWS CodeBuild-hosted GitHub Actions runners](https://docs.aws.amazon.com/codebuild/latest/userguide/action-runner.html).
Trust reference: [GitHub OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).
