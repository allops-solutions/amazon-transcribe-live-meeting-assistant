# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
# Invokes Anthropic generate text API using requests module
# see https://console.anthropic.com/docs/api/reference for more details

import json
import logging
import os
import re

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.ERROR)

# grab environment variables

# use inference profile for model id as Nova models require the use of inference profiles
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
# Retried once, per section, if the primary model errors (not empty = enabled).
BEDROCK_FALLBACK_MODEL_ID = os.getenv("BEDROCK_FALLBACK_MODEL_ID", "").strip()
FETCH_TRANSCRIPT_LAMBDA_ARN = os.environ["FETCH_TRANSCRIPT_LAMBDA_ARN"]
PROCESS_TRANSCRIPT = os.getenv("PROCESS_TRANSCRIPT", "False") == "True"
TOKEN_COUNT = int(os.getenv("TOKEN_COUNT", "0"))  # default 0 - do not truncate.
# Max output tokens per summary section (thinking tokens count against this when enabled).
SUMMARY_MAX_TOKENS = int(os.getenv("SUMMARY_MAX_TOKENS", "4096"))
# Adaptive-thinking effort for Anthropic models: off | low | medium | high | max.
SUMMARY_EFFORT = os.getenv("SUMMARY_EFFORT", "medium").strip().lower()
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
S3_PREFIX = os.environ["S3_PREFIX"]

# Table name and keys used for default and custom prompt templates items in DDB
LLM_PROMPT_TEMPLATE_TABLE_NAME = os.environ["LLM_PROMPT_TEMPLATE_TABLE_NAME"]
DEFAULT_PROMPT_TEMPLATES_PK = "DefaultSummaryPromptTemplates"
CUSTOM_PROMPT_TEMPLATES_PK = "CustomSummaryPromptTemplates"

# Optional environment variables allow region / endpoint override for bedrock Boto3
BEDROCK_REGION = (
    os.environ["BEDROCK_REGION_OVERRIDE"]
    if "BEDROCK_REGION_OVERRIDE" in os.environ
    else os.environ["AWS_REGION"]
)
BEDROCK_ENDPOINT_URL = os.environ.get(
    "BEDROCK_ENDPOINT_URL", f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com"
)


# Adaptive retry (not boto3's default "legacy" mode, which gives up after a
# handful of fixed-delay attempts) — get_transcripts() below invokes
# FetchTranscript synchronously, and on 2026-09-16 that invoke hit
# TooManyRequestsException (account-wide concurrent-executions ceiling, not
# a code bug — see LMA-HANDOFF.md) and gave up, producing "An error
# occurred." for the whole summary. Matches the bedrock client's config below.
lambda_client = boto3.client("lambda", config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))
dynamodb_client = boto3.client("dynamodb")
bedrock = boto3.client(
    service_name="bedrock-runtime",
    region_name=BEDROCK_REGION,
    endpoint_url=BEDROCK_ENDPOINT_URL,
    config=Config(retries={"max_attempts": 50, "mode": "adaptive"}),
)


def get_templates_from_dynamodb(prompt_override):
    templates = []
    prompt_template_str = None

    if prompt_override is not None:
        print("Prompt Template String override:", prompt_override)
        prompt_template_str = prompt_override
        try:
            prompt_templates = json.loads(prompt_template_str)
            for k, v in prompt_templates.items():
                prompt = v.replace("<br>", "\n")
                templates.append({k: prompt})
        except Exception:
            prompt = prompt_template_str.replace("<br>", "\n")
            templates.append({"Summary": prompt})

    if prompt_template_str is None:
        try:
            defaultPromptTemplatesResponse = dynamodb_client.get_item(
                Key={"LLMPromptTemplateId": {"S": DEFAULT_PROMPT_TEMPLATES_PK}},
                TableName=LLM_PROMPT_TEMPLATE_TABLE_NAME,
            )
            customPromptTemplatesResponse = dynamodb_client.get_item(
                Key={"LLMPromptTemplateId": {"S": CUSTOM_PROMPT_TEMPLATES_PK}},
                TableName=LLM_PROMPT_TEMPLATE_TABLE_NAME,
            )

            defaultPromptTemplates = defaultPromptTemplatesResponse["Item"]
            customPromptTemplates = customPromptTemplatesResponse["Item"]
            print("Default Prompt Template:", defaultPromptTemplates)
            print("Custom Template:", customPromptTemplates)

            mergedPromptTemplates = {**defaultPromptTemplates, **customPromptTemplates}
            print("Merged Prompt Template:", mergedPromptTemplates)

            for k in sorted(mergedPromptTemplates):
                if k != "LLMPromptTemplateId" and k != "*Information*":
                    prompt = mergedPromptTemplates[k]["S"]
                    # skip if prompt value is empty, or set to 'NONE'
                    if prompt and prompt != "NONE":
                        prompt = prompt.replace("<br>", "\n")
                        index = k.find("#")
                        k_stripped = k[index + 1 :]
                        templates.append({k_stripped: prompt})
        except Exception as e:
            print("Exception:", e)
            raise (e)

    return templates


def get_profile_templates_from_dynamodb(profile_id):
    """Load a named summary profile's section templates (e.g. "technical-client",
    "startup", "team-weekly"). Profiles are self-contained — unlike the
    Default/Custom pair, there is no merge; a profile's own N#LABEL fields are
    its complete set of sections. Falls back to the Default+Custom merge if
    the profile can't be found, so a stale/deleted profile reference on an
    old meeting never breaks summary generation."""
    try:
        response = dynamodb_client.get_item(
            Key={"LLMPromptTemplateId": {"S": f"Profile#{profile_id}"}},
            TableName=LLM_PROMPT_TEMPLATE_TABLE_NAME,
        )
        item = response.get("Item")
        if not item:
            print(f"Summary profile '{profile_id}' not found — falling back to Default+Custom templates")
            return get_templates_from_dynamodb(None)

        templates = []
        for k in sorted(item):
            if k in ("LLMPromptTemplateId", "*Information*"):
                continue
            prompt = item[k]["S"]
            if prompt and prompt != "NONE":
                prompt = prompt.replace("<br>", "\n")
                index = k.find("#")
                k_stripped = k[index + 1:]
                templates.append({k_stripped: prompt})
        return templates
    except Exception as e:
        print(f"Error loading summary profile '{profile_id}': {e} — falling back to Default+Custom templates")
        return get_templates_from_dynamodb(None)


def apply_language(prompt, language):
    """Inject the requested output language into a section prompt. Profile
    authors can place a {language} placeholder anywhere in their prompt for
    exact control; prompts without one (including the existing Default/Custom
    templates, which predate this feature) get the instruction appended."""
    if not language:
        return prompt
    if "{language}" in prompt:
        return prompt.replace("{language}", language)
    return f"{prompt}\n\nWrite your entire response in {language}."


def get_transcripts(callId):
    payload = {
        "CallId": callId,
        "ProcessTranscript": PROCESS_TRANSCRIPT,
        "TokenCount": TOKEN_COUNT,
        "IncludeSpeaker": True,
    }
    print("Invoking lambda", payload)
    response = lambda_client.invoke(
        FunctionName=FETCH_TRANSCRIPT_LAMBDA_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    print("Lambda response:", response)
    transcript_data = response["Payload"].read().decode()
    transcript_json = json.loads(transcript_data)
    print("Transcript JSON:", transcript_json)
    return transcript_json


def get_generated_text(response):
    # With thinking enabled the first block is reasoningContent; return the first text block.
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            return block["text"]
    raise ValueError("No text block in Bedrock response")


def build_inference_args(modelId):
    """inferenceConfig plus provider-specific reasoning/effort fields.

    SUMMARY_EFFORT: off | low | medium | high | max (max is Anthropic-only).
    Verified live per provider on 2026-09-15:
      Anthropic: thinking.type=adaptive + output_config.effort, temperature must be 1
      OpenAI:    reasoning_effort (low|medium|high)
      Nova 2:    reasoningConfig.type=enabled + maxReasoningEffort (low|medium|high), temperature must be 0
    """
    args = {"inferenceConfig": {"maxTokens": SUMMARY_MAX_TOKENS, "temperature": 0}}
    effort = SUMMARY_EFFORT
    if effort in ("", "off", "none", "0"):
        return args
    if "anthropic" in modelId:
        args["inferenceConfig"]["temperature"] = 1
        args["additionalModelRequestFields"] = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    elif "openai" in modelId:
        args["additionalModelRequestFields"] = {"reasoning_effort": "high" if effort == "max" else effort}
    elif "nova-2" in modelId:
        args["additionalModelRequestFields"] = {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "high" if effort == "max" else effort}
        }
    # other models (Nova 1.x etc.): no reasoning support, plain call
    return args


def _converse(modelId, prompt_data):
    print("Bedrock request - ModelId", modelId)
    message = {"role": "user", "content": [{"text": prompt_data}]}
    args = build_inference_args(modelId)
    print("Bedrock inference args:", json.dumps(args))
    response = bedrock.converse(modelId=modelId, messages=[message], **args)
    usage = response.get("usage", {})
    print(
        f"Bedrock usage - input: {usage.get('inputTokens')} output: {usage.get('outputTokens')} "
        f"stopReason: {response.get('stopReason')}"
    )
    generated_text = get_generated_text(response)
    print("Bedrock response: ", json.dumps(generated_text))
    return generated_text


def call_bedrock(prompt_data):
    # boto3's adaptive retry (see the bedrock client config below) already
    # exhausts its own retry budget for transient/throttling errors before
    # raising — so an exception reaching here is a harder failure (model
    # unavailable, access denied, validation error, region outage). One
    # retry against a different model/provider covers exactly that case
    # without masking a genuinely broken prompt (which would fail on the
    # fallback too, and correctly still surface as an error).
    try:
        return _converse(BEDROCK_MODEL_ID, prompt_data)
    except Exception as primary_error:
        if not BEDROCK_FALLBACK_MODEL_ID or BEDROCK_FALLBACK_MODEL_ID == BEDROCK_MODEL_ID:
            raise
        print(
            f"Primary model '{BEDROCK_MODEL_ID}' failed ({primary_error}); "
            f"retrying with fallback model '{BEDROCK_FALLBACK_MODEL_ID}'"
        )
        try:
            return _converse(BEDROCK_FALLBACK_MODEL_ID, prompt_data)
        except Exception as fallback_error:
            print(f"Fallback model '{BEDROCK_FALLBACK_MODEL_ID}' also failed: {fallback_error}")
            raise


def generate_summary(transcript, prompt_override, profile_id=None, language=None):
    # Priority: an explicit ad-hoc Prompt override (existing behavior, e.g.
    # debug/manual invocation) > a named summary profile > the stack's
    # Default+Custom template merge (today's only behavior, unchanged when
    # neither of the above is set).
    if prompt_override is not None:
        templates = get_templates_from_dynamodb(prompt_override)
    elif profile_id:
        templates = get_profile_templates_from_dynamodb(profile_id)
    else:
        templates = get_templates_from_dynamodb(None)
    result = {}
    for item in templates:
        key = list(item.keys())[0]
        prompt = item[key]
        prompt = prompt.replace("{transcript}", transcript)
        prompt = apply_language(prompt, language)
        print("Prompt:", prompt)
        response = call_bedrock(prompt)
        print("API Response:", response)
        result[key] = response
    if len(result.keys()) == 1:
        # there's only one summary in here, so let's return just that.
        # this may contain json or a string.
        return result[list(result.keys())[0]]
    return json.dumps(result)


def posixify_filename(filename: str) -> str:
    # Replace all invalid characters with underscores
    regex = r"[^a-zA-Z0-9_.]"
    posix_filename = re.sub(regex, "_", filename)
    # Remove leading and trailing underscores
    posix_filename = re.sub(r"^_+", "", posix_filename)
    posix_filename = re.sub(r"_+$", "", posix_filename)
    return posix_filename


def getKBMetadata(metadata):
    # Keys to include
    keys_to_include = [
        "CallId",
        "CreatedAt",
        "UpdatedAt",
        "Owner",
        "TotalConversationDurationMillis",
    ]
    # Create a new dictionary with only the specified keys
    filtered_metadata = {key: metadata[key] for key in keys_to_include if key in metadata}
    kbMetadata = {"metadataAttributes": filtered_metadata}
    return json.dumps(kbMetadata)


def format_summary(summary, metadata):
    summary_dict = json.loads(summary)
    summary_dict["MEETING NAME"] = metadata["CallId"]
    summary_dict["MEETING DATE AND TIME"] = metadata["CreatedAt"]
    summary_dict["MEETING DURATION (SECONDS)"] = int(
        metadata["TotalConversationDurationMillis"] / 1000
    )
    return json.dumps(summary_dict)


def write_to_s3(callId, metadata, transcript, summary):
    s3 = boto3.client("s3")
    filename = posixify_filename(f"{callId}")
    summary_file_key = f"{S3_PREFIX}{filename}-SUMMARY.txt"
    transcript_file_key = f"{S3_PREFIX}{filename}-TRANSCRIPT.txt"
    summary = format_summary(summary, metadata)
    kbMetadata = getKBMetadata(metadata)
    print(f"KB Summary: {summary}")
    print(f"KB Metadata: {kbMetadata}")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=summary_file_key, Body=summary)
    print(f"Wrote summary to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=f"{summary_file_key}.metadata.json", Body=kbMetadata)
    print(f"Wrote summary metadata to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}.metadata.json")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=transcript_file_key, Body=transcript)
    print(f"Wrote transcript to S3: s3://{S3_BUCKET_NAME}/{transcript_file_key}")
    s3.put_object(
        Bucket=S3_BUCKET_NAME, Key=f"{transcript_file_key}.metadata.json", Body=kbMetadata
    )
    print(
        f"Wrote transcript metadata to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}.metadata.json"
    )


def handler(event, context):
    print("Received event: ", json.dumps(event))
    callId = event["CallId"]
    try:
        transcript_json = get_transcripts(callId)
        transcript = transcript_json["transcript"]
        metadata = transcript_json["metadata"]
        summary = "No summary available"
        prompt_override = None
        if "Prompt" in event:
            prompt_override = event["Prompt"]
        # Per-meeting summary profile ("technical-client", "startup", "team-weekly",
        # ...) and output language, set at meeting-creation time and carried
        # through the END call event. Both optional; absent means today's
        # stack-wide Default+Custom templates, in whatever language they're
        # written in — unchanged behavior for meetings that don't set them.
        summary_profile = event.get("SummaryProfile") or None
        summary_language = event.get("SummaryLanguage") or None
        summary = generate_summary(transcript, prompt_override, summary_profile, summary_language)
        if not prompt_override:
            # only write to S3 when using default summary prompt
            write_to_s3(callId, metadata, transcript, summary)
    except Exception as e:
        print(e)
        summary = "An error occurred."
    print("Returning: ", json.dumps({"summary": summary}))
    return {"summary": summary}


# for testing on terminal
if __name__ == "__main__":
    event = {"CallId": "8cfc6ec4-0dbe-4959-b1f3-34f13359826b"}
    handler(event)
