#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Verify setup uses the already-allowed, filtered stack metadata API."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[3]


class MeetingAssistSetupTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "meetingassist_setup_test", ROOT / "lma-meetingassist-setup-stack/src/setup_function.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"AWS_REGION": "us-east-1"}), \
                patch.dict("sys.modules", {"cfnresponse": MagicMock()}), \
                patch("boto3.client", return_value=MagicMock()):
            spec.loader.exec_module(self.module)
        self.module.cf = MagicMock()
        self.module.lam = MagicMock()

    def test_filtered_plural_lookup_returns_exact_identity(self):
        self.module.cf.describe_stack_resources.return_value = {"StackResources": [
            {"LogicalResourceId": "Other", "PhysicalResourceId": "wrong"},
            {"LogicalResourceId": "AsyncAgentAssistOrchestrator", "PhysicalResourceId": "LMA-async"},
        ]}
        self.assertEqual(self.module.getStackResource("exact-stack", "AsyncAgentAssistOrchestrator"),
                         "LMA-async")
        self.module.cf.describe_stack_resources.assert_called_once_with(
            StackName="exact-stack", LogicalResourceId="AsyncAgentAssistOrchestrator")
        self.module.cf.describe_stack_resource.assert_not_called()

    def test_missing_or_ambiguous_identity_fails_closed(self):
        for rows in ([], [{"LogicalResourceId": "Other", "PhysicalResourceId": "wrong"}],
                     [{"LogicalResourceId": "Expected", "PhysicalResourceId": "one"},
                      {"LogicalResourceId": "Expected", "PhysicalResourceId": "two"}]):
            with self.subTest(rows=rows):
                self.module.cf.describe_stack_resources.return_value = {"StackResources": rows}
                with self.assertRaises(ValueError):
                    self.module.getStackResource("exact-stack", "Expected")

    def test_configuration_preserves_other_environment_values(self):
        self.module.cf.describe_stack_resources.side_effect = [
            {"StackResources": [{"LogicalResourceId": "AsyncAgentAssistOrchestrator",
                                  "PhysicalResourceId": "LMA-async"}]},
            {"StackResources": [{"LogicalResourceId": "QueryKnowledgeBaseResolverFunction",
                                  "PhysicalResourceId": "LMA-kb"}]},
        ]
        self.module.lam.get_function.return_value = {
            "Configuration": {"FunctionArn": "arn:aws:lambda:us-east-1:009853297978:function:LMA-StrandsMeetingAssist"}}
        self.module.lam.get_function_configuration.side_effect = [
            {"Environment": {"Variables": {"KEEP": "async"}}},
            {"Environment": {"Variables": {"KEEP": "kb"}}},
        ]
        self.module.configureStrandsMode({"AISTACK": "exact-stack",
                                         "StrandsMeetingAssistFunction": "LMA-StrandsMeetingAssist",
                                         "TranscriptBedrockKnowledgeBaseId": "test-kb"})
        calls = self.module.lam.update_function_configuration.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["FunctionName"], "LMA-async")
        self.assertEqual(calls[0].kwargs["Environment"]["Variables"]["KEEP"], "async")
        self.assertTrue(calls[0].kwargs["Environment"]["Variables"]["LAMBDA_AGENT_ASSIST_FUNCTION_ARN"].endswith(":LMA-StrandsMeetingAssist"))
        self.assertEqual(calls[1].kwargs["FunctionName"], "LMA-kb")
        self.assertEqual(calls[1].kwargs["Environment"]["Variables"], {"KEEP": "kb", "KB_ID": "test-kb"})

    def test_identity_permissions_remain_scoped_and_boundary_already_allows_api(self):
        template = yaml.load((ROOT / "lma-meetingassist-setup-stack/template.yaml").read_text(), Loader=yaml.BaseLoader)
        statements = template["Resources"]["SetupFunctionRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        discovery = [stmt for stmt in statements if "cloudformation:DescribeStackResources" in stmt["Action"]]
        self.assertEqual([stmt["Resource"] for stmt in discovery], [["AISTACK"], ["COGNITOSTACK"]])
        policy = yaml.load((ROOT / "deploy/ci/practical-access.yaml").read_text(), Loader=yaml.BaseLoader)
        boundary = policy["Resources"]["ApplicationBoundary"]["Properties"]["PolicyDocument"]["Statement"]
        self.assertTrue(any(stmt["Effect"] == "Allow" and "cloudformation:DescribeStackResources" in stmt.get("Action", [])
                            for stmt in boundary))


if __name__ == "__main__":
    unittest.main()
