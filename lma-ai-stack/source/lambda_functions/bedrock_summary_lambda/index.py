# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
# Generates text-only summaries through Bedrock InvokeModel (Kimi) or Converse.

import json
import logging
import math
import os
import re
import time
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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
# Kimi uses part of the output budget for reasoning. Keep this separate so
# fallback/other providers are not sent a larger, potentially unsupported limit.
KIMI_SUMMARY_MAX_TOKENS = int(os.getenv("KIMI_SUMMARY_MAX_TOKENS", "16384"))
KIMI_SUMMARY_EFFORT = os.getenv("KIMI_SUMMARY_EFFORT", "medium").strip().lower()
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


# Transcript retrieval is bounded too; it must not exhaust the model deadline.
lambda_client = boto3.client(
    "lambda",
    config=Config(
        retries={"total_max_attempts": 1, "mode": "standard"},
        connect_timeout=5,
        read_timeout=60,
    ),
)
dynamodb_client = boto3.client("dynamodb")
# A read timeout is ambiguous: Bedrock may still be generating/billing. Never
# let the SDK resubmit it. Only an explicit throttling rejection is retried,
# once, under the SAME model and invocation deadlines.
PRIMARY_TIMEOUT_SECONDS = 300
FALLBACK_TIMEOUT_SECONDS = 120
GENERATION_BUDGET_SECONDS = 540
FINALIZE_RESERVE_SECONDS = 30
REQUEST_OVERHEAD_SECONDS = 10  # connect timeout + signing/response handling


@lru_cache(maxsize=8)
def bedrock_client(read_timeout):
    """Reuse single-attempt clients with timeouts bounded by the current budget."""
    return boto3.client(
        service_name="bedrock-runtime",
        region_name=BEDROCK_REGION,
        endpoint_url=BEDROCK_ENDPOINT_URL,
        config=Config(
            retries={"total_max_attempts": 1, "mode": "standard"},
            connect_timeout=5,
            read_timeout=read_timeout,
        ),
    )


bedrock = bedrock_client(PRIMARY_TIMEOUT_SECONDS)


class SummaryDeadlineError(TimeoutError):
    """There is no safe time left for another generation request."""


def generation_deadline(context=None):
    """Share one model budget across all sections, leaving time to save/fail."""
    budget = GENERATION_BUDGET_SECONDS
    if context is not None:
        budget = min(
            budget, context.get_remaining_time_in_millis() / 1000 - FINALIZE_RESERVE_SECONDS
        )
    return time.monotonic() + max(0, budget)


def get_templates_from_dynamodb(prompt_override):
    templates = []
    prompt_template_str = None

    if prompt_override is not None:
        print("Using ad-hoc prompt override")
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

            # Custom, when it has any sections, is the complete set — the same
            # rule named profiles follow, and what the Transcript Summary page
            # shows. Merging it over Default silently kept Default's other
            # sections (and their Bedrock calls) running behind a page that
            # showed only the custom one.
            customSections = {
                k: v
                for k, v in customPromptTemplates.items()
                if k not in ("LLMPromptTemplateId", "*Information*")
            }
            if customSections:
                mergedPromptTemplates = customSections
                print("Using custom templates as the complete set")
            else:
                mergedPromptTemplates = defaultPromptTemplates
                print("No custom templates — using defaults")

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
    its complete set of sections. A missing/deleted profile fails without
    silently generating from a different prompt set."""
    try:
        response = dynamodb_client.get_item(
            Key={"LLMPromptTemplateId": {"S": f"Profile#{profile_id}"}},
            TableName=LLM_PROMPT_TEMPLATE_TABLE_NAME,
        )
        item = response.get("Item")
        if not item:
            raise ValueError(
                f"Summary profile '{profile_id}' no longer exists. Choose another profile."
            )

        templates = []
        for k in sorted(item):
            if k in ("LLMPromptTemplateId", "*Information*"):
                continue
            prompt = item[k]["S"]
            if prompt and prompt != "NONE":
                prompt = prompt.replace("<br>", "\n")
                index = k.find("#")
                k_stripped = k[index + 1 :]
                templates.append({k_stripped: prompt})
        if not templates:
            raise ValueError("The selected summary profile has no sections.")
        return templates
    except Exception as e:
        print(f"Error loading summary profile '{profile_id}': {e}")
        raise


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
    transcript_data = response["Payload"].read().decode()
    transcript_json = json.loads(transcript_data)
    print("Fetched transcript characters:", len(transcript_json.get("transcript", "")))
    return transcript_json


def get_generated_text(response):
    # With thinking enabled the first block is reasoningContent; return the first text block.
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            return block["text"]
    raise ValueError("No text block in Bedrock response")


class SummaryOutputLimitError(ValueError):
    """A model stopped before completing the requested summary."""


def build_inference_args(modelId):
    """inferenceConfig plus provider-specific reasoning/effort fields.

    SUMMARY_EFFORT: off | low | medium | high | max (max is Anthropic-only).
    Verified live per provider on 2026-09-15:
      Anthropic: thinking.type=adaptive + output_config.effort, temperature must be 1
      OpenAI:    reasoning_effort (low|medium|high)
      Nova 2:    reasoningConfig.type=enabled + maxReasoningEffort (low|medium|high),
                 temperature must be 0
    """
    inference_config = {
        "maxTokens": KIMI_SUMMARY_MAX_TOKENS if "moonshotai.kimi" in modelId else SUMMARY_MAX_TOKENS
    }
    # Kimi K3 rejects the temperature field even when it is set to zero.
    # Keep provider-specific optional fields out of the shared request.
    if "moonshotai.kimi" not in modelId:
        inference_config["temperature"] = 0
    args = {"inferenceConfig": inference_config}
    effort = SUMMARY_EFFORT
    if effort in ("", "off", "none", "0"):
        return args
    if "anthropic" in modelId and "claude-haiku-4-5" not in modelId:
        args["inferenceConfig"]["temperature"] = 1
        args["additionalModelRequestFields"] = {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    elif "openai" in modelId:
        args["additionalModelRequestFields"] = {
            "reasoning_effort": "high" if effort == "max" else effort
        }
    elif "nova-2" in modelId:
        args["additionalModelRequestFields"] = {
            "reasoningConfig": {
                "type": "enabled",
                "maxReasoningEffort": "high" if effort == "max" else effort,
            }
        }
    # Kimi K3 bypasses Converse entirely so its native effort field is honored.
    # Other providers receive only their own supported optional settings.
    return args


def build_kimi_request(prompt_data):
    """Use the native API's validated effort field, never an implicit max default."""
    if KIMI_SUMMARY_EFFORT not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported Kimi summary reasoning effort.")
    return {
        "messages": [{"role": "user", "content": prompt_data}],
        "max_tokens": KIMI_SUMMARY_MAX_TOKENS,
        "reasoning_effort": KIMI_SUMMARY_EFFORT,
    }


def invoke_kimi(client, model_id, request):
    """Normalize a native Chat Completion without exposing its reasoning content."""
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps(request),
        contentType="application/json",
        accept="application/json",
    )
    body = response["body"]
    try:
        result = json.loads(body.read())
    finally:
        body.close()
    choice = result["choices"][0]
    message = choice.get("message") or {}
    text = message.get("content")
    usage = result.get("usage") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    finish_reason = choice.get("finish_reason")
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "model_context_window_exceeded": "model_context_window_exceeded",
    }.get(finish_reason, "unexpected_finish")
    # Unknown counts remain null; character lengths are not token estimates.
    return {
        "output": {"message": {"content": [{"text": text}] if isinstance(text, str) else []}},
        "stopReason": stop_reason,
        "usage": {
            "inputTokens": usage.get("prompt_tokens"),
            "outputTokens": usage.get("completion_tokens"),
            "totalTokens": usage.get("total_tokens"),
            "reasoningTokens": completion_details.get("reasoning_tokens"),
            "cacheReadInputTokens": prompt_details.get("cached_tokens"),
            "cacheWriteInputTokens": prompt_details.get("cache_write_tokens"),
        },
        "summaryTextCharacters": len(text) if isinstance(text, str) else 0,
    }


def log_usage(model_id, api, response):
    """Log only whitelisted usage metadata, never model text or reasoning."""
    usage = response.get("usage") or {}
    counts = {
        key: usage.get(key)
        for key in (
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "reasoningTokens",
            "cacheReadInputTokens",
            "cacheWriteInputTokens",
        )
    }
    # Providers need not report reasoning separately. Do not invent that split.
    output, reasoning = counts["outputTokens"], counts["reasoningTokens"]
    counts["nonReasoningOutputTokens"] = (
        output - reasoning
        if type(output) is int and type(reasoning) is int and 0 <= reasoning <= output
        else None
    )
    print(
        "Bedrock usage:",
        json.dumps(
            {
                "modelId": model_id,
                "api": api,
                "stopReason": response.get("stopReason"),
                "summaryTextCharacters": response.get("summaryTextCharacters"),
                **counts,
            }
        ),
    )


def _generate_section(modelId, prompt_data, deadline=None, timeout_seconds=PRIMARY_TIMEOUT_SECONDS):
    """One generation attempt; one extra request only for explicit throttling."""
    native_kimi = "moonshotai.kimi-k3" in modelId
    api = "InvokeModel" if native_kimi else "Converse"
    print("Bedrock request - ModelId", modelId, "API", api)
    message = {"role": "user", "content": [{"text": prompt_data}]}
    args = build_kimi_request(prompt_data) if native_kimi else build_inference_args(modelId)
    print(
        "Bedrock inference settings:",
        json.dumps({k: v for k, v in args.items() if k != "messages"}),
    )
    started = time.monotonic()
    deadline = min(
        deadline if deadline is not None else float("inf"),
        started + timeout_seconds + REQUEST_OVERHEAD_SECONDS,
    )
    try:
        for attempt in range(2):
            remaining = deadline - time.monotonic() - REQUEST_OVERHEAD_SECONDS
            if remaining < 1:
                raise SummaryDeadlineError("Not enough time left for summary generation.")
            read_timeout = min(timeout_seconds, math.ceil(remaining))
            client = (
                bedrock if read_timeout == PRIMARY_TIMEOUT_SECONDS else bedrock_client(read_timeout)
            )
            try:
                response = (
                    invoke_kimi(client, modelId, args)
                    if native_kimi
                    else client.converse(modelId=modelId, messages=[message], **args)
                )
                if time.monotonic() > deadline:
                    raise SummaryDeadlineError(
                        "Summary response arrived after the generation deadline."
                    )
                break
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if attempt or code not in ("ThrottlingException", "TooManyRequestsException"):
                    raise
                if deadline - time.monotonic() <= REQUEST_OVERHEAD_SECONDS + 2:
                    raise SummaryDeadlineError(
                        "No time left to retry a throttled request."
                    ) from error
                time.sleep(1)
    except Exception:
        print(f"Bedrock {api} for {modelId} failed after {time.monotonic() - started:.1f}s")
        raise
    print(f"Bedrock {api} for {modelId} took {time.monotonic() - started:.1f}s")
    log_usage(modelId, api, response)
    if response.get("stopReason") in ("max_tokens", "model_context_window_exceeded"):
        # Check before extracting text: even a plausible, nonempty result is
        # incomplete, and must never replace a saved summary or KB export.
        raise SummaryOutputLimitError("Summary reached the model output/context limit.")
    if native_kimi and response["stopReason"] != "end_turn":
        raise ValueError("Kimi did not finish a summary normally.")
    generated_text = get_generated_text(response)
    if not isinstance(generated_text, str) or not generated_text.strip():
        raise ValueError("The model returned an empty summary section.")
    return generated_text


def call_bedrock(prompt_data, deadline=None):
    """Try the primary, then fallback once, without replaying ambiguous failures."""
    deadline = deadline if deadline is not None else generation_deadline()
    has_fallback = BEDROCK_FALLBACK_MODEL_ID and BEDROCK_FALLBACK_MODEL_ID != BEDROCK_MODEL_ID
    fallback_reserve = FALLBACK_TIMEOUT_SECONDS + REQUEST_OVERHEAD_SECONDS if has_fallback else 0
    try:
        return _generate_section(BEDROCK_MODEL_ID, prompt_data, deadline - fallback_reserve)
    except Exception as primary_error:
        if not has_fallback:
            raise
        print(
            f"Primary model '{BEDROCK_MODEL_ID}' failed ({type(primary_error).__name__}); "
            f"retrying with fallback model '{BEDROCK_FALLBACK_MODEL_ID}'"
        )
        try:
            return _generate_section(
                BEDROCK_FALLBACK_MODEL_ID, prompt_data, deadline, FALLBACK_TIMEOUT_SECONDS
            )
        except Exception as fallback_error:
            print(
                f"Fallback model '{BEDROCK_FALLBACK_MODEL_ID}' also failed: "
                f"{type(fallback_error).__name__}"
            )
            raise


def generate_summary(transcript, prompt_override, profile_id=None, language=None, deadline=None):
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
        print("Summary section:", key, "prompt characters:", len(prompt))
        response = call_bedrock(prompt, deadline) if deadline is not None else call_bedrock(prompt)
        if not isinstance(response, str) or not response.strip():
            raise ValueError("The model returned an empty summary section.")
        print("Summary section characters:", len(response))
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
    # Single-section profiles may deliberately return Markdown/plain text.
    # Preserve the model text instead of requiring every prompt to return JSON.
    try:
        summary_dict = json.loads(summary)
    except (TypeError, ValueError):
        summary_dict = {"SUMMARY": summary}
    if not isinstance(summary_dict, dict):
        summary_dict = {"SUMMARY": summary}
    summary_dict["MEETING NAME"] = metadata["CallId"]
    summary_dict["MEETING DATE AND TIME"] = metadata["CreatedAt"]
    summary_dict["MEETING DURATION (SECONDS)"] = int(
        metadata["TotalConversationDurationMillis"] / 1000
    )
    return json.dumps(summary_dict)


def write_transcript_to_s3(callId, metadata, transcript):
    """Archive the transcript (+ KB metadata) so the Knowledge Base ingests every
    meeting, whether or not a summary is ever generated."""
    s3 = boto3.client("s3")
    filename = posixify_filename(f"{callId}")
    transcript_file_key = f"{S3_PREFIX}{filename}-TRANSCRIPT.txt"
    kbMetadata = getKBMetadata(metadata)
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=transcript_file_key, Body=transcript)
    print(f"Wrote transcript to S3: s3://{S3_BUCKET_NAME}/{transcript_file_key}")
    s3.put_object(
        Bucket=S3_BUCKET_NAME, Key=f"{transcript_file_key}.metadata.json", Body=kbMetadata
    )
    print(
        f"Wrote transcript metadata to S3: s3://{S3_BUCKET_NAME}/{transcript_file_key}.metadata.json"
    )


def write_to_s3(callId, metadata, transcript, summary):
    s3 = boto3.client("s3")
    filename = posixify_filename(f"{callId}")
    summary_file_key = f"{S3_PREFIX}{filename}-SUMMARY.txt"
    summary = format_summary(summary, metadata)
    kbMetadata = getKBMetadata(metadata)
    print("KB summary characters:", len(summary))
    print(f"KB Metadata: {kbMetadata}")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=summary_file_key, Body=summary)
    print(f"Wrote summary to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}")
    s3.put_object(Bucket=S3_BUCKET_NAME, Key=f"{summary_file_key}.metadata.json", Body=kbMetadata)
    print(f"Wrote summary metadata to S3: s3://{S3_BUCKET_NAME}/{summary_file_key}.metadata.json")
    write_transcript_to_s3(callId, metadata, transcript)


def handler(event, context):
    print(
        "Received summary request:",
        json.dumps(
            {key: event.get(key) for key in ("CallId", "SummaryProfile", "SummaryRequestedAt")}
        ),
    )
    callId = event["CallId"]
    try:
        deadline = generation_deadline(context)
        transcript_json = get_transcripts(callId)
        transcript = transcript_json["transcript"]
        metadata = transcript_json["metadata"]
        if event.get("TranscriptOnly"):
            # End-of-call in ON_DEMAND mode: archive for the Knowledge Base,
            # spend nothing on Bedrock. A summary comes later, if asked for.
            write_transcript_to_s3(callId, metadata, transcript)
            return {"summary": None}
        summary = "No summary available"
        prompt_override = None
        if "Prompt" in event:
            prompt_override = event["Prompt"]
        # On-demand requests carry an explicitly selected profile. Legacy
        # automatic/debug requests can still use the Default+Custom templates.
        # Language comes exclusively from the prompt, never a saved override.
        summary_profile = event.get("SummaryProfile") or None
        summary_language = None
        summary = generate_summary(
            transcript, prompt_override, summary_profile, summary_language, deadline
        )
        if not prompt_override:
            # Archive normal profile/default runs, not ad-hoc debug prompts.
            write_to_s3(callId, metadata, transcript, summary)
    except SummaryOutputLimitError:
        return {
            "summary": None,
            "errorCode": "OUTPUT_LIMIT",
            "error": (
                "Summary exceeded the model output/context limit. The previous summary was kept. "
                "Use a more concise profile or ask an administrator to review the output budget."
            ),
        }
    except Exception as e:
        print(e)
        # Never overwrite a previous summary with an error placeholder.
        return {
            "summary": None,
            "error": "Summary generation failed. The previous summary was kept.",
        }
    print("Returning summary characters:", len(summary))
    return {"summary": summary}


# for testing on terminal
if __name__ == "__main__":
    event = {"CallId": "8cfc6ec4-0dbe-4959-b1f3-34f13359826b"}
    handler(event)
