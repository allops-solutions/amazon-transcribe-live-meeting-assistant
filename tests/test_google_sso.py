"""Offline SSO contract tests: bridge, triggers, callback lifecycle and data safety."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]


class CfnLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key, _ in node.value:
            value = self.construct_object(key, deep=deep)
            if value in seen:
                raise ValueError("Duplicate YAML key: " + str(value))
            seen.add(value)
        return super().construct_mapping(node, deep=deep)


def intrinsic(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


CfnLoader.add_multi_constructor("!", intrinsic)


def template(relative, baseline=False):
    text = (subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=ROOT).decode()
            if baseline else (ROOT / relative).read_text())
    return yaml.load(text, Loader=CfnLoader)


def inline(relative, resource, client, environment):
    code = template(relative)["Resources"][resource]["Properties"]["InlineCode"]
    responder = types.SimpleNamespace(SUCCESS="SUCCESS", FAILED="FAILED", send=Mock())
    namespace = {}
    with patch.dict(sys.modules, {
        "boto3": types.SimpleNamespace(client=lambda _: client), "cfnresponse": responder,
    }), patch.dict(os.environ, environment):
        exec(compile(code, resource, "exec"), namespace)
    return namespace["lambda_handler"], responder


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "LMA_SETTINGS_PARAMETER": "settings", "GOOGLE_CLIENT_ID": "test-client",
            "GOOGLE_CLIENT_SECRET": "test-secret",
            "COGNITO_LOGIN_URL": "https://pool.auth.us-east-1.amazoncognito.com",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        ssm = Mock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": json.dumps({
            "CloudFrontEndpoint": "https://example.cloudfront.net/"})}}
        spec = importlib.util.spec_from_file_location("bridge",
            ROOT / "lma-ai-stack/source/lambda_functions/google_oauth_bridge/index.py")
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"boto3": types.SimpleNamespace(client=lambda _: ssm)}):
            spec.loader.exec_module(self.bridge)
        self.callback = "https://pool.auth.us-east-1.amazoncognito.com/oauth2/idpresponse"
        self.web_callback = "https://example.cloudfront.net/oauth2/idpresponse"

    def event(self, path, method="GET", params=None):
        result = {"rawPath": path, "requestContext": {"http": {"method": method}}, "headers": {}}
        result["rawQueryString" if method == "GET" else "body"] = urlencode(params or {})
        return result

    def authorize(self, **changes):
        params = dict(client_id="test-client", redirect_uri=self.callback,
                      response_type="code", state="state", nonce="nonce",
                      scope="openid email profile", code_challenge="pkce", code_challenge_method="S256")
        params.update(changes)
        return self.bridge.lambda_handler(self.event("/oauth2/google/authorize", params=params), None)

    def test_authorize_preserves_state_nonce_pkce_and_changes_only_redirect(self):
        result = self.authorize()
        self.assertEqual(result["statusCode"], 302)
        url = urlsplit(result["headers"]["Location"])
        self.assertEqual(url.netloc, "accounts.google.com")
        params = parse_qs(url.query)
        for key, value in {"redirect_uri": self.web_callback, "state": "state",
                           "nonce": "nonce", "code_challenge": "pkce"}.items():
            self.assertEqual(params[key], [value])
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")

    def test_authorize_accepts_actual_cognito_request_without_nonce(self):
        # Captured upstream shape: Cognito sends state but no nonce or PKCE.
        event = self.event("/oauth2/google/authorize", params={
            "client_id": "test-client", "redirect_uri": self.callback,
            "response_type": "code", "state": "cognito-state",
            "scope": "openid email profile",
        })
        result = self.bridge.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 302)
        params = parse_qs(urlsplit(result["headers"]["Location"]).query)
        self.assertEqual(params["state"], ["cognito-state"])
        self.assertEqual(params["redirect_uri"], [self.web_callback])
        self.assertNotIn("nonce", params)

    def test_authorize_rejects_alternate_client_redirect_and_missing_state(self):
        for changes in ({"client_id": "other"}, {"redirect_uri": "https://evil.test"},
                        {"state": ""}, {"response_type": "token"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.authorize(**changes)["statusCode"], 400)

    def test_callback_returns_only_code_state_to_fixed_cognito_endpoint(self):
        event = self.event("/oauth2/idpresponse", params={
            "code": "code", "state": "state", "redirect_uri": "https://evil.test", "scope": "extra"})
        result = self.bridge.lambda_handler(event, None)
        url = urlsplit(result["headers"]["Location"])
        self.assertEqual(url.netloc, "pool.auth.us-east-1.amazoncognito.com")
        self.assertEqual(parse_qs(url.query), {"code": ["code"], "state": ["state"]})

    def test_callback_preserves_google_error(self):
        result = self.bridge.lambda_handler(self.event("/oauth2/idpresponse",
            params={"error": "access_denied", "state": "state"}), None)
        self.assertIn("error=access_denied", result["headers"]["Location"])

    def test_callback_rejects_missing_and_duplicate_state(self):
        for raw in ("code=code", "code=code&state=a&state=b"):
            event = self.event("/oauth2/idpresponse")
            event["rawQueryString"] = raw
            self.assertEqual(self.bridge.lambda_handler(event, None)["statusCode"], 400)

    def token_event(self):
        return self.event("/oauth2/google/token", "POST", dict(
            client_id="test-client", client_secret="test-secret", code="code",
            grant_type="authorization_code", redirect_uri=self.callback))

    def test_token_exchanges_using_identical_cloudfront_redirect(self):
        upstream = Mock()
        upstream.__enter__ = Mock(return_value=upstream)
        upstream.__exit__ = Mock(return_value=False)
        upstream.read.return_value = b'{"id_token":"test-token"}'
        with patch.object(self.bridge, "urlopen", return_value=upstream) as send:
            result = self.bridge.lambda_handler(self.token_event(), None)
        self.assertEqual(result["statusCode"], 200)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://oauth2.googleapis.com/token")
        self.assertEqual(parse_qs(request.data.decode())["redirect_uri"], [self.web_callback])

    def test_bad_client_does_not_send_google_request(self):
        event = self.token_event()
        event["body"] = event["body"].replace("test-secret", "wrong")
        with patch.object(self.bridge, "urlopen") as send:
            result = self.bridge.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 401)
        send.assert_not_called()

    def test_basic_auth_and_base64_body_supported(self):
        event = self.token_event()
        event["body"] = base64.b64encode(event["body"].encode()).decode()
        event["isBase64Encoded"] = True
        event["headers"]["Authorization"] = "Basic " + base64.b64encode(b"test-client:wrong").decode()
        self.assertEqual(self.bridge.lambda_handler(event, None)["statusCode"], 401)

    def test_token_rejects_wrong_redirect_or_grant_without_network(self):
        for replacements in (("authorization_code", "refresh_token"), (self.callback, "https://evil.test")):
            event = self.token_event()
            params = {key: value[0] for key, value in parse_qs(event["body"]).items()}
            key = "grant_type" if replacements[0] == "authorization_code" else "redirect_uri"
            params[key] = replacements[1]
            event["body"] = urlencode(params)
            with patch.object(self.bridge, "urlopen") as send:
                self.assertEqual(self.bridge.lambda_handler(event, None)["statusCode"], 400)
                send.assert_not_called()

    def test_unknown_route_does_not_accept_request(self):
        self.assertEqual(self.bridge.lambda_handler(self.event("/oauth2/other"), None)["statusCode"], 404)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.guard, _ = inline("lma-cognito-stack/deployment/lma-cognito-stack.yaml",
            "FederatedSignInGuard", self.client, {
                "ALLOWED_DOMAIN": "allops.co", "GOOGLE_ADMIN_EMAIL": "admin@allops.co",
                "PASSWORD_SIGN_IN_ENABLED": "true"})

    def event(self, source="PreSignUp_ExternalProvider", email="user@allops.co", verified="true"):
        return {"triggerSource": source, "userPoolId": "pool", "userName": "googleworkspace_123",
                "request": {"userAttributes": {
                    "email": email, "email_verified": verified,
                    "identities": json.dumps([{"providerName": "GoogleWorkspace"}])}},
                "response": {}}

    def test_verified_company_signup(self):
        event = self.event(email="User@ALLops.CO")
        self.assertIs(self.guard(event, None), event)
        self.client.admin_add_user_to_group.assert_not_called()

    def test_denies_external_suffix_subdomain_missing_or_unverified_email(self):
        for email, verified in (("user@gmail.com", "true"), ("user@allops.co.evil", "true"),
                                ("user@sub.allops.co", "true"), ("", "true"),
                                ("user@allops.co", "false")):
            with self.subTest(email=email, verified=verified), self.assertRaises(ValueError):
                self.guard(self.event(email=email, verified=verified), None)

    def test_rejects_unexpected_provider(self):
        event = self.event()
        event["userName"] = "other_123"
        with self.assertRaises(ValueError):
            self.guard(event, None)

    def test_admin_invitation_allowed_but_public_password_signup_denied(self):
        self.guard(self.event(source="PreSignUp_AdminCreateUser", verified="false"), None)
        with self.assertRaises(ValueError):
            self.guard(self.event(source="PreSignUp_SignUp"), None)

    def test_explicit_admin_bootstrap_preserves_existing_groups(self):
        event = self.event(source="TokenGeneration_HostedAuth", email="admin@allops.co")
        event["request"]["groupConfiguration"] = {"groupsToOverride": ["GoogleWorkspace"], "iamRolesToOverride": []}
        result = self.guard(event, None)
        self.client.admin_add_user_to_group.assert_called_once_with(
            UserPoolId="pool", Username="googleworkspace_123", GroupName="Admin")
        self.assertEqual(result["response"]["claimsOverrideDetails"]["groupOverrideDetails"][
            "groupsToOverride"], ["GoogleWorkspace", "Admin"])

    def test_other_company_users_do_not_receive_admin(self):
        self.guard(self.event(source="TokenGeneration_HostedAuth"), None)
        self.client.admin_add_user_to_group.assert_not_called()

    def test_admin_bootstrap_handles_cognito_null_response_fields(self):
        for response in (None, {"claimsOverrideDetails": None}):
            with self.subTest(response=response):
                event = self.event(source="TokenGeneration_HostedAuth", email="admin@allops.co")
                event["response"] = response
                event["request"]["groupConfiguration"] = None
                result = self.guard(event, None)
                self.assertEqual(result["response"]["claimsOverrideDetails"][
                    "groupOverrideDetails"]["groupsToOverride"], ["Admin"])

    def test_admin_bootstrap_preserves_claims_and_role_overrides(self):
        event = self.event(source="TokenGeneration_RefreshTokens", email="admin@allops.co")
        event["response"] = {"claimsOverrideDetails": {
            "claimsToAddOrOverride": {"custom:example": "keep"}, "claimsToSuppress": ["nickname"]}}
        event["request"]["groupConfiguration"] = {
            "groupsToOverride": ["Admin", "Existing"], "iamRolesToOverride": ["role"],
            "preferredRole": "role"}
        result = self.guard(event, None)
        overrides = result["response"]["claimsOverrideDetails"]
        self.assertEqual(overrides["claimsToAddOrOverride"], {"custom:example": "keep"})
        self.assertEqual(overrides["claimsToSuppress"], ["nickname"])
        self.assertEqual(overrides["groupOverrideDetails"], event["request"]["groupConfiguration"])

    def test_domain_check_also_applies_on_refresh(self):
        with self.assertRaises(ValueError):
            self.guard(self.event(source="TokenGeneration_RefreshTokens", email="user@gmail.com"), None)

    def test_sso_only_blocks_native_refresh_but_dev_allows_it(self):
        event = self.event(source="TokenGeneration_RefreshTokens")
        event["request"]["userAttributes"]["identities"] = "[]"
        self.guard(copy.deepcopy(event), None)
        strict, _ = inline("lma-cognito-stack/deployment/lma-cognito-stack.yaml",
            "FederatedSignInGuard", self.client, {"ALLOWED_DOMAIN": "allops.co",
                "PASSWORD_SIGN_IN_ENABLED": "false"})
        with self.assertRaises(ValueError):
            strict(event, None)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.current = {
            "ClientId": "client", "UserPoolId": "pool", "ClientName": "web",
            "CreationDate": "readonly", "ClientSecret": "readonly-secret",
            "CallbackURLs": ["http://localhost:3000/", "https://extension.chromiumapp.org/"],
            "LogoutURLs": [], "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            "SupportedIdentityProviders": ["COGNITO"], "RefreshTokenValidity": 30,
        }
        self.client.describe_user_pool_client.side_effect = lambda **_: {"UserPoolClient": copy.deepcopy(self.current)}
        fields = set(self.current) - {"CreationDate", "ClientSecret"}
        self.client.meta.service_model.operation_model.return_value.input_shape.members = fields
        self.handler, self.responder = inline("lma-ai-stack/deployment/lma-ai-stack.yaml",
            "CognitoWebCallbackFunction", self.client, {})
        self.event = {"RequestType": "Create", "ResourceProperties": {
            "UserPoolId": "pool", "ClientId": "client", "WebUrl": "https://app.cloudfront.net/",
            "ProviderName": "GoogleWorkspace", "PasswordSignInEnabled": "true"}}

    def test_adds_provider_and_web_urls_preserving_extension_and_client_settings(self):
        self.handler(self.event, None)
        request = self.client.update_user_pool_client.call_args.kwargs
        self.assertEqual(request["CallbackURLs"], self.current["CallbackURLs"] + ["https://app.cloudfront.net/"])
        self.assertEqual(request["ExplicitAuthFlows"], self.current["ExplicitAuthFlows"])
        self.assertEqual(request["SupportedIdentityProviders"], ["COGNITO", "GoogleWorkspace"])
        self.assertNotIn("ClientSecret", request)
        self.assertNotIn("CreationDate", request)
        self.assertEqual(self.responder.send.call_args.args[2], "SUCCESS")

    def test_repeated_update_does_not_duplicate_urls(self):
        self.current["CallbackURLs"].append("https://app.cloudfront.net/")
        self.current["LogoutURLs"].append("https://app.cloudfront.net/")
        self.event["RequestType"] = "Update"
        self.handler(self.event, None)
        self.assertEqual(self.client.update_user_pool_client.call_args.kwargs["CallbackURLs"],
                         self.current["CallbackURLs"])

    def test_sso_only_excludes_native_hosted_ui_provider(self):
        self.event["ResourceProperties"]["PasswordSignInEnabled"] = "false"
        self.handler(self.event, None)
        self.assertEqual(self.client.update_user_pool_client.call_args.kwargs["SupportedIdentityProviders"],
                         ["GoogleWorkspace"])

    def test_disable_detaches_google_without_deleting_users_or_callbacks(self):
        self.current["SupportedIdentityProviders"] = ["COGNITO", "GoogleWorkspace"]
        self.event["RequestType"] = "Delete"
        self.handler(self.event, None)
        result = self.client.update_user_pool_client.call_args.kwargs
        self.assertEqual(result["SupportedIdentityProviders"], ["COGNITO"])
        self.assertEqual(result["CallbackURLs"], self.current["CallbackURLs"])
        self.client.delete_user_pool.assert_not_called()

    def test_failure_response_does_not_expose_secret(self):
        self.client.update_user_pool_client.side_effect = RuntimeError("sensitive-secret")
        self.handler(self.event, None)
        self.assertEqual(self.responder.send.call_args.args[2], "FAILED")
        self.assertNotIn("sensitive-secret", repr(self.responder.send.call_args))


class TemplateTests(unittest.TestCase):
    def test_all_original_resources_and_data_definitions_preserved(self):
        protected = {"AWS::S3::Bucket", "AWS::DynamoDB::Table", "AWS::KMS::Key"}
        for relative in ("lma-main.yaml", "lma-ai-stack/deployment/lma-ai-stack.yaml",
                         "lma-cognito-stack/deployment/lma-cognito-stack.yaml"):
            old, new = template(relative, True), template(relative)
            self.assertFalse(set(old["Resources"]) - set(new["Resources"]))
            for name, resource in old["Resources"].items():
                if resource["Type"] in protected:
                    self.assertEqual(new["Resources"][name], resource, (relative, name))

    def test_both_pool_and_nested_stack_are_retained(self):
        main = template("lma-main.yaml")
        cognito = template("lma-cognito-stack/deployment/lma-cognito-stack.yaml")
        for resource in (main["Resources"]["SSOCOGNITOSTACK"], cognito["Resources"]["UserPool"],
                         cognito["Resources"]["AdminUser"],
                         cognito["Resources"]["AdminUserToGroupAttachment"]):
            self.assertEqual(resource["DeletionPolicy"], "Retain")
            self.assertEqual(resource["UpdateReplacePolicy"], "Retain")

    def test_google_provider_uses_cloudfront_adapters_and_google_issuer(self):
        ai = template("lma-ai-stack/deployment/lma-ai-stack.yaml")
        details = ai["Resources"]["GoogleWorkspaceIdentityProvider"]["Properties"]["ProviderDetails"]
        self.assertEqual(details["oidc_issuer"], "https://accounts.google.com")
        self.assertIn("WebAppCloudFrontDistribution.DomainName", details["authorize_url"]["Sub"])
        self.assertIn("/oauth2/google/token", details["token_url"]["Sub"])

    def test_environment_flags_and_client_id(self):
        dev = json.loads((ROOT / "deploy/params/dev.json").read_text())
        prod = json.loads((ROOT / "deploy/params/prod.json").read_text())
        dev = {p["ParameterKey"]: p["ParameterValue"] for p in dev}
        prod = {p["ParameterKey"]: p["ParameterValue"] for p in prod}
        self.assertEqual(dev["EnableGoogleSso"], "false")
        self.assertEqual(dev["SsoPoolMode"], "LEGACY")
        self.assertEqual(prod["EnableGoogleSso"], "true")
        self.assertEqual(prod["PasswordSignInEnabled"], "false")
        self.assertTrue(dev["GoogleOAuthClientId"].endswith(".apps.googleusercontent.com"))
        self.assertEqual(dev["GoogleOAuthClientSecret"], "FILL_IN_FROM_SECRET_STORE")


if __name__ == "__main__":
    unittest.main()
