"""Offline behavioural tests for explicit summary profiles and admin deletion."""
import ast
from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict
import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError
from test_google_sso import template

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in selected:
        node.decorator_list = []
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, "exec"), namespace)
    return namespace


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.profiles, self.lambda_client = Mock(), Mock(), Mock()
        self.lambda_client.invoke.return_value = {"StatusCode": 202}
        self.calls.get_item.return_value = {"Item": {
            "PK": "c#meeting", "SummaryProfile": "old-profile", "SummaryLanguage": "Bosnian",
            "CallSummaryText": "old summary"}}
        self.profiles.get_item.return_value = {"Item": {
            "LLMPromptTemplateId": "Profile#chosen", "0#SUMMARY": "Summarize {transcript}"}}
        ddb = Mock()
        ddb.Table.side_effect = lambda name: self.calls if name == "calls" else self.profiles
        self.ns = functions("lma-ai-stack/source/lambda_functions/regenerate_summary_resolver/index.py",
                            {"handler", "_seconds_since", "_in_progress_age"}, {
            "Any": Any, "Dict": Dict, "datetime": datetime, "timezone": timezone,
            "logger": logging.getLogger(), "json": json, "re": re, "dynamodb": ddb,
            "lambda_client": self.lambda_client, "EVENT_SOURCING_TABLE_NAME": "calls",
            "LLM_PROMPT_TEMPLATE_TABLE_NAME": "profiles", "STATUS_IN_PROGRESS": "IN_PROGRESS",
            "SUMMARY_IN_PROGRESS_TTL_SECONDS": 660, "ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN": "orchestrator"})

    def event(self, **options):
        return {"arguments": {"input": {"CallId": "meeting", **options}}}

    def test_missing_blank_or_invalid_profile_rejected_without_spending(self):
        for profile in (None, "", " ", "invalid/profile"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.ns["handler"](self.event(SummaryProfile=profile), None)
        self.calls.update_item.assert_not_called()
        self.lambda_client.invoke.assert_not_called()

    def test_missing_or_empty_profile_rejected(self):
        for item in (None, {"LLMPromptTemplateId": "Profile#chosen", "0#SUMMARY": "NONE"}):
            self.profiles.get_item.return_value = {"Item": item}
            with self.assertRaises(ValueError):
                self.ns["handler"](self.event(SummaryProfile="chosen"), None)
        self.calls.update_item.assert_not_called()
        self.lambda_client.invoke.assert_not_called()

    def test_selected_profile_clears_old_language_without_erasing_old_summary(self):
        self.ns["handler"](self.event(SummaryProfile="chosen", SummaryLanguage="Bosnian"), None)
        update = self.calls.update_item.call_args.kwargs
        self.assertIn("REMOVE SummaryLanguage", update["UpdateExpression"])
        self.assertNotIn("CallSummaryText", update["UpdateExpression"])
        self.assertIn("attribute_not_exists(SummaryRequestedAt)", update["ConditionExpression"])
        payload = json.loads(self.lambda_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["CallId"], "meeting")
        self.assertEqual(payload["SummaryProfile"], "chosen")
        self.assertTrue(payload["SummaryRequestedAt"].endswith("Z"))

    def test_failed_async_invoke_releases_only_its_claim(self):
        self.lambda_client.invoke.side_effect = RuntimeError("invoke unavailable")
        with self.assertRaises(RuntimeError):
            self.ns["handler"](self.event(SummaryProfile="chosen"), None)
        self.assertEqual(self.calls.update_item.call_count, 2)
        release = self.calls.update_item.call_args.kwargs
        self.assertEqual(release["ExpressionAttributeValues"][":failed"], "FAILED")
        self.assertIn("SummaryRequestedAt = :requested_at", release["ConditionExpression"])

    def test_atomic_claim_failure_never_invokes_second_run(self):
        self.calls.get_item.return_value["Item"]["SummaryRequestedAt"] = "2020-01-01T00:00:00Z"
        self.calls.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "race"}}, "UpdateItem")
        with self.assertRaises(ClientError):
            self.ns["handler"](self.event(SummaryProfile="chosen"), None)
        self.assertEqual(self.calls.update_item.call_args.kwargs["ConditionExpression"],
                         "attribute_exists(PK) AND SummaryRequestedAt = :previous_requested_at")
        self.lambda_client.invoke.assert_not_called()

    def test_in_progress_run_rejected(self):
        self.calls.get_item.return_value["Item"].update({
            "SummaryStatus": "IN_PROGRESS", "SummaryRequestedAt": datetime.now(timezone.utc).isoformat()})
        with self.assertRaises(ValueError):
            self.ns["handler"](self.event(SummaryProfile="chosen"), None)
        self.lambda_client.invoke.assert_not_called()


class DeletePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.delete, self.share, self.verify = Mock(), Mock(), Mock(return_value=True)
        self.ns = functions("lma-ai-stack/source/lambda_functions/meeting_controls_resolver/index.py",
                            {"lambda_handler"}, {"verify_permissions": self.verify,
                            "delete_meetings": self.delete, "share_meetings": self.share})

    def event(self, groups=None, action="deleteMeetings", claims=False):
        identity = {"username": "owner"}
        identity["claims" if claims else "groups"] = {"cognito:groups": groups} if claims else groups
        return {"identity": identity, "info": {"fieldName": action},
                "arguments": {"input": {"Calls": [], "MeetingRecipients": "other@allops.co"}}}

    def test_non_admin_owner_and_missing_or_malformed_groups_denied_before_work(self):
        for groups in (None, [], ["User"], ["NotAdmin"], "Admin"):
            for claims in (False, True):
                with self.subTest(groups=groups, claims=claims), self.assertRaises(PermissionError):
                    self.ns["lambda_handler"](self.event(groups, claims=claims), None)
        self.verify.assert_not_called()
        self.delete.assert_not_called()

    def test_admin_group_and_claims_allowed(self):
        for claims in (False, True):
            self.ns["lambda_handler"](self.event(["Admin"], claims=claims), None)
        self.assertEqual(self.delete.call_count, 2)

    def test_sharing_permissions_unchanged(self):
        self.ns["lambda_handler"](self.event(["User"], action="shareMeetings"), None)
        self.share.assert_called_once_with([], "owner", "other@allops.co")
        self.delete.assert_not_called()


class SummaryFailureTests(unittest.TestCase):
    def test_plain_text_and_json_profiles_can_be_archived(self):
        ns = functions("lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/index.py",
                       {"format_summary"}, {"json": json})
        metadata = {"CallId": "meeting", "CreatedAt": "2026-09-29T10:00:00Z",
                    "TotalConversationDurationMillis": 60000}
        for text in ("# Summary\nDecisions and actions", '["action"]', '"summary"'):
            archived = json.loads(ns["format_summary"](text, metadata))
            self.assertEqual(archived["SUMMARY"], text)
            self.assertEqual(archived["MEETING NAME"], "meeting")
        archived = json.loads(ns["format_summary"]('{"DECISIONS":"agreed"}', metadata))
        self.assertEqual(archived["DECISIONS"], "agreed")

    def test_empty_model_section_fails_before_summary_is_archived(self):
        ns = functions("lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/index.py",
                       {"generate_summary"}, {"json": json,
                       "get_profile_templates_from_dynamodb": Mock(return_value=[{"SUMMARY": "{transcript}"}]),
                       "apply_language": lambda prompt, language: prompt, "call_bedrock": Mock(return_value=" ")})
        with self.assertRaises(ValueError):
            ns["generate_summary"]("transcript", None, "chosen")

    def test_failure_or_empty_result_does_not_publish_summary(self):
        for result in ({"summary": None, "error": "failure"}, {"summary": ""}):
            write = Mock()
            ns = functions("lma-ai-stack/source/lambda_functions/async_transcript_summary_orchestrator/lambda_function.py",
                           {"handler"}, {"Any": Any, "Dict": Dict, "LambdaContext": object,
                           "json": json, "LOGGER": Mock(), "get_call_summary": Mock(return_value=result),
                           "write_call_summary_to_kds": write, "mark_summary_failed": Mock()})
            ns["handler"]({"CallId": "meeting"}, None)
            write.assert_not_called()
            ns["mark_summary_failed"].assert_called_once()

    def test_failed_run_status_update_is_conditional_and_preserves_summary(self):
        ddb = Mock()
        ns = functions("lma-ai-stack/source/lambda_functions/async_transcript_summary_orchestrator/lambda_function.py",
                       {"mark_summary_failed"}, {"Any": Any, "Dict": Dict,
                       "DYNAMODB_CLIENT": ddb, "EVENT_SOURCING_TABLE_NAME": "calls",
                       "ClientError": ClientError, "LOGGER": Mock()})
        ns["mark_summary_failed"]({"CallId": "meeting", "SummaryRequestedAt": "request-1"})
        args = ddb.update_item.call_args.kwargs
        self.assertEqual(args["Key"]["PK"], {"S": "c#meeting"})
        self.assertEqual(args["ExpressionAttributeValues"][":requested_at"], {"S": "request-1"})
        self.assertIn("SummaryRequestedAt = :requested_at", args["ConditionExpression"])
        self.assertNotIn("CallSummaryText", args["UpdateExpression"])

    def test_orchestrator_invoke_error_releases_run_without_publishing(self):
        failed, write = Mock(), Mock()
        ns = functions("lma-ai-stack/source/lambda_functions/async_transcript_summary_orchestrator/lambda_function.py",
                       {"handler"}, {"Any": Any, "Dict": Dict, "LambdaContext": object,
                       "json": json, "LOGGER": Mock(),
                       "get_call_summary": Mock(side_effect=RuntimeError("invoke failed")),
                       "write_call_summary_to_kds": write, "mark_summary_failed": failed})
        event = {"CallId": "meeting", "SummaryRequestedAt": "request-1"}
        ns["handler"](event, None)
        failed.assert_called_once_with(event)
        write.assert_not_called()

    def test_kinesis_write_error_releases_run(self):
        failed = Mock()
        ns = functions("lma-ai-stack/source/lambda_functions/async_transcript_summary_orchestrator/lambda_function.py",
                       {"handler"}, {"Any": Any, "Dict": Dict, "LambdaContext": object,
                       "json": json, "LOGGER": Mock(),
                       "get_call_summary": Mock(return_value={"summary": "new"}),
                       "write_call_summary_to_kds": Mock(side_effect=RuntimeError("stream failed")),
                       "mark_summary_failed": failed})
        event = {"CallId": "meeting", "SummaryRequestedAt": "request-1"}
        ns["handler"](event, None)
        failed.assert_called_once()

    def test_success_publishes_replacement(self):
        write = Mock()
        ns = functions("lma-ai-stack/source/lambda_functions/async_transcript_summary_orchestrator/lambda_function.py",
                       {"handler"}, {"Any": Any, "Dict": Dict, "LambdaContext": object,
                       "json": json, "LOGGER": Mock(), "get_call_summary": Mock(return_value={"summary": "new"}),
                       "write_call_summary_to_kds": write})
        ns["handler"]({"CallId": "meeting"}, None)
        self.assertEqual(write.call_args.args[0]["CallSummaryText"], "new")

    def test_summary_lambda_failure_does_not_write_s3(self):
        write = Mock()
        ns = functions("lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/index.py",
                       {"handler"}, {"json": json, "get_transcripts": Mock(return_value={
                           "transcript": "text", "metadata": {}}),
                           "generate_summary": Mock(side_effect=RuntimeError("model unavailable")),
                           "write_to_s3": write})
        result = ns["handler"]({"CallId": "meeting", "SummaryProfile": "chosen"}, None)
        self.assertIsNone(result["summary"])
        self.assertIn("error", result)
        write.assert_not_called()


class McpMetadataTests(unittest.TestCase):
    def test_existing_storage_and_cognito_definitions_are_unchanged(self):
        protected_types = {"AWS::DynamoDB::Table", "AWS::S3::Bucket", "AWS::Cognito::UserPool",
                           "AWS::Cognito::UserPoolClient", "AWS::Cognito::IdentityPool"}
        for path in ("lma-main.yaml", "lma-ai-stack/deployment/lma-ai-stack.yaml",
                     "lma-virtual-participant-stack/template.yaml"):
            current = template(path)["Resources"]
            original = template(path, baseline=True)["Resources"]
            protected = {key: value for key, value in original.items() if value["Type"] in protected_types}
            # New calendar claim metadata is additive; every EXISTING protected
            # resource must retain its complete definition and logical ID.
            self.assertEqual({key: current.get(key) for key in protected}, protected)
            added = {key for key, value in current.items()
                     if value["Type"] in protected_types and key not in protected}
            self.assertEqual(added, {"CalendarScheduleClaimsTable"}
                             if path == "lma-ai-stack/deployment/lma-ai-stack.yaml"
                             and "CalendarScheduleClaimsTable" not in original else set())
        main, original = template("lma-main.yaml"), template("lma-main.yaml", baseline=True)
        for key in ("COGNITOSTACK", "SSOCOGNITOSTACK"):
            self.assertEqual(main["Resources"][key], original["Resources"][key])

    def test_sharing_resolver_is_unchanged_and_delete_guard_is_only_on_deletion(self):
        ai = template("lma-ai-stack/deployment/lma-ai-stack.yaml")
        original = template("lma-ai-stack/deployment/lma-ai-stack.yaml", baseline=True)
        self.assertEqual(ai["Resources"]["ShareCallAppSyncResolver"],
                         original["Resources"]["ShareCallAppSyncResolver"])
        for resource in ("DeleteCallAppSyncResolver", "DeleteTranscriptSegmentAppSyncResolver"):
            source = ai["Resources"][resource]["Properties"]["RequestMappingTemplate"]["Sub"]
            self.assertIn('$util.unauthorized()', source)
            self.assertIn('.contains("Admin")', source)
            self.assertIn('assumed-role/${MeetingControlsResolverFunctionExecutionRole}/', source)

    def test_published_tool_schema_explicitly_supports_google_meet(self):
        ai = template("lma-ai-stack/deployment/lma-ai-stack.yaml")
        target = ai["Resources"]["MCPServerGatewayTarget"]["Properties"]
        tools = target["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]
        relevant = [t for t in tools if t["Name"] in ("start_meeting_now", "schedule_meeting")]
        self.assertEqual(len(relevant), 2)
        for tool in relevant:
            self.assertIn("Google Meet IS supported", tool["Description"])
            self.assertIn("Google Meet", tool["InputSchema"]["Properties"]["meetingPlatform"]["Description"])


if __name__ == "__main__":
    unittest.main()
