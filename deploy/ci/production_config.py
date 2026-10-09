#!/usr/bin/env python3
"""Read-only production configuration check. Never print parameter/secret values."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import boto3

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = '009853297978'
SECRET = 'lma/ci/production/parameters'
DISABLED = ('AcsConnectionString', 'ElevenLabsApiKey', 'ElevenLabsAgentId',
            'SimliApiKey', 'SimliFaceId', 'TavilyApiKey',
            'ZoomMeetingSdkClientId', 'ZoomMeetingSdkClientSecret')


def issues(base, overrides):
    """Return only names/reasons; deliberately exclude values even on errors."""
    if not isinstance(overrides, dict) or not all(isinstance(value, str) for value in overrides.values()):
        raise ValueError('Configuration must be a JSON object of strings')
    values = {entry['ParameterKey']: entry['ParameterValue'] for entry in base}
    if len(values) != len(base) or set(overrides) - set(values):
        raise ValueError('Unknown or duplicate parameter names')
    values.update(overrides)
    found = []
    for key, value in values.items():
        if 'TODO_' in value or 'FILL_IN_FROM_SECRET_STORE' in value:
            found.append({'parameter': key, 'reason': 'unresolved placeholder'})
    for key in ('GoogleOAuthClientId', 'GoogleOAuthClientSecret'):
        if not values.get(key):
            found.append({'parameter': key, 'reason': 'required for production Google SSO'})
    for key in DISABLED:
        if values.get(key) and not any(item['parameter'] == key for item in found):
            found.append({'parameter': key, 'reason': 'must be empty for the approved initial release'})
    expected = {
        'VPLaunchType': 'FARGATE', 'VoiceAssistantProvider': 'none',
        'SsoPoolMode': 'ACTIVE', 'EnableGoogleSso': 'true', 'PasswordSignInEnabled': 'false',
        'GoogleAdminEmail': 'ismail.icanovic@allops.co', 'EnableDataRetentionOnDelete': 'true',
        'SummaryBedrockModelId': 'global.moonshotai.kimi-k3',
        'BedrockModelId': 'global.anthropic.claude-sonnet-4-6',
        'BedrockFallbackModelId': 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
        'MeetingAssistServiceBedrockModelID': 'global.anthropic.claude-sonnet-4-6',
        'TranscriptionEngine': 'AmazonTranscribe',
        'MeetingAssistService': 'STRANDS_BEDROCK_WITH_KB (Create)',
        'TranscriptKnowledgeBase': 'BEDROCK_KNOWLEDGE_BASE (Create)',
        'CreateDocumentSourceBucket': 'true', 'BedrockKnowledgeBaseS3BucketName': '',
        'ModelValidation': 'true',
    }
    for key, value in expected.items():
        if values.get(key) != value:
            found.append({'parameter': key, 'reason': 'does not match the approved initial release'})
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-models', action='store_true', help='Read profile metadata; does not invoke models')
    args = parser.parse_args()
    session = boto3.Session(profile_name='default', region_name='us-east-1')
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise ValueError('Production account authentication required')
    overrides = json.loads(session.client('secretsmanager').get_secret_value(SecretId=SECRET)['SecretString'])
    base = json.loads((ROOT / 'deploy/params/prod.json').read_text())
    found = issues(base, overrides)
    report = {'configurationComplete': not found, 'issues': found,
              'deploymentReady': False, 'note': 'Configuration validation alone does not validate or activate deployment isolation.'}
    if args.check_models:
        spec = importlib.util.spec_from_file_location('model_resources', Path(__file__).with_name('model_resources.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        scopes = module.collect(session.client('bedrock'), ACCOUNT, 'us-east-1')
        report['activeApprovedProfiles'] = len(scopes['profiles'])
        report['inferenceTested'] = False
    print(json.dumps(report, indent=2))
    return 1 if found else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        # SDK exception messages may contain request data; never print them.
        print(f'Production configuration check failed ({type(exc).__name__}); no secret values displayed', file=sys.stderr)
        sys.exit(1)
