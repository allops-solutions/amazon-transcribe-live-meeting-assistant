# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the scheduled pipeline's own machinery. No AWS, no stack.

Everything else in this directory needs a deployed stack, so it runs only when
someone asks for it. These do not, and they run in the fast pipeline
(`make test-integ-plumbing`), because they cover the two pieces that decide
whether a scheduled run can report success without having tested anything:

  * ``cognito_test_user.resolve`` — where the opt-in audio test's credentials
    come from. If it silently returns nothing, that test skips.
  * the ``--no-skips`` hook in conftest.py — which turns such a skip into a
    failure, and is therefore the thing standing between a lapsed secret and a
    green nightly.

``ci_assume_role`` is covered for the file it writes; the STS exchange itself
needs a real OIDC token and is exercised only by the pipeline.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_assume_role  # noqa: E402
import cognito_test_user  # noqa: E402
import kds_pipeline_probe  # noqa: E402
import mcp_oauth_probe  # noqa: E402


def test_oauth_probe_unwraps_gateway_and_lambda_payloads():
    vp = {"virtualParticipantId": "test-vp", "status": "CANCELLED"}
    payload = {"result": {"content": [{"text": json.dumps({
        "statusCode": 200, "body": json.dumps({"content": [{"text": json.dumps(vp)}]})
    })}]}}
    assert vp in list(mcp_oauth_probe.result_objects(payload))


@pytest.mark.parametrize("payload", [{"isError": True}, {"error": {}}, {"statusCode": 403}])
def test_oauth_probe_does_not_mistake_tool_errors_for_success(payload):
    with pytest.raises(RuntimeError):
        list(mcp_oauth_probe.result_objects(payload))


def test_oauth_probe_refuses_production_before_any_user_or_meeting_writes(monkeypatch):
    session = mock.Mock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "009853297978"}
    monkeypatch.setattr(mcp_oauth_probe.boto3, "Session", mock.Mock(return_value=session))
    with pytest.raises(ValueError, match="dev AWS account"):
        mcp_oauth_probe.run("LMA-CI-254333", "38377254333", "https://meet.google.com/vng-fbkk-chh")
    assert session.client.call_args_list == [mock.call("sts")]


def test_oauth_probe_refuses_original_dev_before_resource_lookup(monkeypatch):
    session = mock.Mock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "135755363077"}
    monkeypatch.setattr(mcp_oauth_probe.boto3, "Session", mock.Mock(return_value=session))
    with pytest.raises(ValueError, match="exact isolated stack"):
        mcp_oauth_probe.run("LMA-Dev", "38377254333", "https://meet.google.com/vng-fbkk-chh")
    assert session.client.call_args_list == [mock.call("sts")]


def _cleanup_clients(monkeypatch, detail=None):
    ddb = mock.Mock()
    ddb.get_item.return_value = {"Item": detail} if detail else {}
    session = mock.Mock()
    session.client.side_effect = lambda name: ddb if name == "dynamodb" else mock.Mock()
    monkeypatch.setattr(kds_pipeline_probe.boto3, "Session", mock.Mock(return_value=session))
    monkeypatch.setattr(kds_pipeline_probe, "_event_sourcing_table", lambda *_args: "test-table")
    return ddb


def test_probe_cleanup_paginates_call_and_separate_listing_rows(monkeypatch):
    call_id = "integ-test-kds-20261013T120000"
    key = {"PK": {"S": "c#" + call_id}, "SK": {"S": "c#" + call_id}}
    listing = {"PK": {"S": "cls#2026-10-13#s#02"}, "SK": {"S": "ts#time#id#" + call_id}}
    ddb = _cleanup_clients(monkeypatch, {**key, "Owner": {"S": "integ-test@lma.aws"}})
    # Even an empty filtered page may carry a continuation key.
    ddb.scan.side_effect = [{"Items": [], "LastEvaluatedKey": key}, {"Items": [listing]}]
    ddb.query.side_effect = [{"Items": [key], "LastEvaluatedKey": key}, {"Items": []}]
    kds_pipeline_probe.cleanup("LMA-CI-123", call_id)
    assert ddb.scan.call_count == ddb.query.call_count == 2
    assert [c.kwargs["Key"] for c in ddb.delete_item.call_args_list] == [key, listing]
    assert "#owner = :owner" in ddb.delete_item.call_args_list[-1].kwargs["ConditionExpression"]


def test_probe_cleanup_removes_orphan_listing_even_without_detail(monkeypatch):
    ddb = _cleanup_clients(monkeypatch)
    listing = {"PK": {"S": "cls#date#s#01"}, "SK": {"S": "ts#time#id#synthetic"}}
    ddb.scan.return_value = {"Items": [listing]}
    ddb.query.return_value = {"Items": []}
    kds_pipeline_probe.cleanup("LMA-CI-123", "integ-test-kds-20261013T120000")
    assert ddb.delete_item.call_args.kwargs["Key"] == listing


def test_probe_cleanup_rejects_foreign_owner_and_non_test_ids(monkeypatch):
    ddb = _cleanup_clients(monkeypatch, {"Owner": {"S": "customer@allops.co"}})
    with pytest.raises(ValueError, match="owner"):
        kds_pipeline_probe.cleanup("LMA-CI-123", "integ-test-kds-20261013T120000")
    with pytest.raises(ValueError, match="synthetic"):
        kds_pipeline_probe.cleanup("LMA-CI-123", "customer-meeting")
    ddb.delete_item.assert_not_called()


def test_probe_cleanup_does_not_hide_delete_failures(monkeypatch):
    ddb = _cleanup_clients(monkeypatch, {"Owner": {"S": "integ-test@lma.aws"}})
    ddb.scan.return_value = {"Items": []}
    ddb.query.return_value = {"Items": [{"PK": {"S": "pk"}, "SK": {"S": "sk"}}]}
    ddb.delete_item.side_effect = RuntimeError("failed")
    with pytest.raises(RuntimeError, match="failed"):
        kds_pipeline_probe.cleanup("LMA-CI-123", "integ-test-kds-20261013T120000")


@pytest.fixture(autouse=True)
def _clear_credential_cache():
    """resolve() memoizes, which would leak one test's answer into the next."""
    cognito_test_user._resolve_cached.cache_clear()
    yield
    cognito_test_user._resolve_cached.cache_clear()


# ── where the test user comes from ──────────────────────────────────────────


def test_environment_credentials_are_used_when_both_are_set(monkeypatch):
    monkeypatch.setenv("LMA_TEST_USERNAME", "someone@example.com")
    monkeypatch.setenv("LMA_TEST_PASSWORD", "from-the-environment")
    monkeypatch.delenv("LMA_TEST_USER_SECRET_ID", raising=False)
    assert cognito_test_user.resolve() == ("someone@example.com", "from-the-environment")


def test_no_credentials_configured_resolves_to_none(monkeypatch):
    """The local default: nothing set, so the opt-in test skips rather than errors."""
    for name in ("LMA_TEST_USERNAME", "LMA_TEST_PASSWORD", "LMA_TEST_USER_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cognito_test_user.resolve() is None
    assert cognito_test_user.available() is False


def test_a_username_without_a_password_is_not_treated_as_configured(monkeypatch):
    """Half-set environment variables must not look like a usable credential."""
    monkeypatch.setenv("LMA_TEST_USERNAME", "someone@example.com")
    monkeypatch.delenv("LMA_TEST_PASSWORD", raising=False)
    monkeypatch.delenv("LMA_TEST_USER_SECRET_ID", raising=False)
    assert cognito_test_user.resolve() is None


def _stub_secret(monkeypatch, payload: object) -> mock.Mock:
    """Point the secret path at a fake Secrets Manager returning ``payload``.

    Returns the stub client, so a test can assert on how it was called.
    """
    monkeypatch.delenv("LMA_TEST_USERNAME", raising=False)
    monkeypatch.delenv("LMA_TEST_PASSWORD", raising=False)
    monkeypatch.setenv("LMA_TEST_USER_SECRET_ID", "lma/integ/test-user")
    client = mock.Mock()
    body = payload if isinstance(payload, str) else json.dumps(payload)
    client.get_secret_value.return_value = {"SecretString": body}
    session = mock.Mock()
    session.client.return_value = client
    monkeypatch.setattr("boto3.Session", mock.Mock(return_value=session))
    return client


def test_secret_manager_credentials_are_used_when_no_environment_variables(monkeypatch):
    client = _stub_secret(
        monkeypatch, {"username": "ci@example.com", "password": "from-the-secret"}
    )
    assert cognito_test_user.resolve("us-west-2") == ("ci@example.com", "from-the-secret")
    client.get_secret_value.assert_called_once_with(SecretId="lma/integ/test-user")


def test_email_is_accepted_as_the_username_key(monkeypatch):
    """A Cognito pool configured for email sign-in calls the username 'email'."""
    _stub_secret(monkeypatch, {"email": "ci@example.com", "password": "pw"})
    assert cognito_test_user.resolve() == ("ci@example.com", "pw")


def test_environment_variables_take_precedence_over_the_secret(monkeypatch):
    _stub_secret(monkeypatch, {"username": "secret@example.com", "password": "secret-pw"})
    monkeypatch.setenv("LMA_TEST_USERNAME", "env@example.com")
    monkeypatch.setenv("LMA_TEST_PASSWORD", "env-pw")
    assert cognito_test_user.resolve() == ("env@example.com", "env-pw")


def test_secret_missing_the_password_key_raises_rather_than_skipping(monkeypatch):
    """A malformed secret must fail loudly; returning None would skip the test."""
    _stub_secret(monkeypatch, {"username": "ci@example.com"})
    with pytest.raises(cognito_test_user.CredentialsUnavailable, match="has no password"):
        cognito_test_user.resolve()


def test_secret_that_is_not_json_raises_rather_than_skipping(monkeypatch):
    _stub_secret(monkeypatch, "not-json-at-all")
    with pytest.raises(cognito_test_user.CredentialsUnavailable, match="is not JSON"):
        cognito_test_user.resolve()


def test_unreadable_secret_raises_rather_than_skipping(monkeypatch):
    """A misspelled secret id, or a role without permission to read it."""
    monkeypatch.delenv("LMA_TEST_USERNAME", raising=False)
    monkeypatch.delenv("LMA_TEST_PASSWORD", raising=False)
    monkeypatch.setenv("LMA_TEST_USER_SECRET_ID", "lma/integ/does-not-exist")
    client = mock.Mock()
    client.get_secret_value.side_effect = RuntimeError("ResourceNotFoundException")
    session = mock.Mock()
    session.client.return_value = client
    monkeypatch.setattr("boto3.Session", mock.Mock(return_value=session))
    with pytest.raises(cognito_test_user.CredentialsUnavailable, match="could not read"):
        cognito_test_user.resolve()


def test_available_reports_false_for_a_broken_source_without_raising(monkeypatch):
    """`available()` is the skip guard, so it must not raise out of a test body."""
    _stub_secret(monkeypatch, {"username": "ci@example.com"})
    assert cognito_test_user.available() is False


# ── the credentials file the pipeline writes ────────────────────────────────

_FAKE_CREDENTIALS = {
    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI-EXAMPLE-KEY",  # pragma: allowlist secret
    "aws_session_token": "FwoGZXIvYXdzEXAMPLE",
}


def test_credentials_file_is_written_owner_only(tmp_path: Path):
    path = tmp_path / "credentials"
    ci_assume_role._write_profile(path, "lma-ci", _FAKE_CREDENTIALS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_existing_credentials_file_is_tightened_not_left_as_found(tmp_path: Path):
    """O_CREAT's mode applies only on creation, so an existing file needs a chmod."""
    path = tmp_path / "credentials"
    path.write_text("[other]\naws_access_key_id = KEEP\n", encoding="utf-8")
    path.chmod(0o644)
    ci_assume_role._write_profile(path, "lma-ci", _FAKE_CREDENTIALS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writing_a_profile_preserves_other_profiles(tmp_path: Path):
    path = tmp_path / "credentials"
    path.write_text("[other]\naws_access_key_id = KEEP\n", encoding="utf-8")
    ci_assume_role._write_profile(path, "lma-ci", _FAKE_CREDENTIALS)
    body = path.read_text(encoding="utf-8")
    assert "[other]" in body and "KEEP" in body
    assert "[lma-ci]" in body
    assert _FAKE_CREDENTIALS["aws_session_token"] in body


def test_credentials_path_honours_the_aws_environment_variable(monkeypatch, tmp_path: Path):
    """The pipeline points this outside the checkout so it cannot become an artifact."""
    target = tmp_path / "elsewhere" / "credentials"
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(target))
    assert ci_assume_role._credentials_path() == target


def test_an_empty_oidc_token_is_refused_before_calling_sts(monkeypatch):
    """An absent `id_tokens:` block yields an empty variable, not an error."""
    called = mock.Mock()
    monkeypatch.setattr(ci_assume_role, "_assume", called)
    exit_code = ci_assume_role.main(
        ["--role-arn", "arn:aws:iam::123456789012:role/example", "--token", "   "]
    )
    assert exit_code == 2
    called.assert_not_called()
