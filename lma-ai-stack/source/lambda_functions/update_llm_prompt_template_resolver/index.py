"""
Lambda function resolver for updateLLMPromptTemplate mutation
Implements input validation and security filtering

Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
"""

import json
import os
import re
from typing import Any, Dict

import boto3

dynamodb = boto3.resource("dynamodb")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AppSync Lambda resolver for updateLLMPromptTemplate

    Validates and filters input to only allow prompt template fields
    matching the N#LABEL pattern (e.g., 1#SUMMARY, 2#DETAILS).

    Args:
        event: AppSync event with arguments and identity
        context: Lambda context

    Returns:
        Dict with LLMPromptTemplateId and Success status
    """
    try:
        # Get table name from environment
        table_name = os.environ["LLM_PROMPT_TEMPLATE_TABLE_NAME"]
        table = dynamodb.Table(table_name)

        # Extract input from AppSync event
        input_data = event["arguments"]["input"]
        template_id = input_data["LLMPromptTemplateId"]
        should_delete = bool(input_data.get("Delete"))

        # Allow updating: the Custom templates (unchanged), the profile
        # catalog (the list of named summary profiles the UI offers), or a
        # named profile's own template set ("Profile#<slug>"). Anything else
        # is rejected — this allowlist is what stops the mutation being used
        # to write arbitrary DynamoDB items.
        profile_pattern = re.compile(r"^Profile#[A-Za-z0-9_-]+$")
        is_profile_id = bool(profile_pattern.match(template_id))
        if template_id not in ("CustomSummaryPromptTemplates", "SummaryProfileCatalog") and not is_profile_id:
            raise ValueError(
                "Only CustomSummaryPromptTemplates, SummaryProfileCatalog, or Profile#<slug> can be updated"
            )

        if should_delete:
            # Hard delete is only ever appropriate for a single named
            # profile's own item — never the Custom templates or the catalog
            # itself, which every meeting/UI page depends on.
            if not is_profile_id:
                raise ValueError("Only a Profile#<slug> item can be deleted")
            table.delete_item(Key={"LLMPromptTemplateId": template_id})
            print(f"Successfully deleted LLM prompt template: {template_id}")
            return {"LLMPromptTemplateId": template_id, "Success": True}

        template_config_str = input_data.get("TemplateConfig")
        if template_config_str is None:
            raise ValueError("TemplateConfig is required unless Delete is true")

        # Parse the JSON configuration
        try:
            config_object = json.loads(template_config_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in TemplateConfig: {str(e)}")

        # Security: Allowlist only prompt template fields (N#LABEL format)
        # This prevents mass assignment of unexpected DynamoDB attributes
        # Pattern matches: "1#SUMMARY", "2#DETAILS", "3#ACTIONS", etc.
        template_pattern = re.compile(r"^\d+#")

        # Build item with only allowed fields
        item = {"LLMPromptTemplateId": template_id}

        for key, value in config_object.items():
            if template_pattern.match(key):
                item[key] = value
            else:
                print(f"Filtered out non-template field: {key}")

        # Store in DynamoDB
        table.put_item(Item=item)

        print(f"Successfully updated LLM prompt template: {template_id}")

        return {"LLMPromptTemplateId": template_id, "Success": True}

    except Exception as e:
        print(f"Error updating LLM prompt template: {str(e)}")
        raise Exception(f"Failed to update LLM prompt template: {str(e)}")
