"""Offline regression coverage for Kimi's summary-only model selection."""
import json
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

import boto3
from test_google_sso import template

ROOT = Path(__file__).resolve().parents[1]
KIMI = "global.moonshotai.kimi-k3"


class SummaryModelWiringTests(unittest.TestCase):
    def test_override_only_changes_summary_lambda(self):
        main = template("lma-main.yaml")
        ai = template("lma-ai-stack/deployment/lma-ai-stack.yaml")
        expected = {"If": ["HasSummaryBedrockModelId", {"Ref": "SummaryBedrockModelId"},
                           {"Ref": "BedrockModelId"}]}
        env = ai["Resources"]["BedrockSummaryLambda"]["Properties"]["Environment"]["Variables"]
        self.assertEqual(env["BEDROCK_MODEL_ID"], expected)
        self.assertEqual(env["BEDROCK_FALLBACK_MODEL_ID"], {"Ref": "BedrockFallbackModelId"})
        for resource, key in (("QueryKnowledgeBaseResolverFunction", "MODEL_ID"),
                              ("MCPServerAnalyticsFunction", "BEDROCK_MODEL_ID")):
            # Existing search generation must not switch to unsupported Kimi KB generation.
            self.assertEqual(ai["Resources"][resource]["Properties"]["Environment"][
                "Variables"][key], {"Ref": "BedrockModelId"})
        nested = main["Resources"]["AISTACK"]["Properties"]["Parameters"]
        self.assertEqual(nested["SummaryBedrockModelId"], {"Ref": "SummaryBedrockModelId"})
        self.assertIn(expected, main["Resources"]["ValidateParameters"]["Properties"]["BedrockModelIds"])

    def test_dev_selects_kimi_without_changing_assistant_or_search(self):
        params = {p["ParameterKey"]: p.get("ParameterValue")
                  for p in json.loads((ROOT / "deploy/params/dev.json").read_text())}
        self.assertEqual(params["SummaryBedrockModelId"], KIMI)
        self.assertEqual(params["BedrockModelId"], "global.anthropic.claude-sonnet-4-6")
        self.assertEqual(params["MeetingAssistServiceBedrockModelID"], "global.anthropic.claude-sonnet-4-6")

    def test_empty_override_preserves_existing_deployments(self):
        for path in ("lma-main.yaml", "lma-ai-stack/deployment/lma-ai-stack.yaml"):
            config = template(path)
            self.assertEqual(config["Parameters"]["SummaryBedrockModelId"]["Default"], "")
            self.assertIn(KIMI, config["Parameters"]["SummaryBedrockModelId"]["AllowedValues"])
            self.assertEqual(config["Conditions"]["HasSummaryBedrockModelId"],
                             {"Not": [{"Equals": [{"Ref": "SummaryBedrockModelId"}, ""]}]})

    def test_prod_records_approved_retention_and_summary_model(self):
        entries = json.loads((ROOT / "deploy/params/prod.json").read_text())
        params = {p["ParameterKey"]: p.get("ParameterValue") for p in entries}
        self.assertEqual(len(entries), len(params), "Duplicate parameter keys")
        self.assertEqual(params["MeetingRecordExpirationInDays"], "180")
        self.assertEqual(params["TranscriptionExpirationInDays"], "180")
        self.assertEqual(params["AudioRecordingExpirationInDays"], "180")
        self.assertEqual(params["CloudWatchLogsExpirationInDays"], "180")
        self.assertEqual(params["SummaryBedrockModelId"], KIMI)
        self.assertEqual(params["BedrockModelId"], "global.anthropic.claude-sonnet-4-6")
        self.assertEqual(params["MeetingAssistServiceBedrockModelID"],
                         "global.anthropic.claude-sonnet-4-6")


class KimiConverseTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        env = {"BEDROCK_MODEL_ID": KIMI, "BEDROCK_FALLBACK_MODEL_ID": "fallback",
               "FETCH_TRANSCRIPT_LAMBDA_ARN": "fetch", "S3_BUCKET_NAME": "bucket",
               "S3_PREFIX": "transcripts", "LLM_PROMPT_TEMPLATE_TABLE_NAME": "templates",
               "AWS_REGION": "us-east-1", "SUMMARY_EFFORT": "medium", "SUMMARY_MAX_TOKENS": "4096"}
        with patch.dict(os.environ, env), patch.object(boto3, "client", return_value=self.client):
            self.module = runpy.run_path(str(ROOT / "lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/index.py"))

    def response(self):
        return {"output": {"message": {"content": [
            {"reasoningContent": {"reasoningText": {"text": "internal"}}},
            {"text": "Meeting summary"}]}}, "usage": {"inputTokens": 20, "outputTokens": 5}}

    def test_single_turn_request_and_text_output(self):
        self.client.converse.return_value = self.response()
        self.assertEqual(self.module["call_bedrock"]("Transcript text"), "Meeting summary")
        self.client.converse.assert_called_once_with(
            modelId=KIMI, messages=[{"role": "user", "content": [{"text": "Transcript text"}]}],
            inferenceConfig={"maxTokens": 4096})

    def test_haiku_fallback_omits_unsupported_adaptive_thinking(self):
        args = self.module["build_inference_args"](
            "global.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertEqual(args, {"inferenceConfig": {"maxTokens": 4096, "temperature": 0}})

    def test_sonnet_keeps_adaptive_thinking(self):
        args = self.module["build_inference_args"]("global.anthropic.claude-sonnet-4-6")
        self.assertEqual(args["inferenceConfig"]["temperature"], 1)
        self.assertEqual(args["additionalModelRequestFields"]["thinking"]["type"], "adaptive")

    def test_existing_fallback_still_runs_after_primary_error(self):
        self.client.converse.side_effect = [RuntimeError("Unavailable"), self.response()]
        self.assertEqual(self.module["call_bedrock"]("Transcript"), "Meeting summary")
        self.assertEqual([call.kwargs["modelId"] for call in self.client.converse.call_args_list],
                         [KIMI, "fallback"])


if __name__ == "__main__":
    unittest.main()
