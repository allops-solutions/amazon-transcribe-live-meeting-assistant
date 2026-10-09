#!/usr/bin/env python3
"""Read exact approved Bedrock profile/model scopes; never invoke a model."""
import re

PROFILES = (
    'global.anthropic.claude-sonnet-4-6',
    'global.moonshotai.kimi-k3',
    'global.anthropic.claude-haiku-4-5-20251001-v1:0',
)
EMBEDDING_MODEL = 'amazon.titan-embed-text-v2:0'


def collect(client, account, region, partition='aws'):
    """Validate returned profiles and destinations against the approved release."""
    if not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('Invalid model inventory account')
    if not re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-[0-9]', region):
        raise ValueError('Invalid model inventory region')
    if partition not in {'aws', 'aws-cn', 'aws-us-gov'}:
        raise ValueError('Unsupported partition')
    profiles, models = [], []
    for identifier in PROFILES:
        result = client.get_inference_profile(inferenceProfileIdentifier=identifier)
        expected = f'arn:{partition}:bedrock:{region}:{account}:inference-profile/{identifier}'
        if result.get('inferenceProfileArn') != expected or result.get('status') != 'ACTIVE':
            raise ValueError('Approved inference profile is unavailable or changed identity')
        destinations = result.get('models', [])
        if not destinations:
            raise ValueError('Approved inference profile has no destinations')
        model = identifier.removeprefix('global.')
        pattern = rf'arn:{re.escape(partition)}:bedrock:(?:[a-z]{{2}}(?:-gov)?-[a-z]+-[0-9])?::foundation-model/{re.escape(model)}'
        for destination in destinations:
            value = destination.get('modelArn', '')
            if not re.fullmatch(pattern, value):
                raise ValueError('Inference destination is not an exact approved foundation model')
            models.append(value)
        profiles.append(expected)
    models.append(f'arn:{partition}:bedrock:{region}::foundation-model/{EMBEDDING_MODEL}')
    return {'profiles': sorted(set(profiles)), 'models': sorted(set(models))}


def validate(scopes, account, region, partition='aws'):
    """Reject wildcard, foreign and unapproved model scopes supplied to a builder."""
    if not isinstance(scopes, dict) or set(scopes) != {'profiles', 'models'}:
        raise ValueError('Expected approved profile and model scopes')
    for key, values in scopes.items():
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError('Model scopes must be ARN lists')
        for value in values:
            if key == 'profiles':
                valid = {f'arn:{partition}:bedrock:{region}:{account}:inference-profile/{identifier}' for identifier in PROFILES}
                if value not in valid:
                    raise ValueError('Unapproved inference profile')
            else:
                ids = [identifier.removeprefix('global.') for identifier in PROFILES] + [EMBEDDING_MODEL]
                pattern = rf'arn:{re.escape(partition)}:bedrock:(?:[a-z]{{2}}(?:-gov)?-[a-z]+-[0-9])?::foundation-model/(?:' + '|'.join(re.escape(i) for i in ids) + ')'
                if not re.fullmatch(pattern, value):
                    raise ValueError('Unapproved foundation model')
    return scopes
