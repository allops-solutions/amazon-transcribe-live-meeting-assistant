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

These files alone do not activate CI/CD. Commit and push them only after review.
The following setup creates paid AWS infrastructure and GitHub configuration;
it is not performed by running local tests.

1. Create GitHub environments `production` and `dev`. Restrict deployment branches
   to `allops-main`. Require a production reviewer and disable self-approval where
   available. Protect `allops-main` and review workflow changes: this is a public
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
   parameter allows it. Required admin emails, document bucket and Google OAuth
   credentials must have real values. Ask the boss for production Google OAuth
   credentials; do not copy dev secrets into production without approval.
   Do not commit this JSON or include it in logs or GitHub artifacts. If encrypted
   with a customer-managed KMS key, supply `ConfigurationKmsKeyArn` and authorize
   the deployment role in that key policy.
6. Review and deploy `deploy/ci/bootstrap.yaml` in the selected account/region.
   Confirm the environment matches that account. Reuse an existing GitHub OIDC
   provider with `ExistingOidcProviderArn` if present. Optional IAM permissions
   boundaries apply to both new roles. Check that the webhook was created and
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

Open Actions → **LMA deployment** → Run workflow. Select `allops-main`, choose
the environment, and type `DEPLOY LMA`. Checks must pass before deployment can
start. Production also waits for its environment approval.

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
