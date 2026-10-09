#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Read-only AWS validation of the inactive speech/text capability ceilings."""
import argparse
import json

import boto3

from runtime_boundary import build_policy


FAMILIES = {
    'speech': (['Logs', 'StreamingTranscription'],
               ['transcribe:StartStreamTranscription', 'transcribe:StartCallAnalyticsStreamTranscription'],
               ['transcribe:GetTranscriptionJob', 'transcribe:ListTranscriptionJobs',
                'transcribe:GetVocabulary', 'iam:PassRole']),
    'text': (['Logs', 'TextProcessing'],
             ['comprehend:DetectSentiment', 'comprehend:DetectPiiEntities',
              'comprehend:DetectDominantLanguage', 'translate:TranslateText'],
             ['comprehend:CreateEndpoint', 'comprehend:DescribeSentimentDetectionJob',
              'translate:TranslateDocument', 'iam:PassRole']),
}


def check(iam, analyzer, account, region, family):
    """Simulate with a hypothetical broad identity grant and a real boundary input.

    Simulators supply aws:RequestedRegion by default when ContextEntries omits
    it. Use explicit wrong/empty values, not omission, for the negative cases.
    No calls here grant, attach or invoke application permissions.
    """
    capabilities, allowed, forbidden = FAMILIES[family]
    policy = build_policy(account, region, f'allops-lma-ci-{account}-production-{region}',
                          capabilities=capabilities)
    validation = analyzer.validate_policy(policyDocument=json.dumps(policy), policyType='IDENTITY_POLICY')
    if validation.get('findings') != [] or validation.get('nextToken'):
        raise ValueError('Capability validation requires review; no IAM writes performed')
    permissive = {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}
    cases = 0
    foreign_region = 'eu-west-1' if region != 'eu-west-1' else 'us-east-1'
    for action in allowed + forbidden:
        for requested in [region, foreign_region, '']:
            expected = 'allowed' if action in allowed and requested == region else 'explicitDeny'
            result = iam.simulate_custom_policy(
                PolicyInputList=[json.dumps(permissive)],
                PermissionsBoundaryPolicyInputList=[json.dumps(policy)],
                ActionNames=[action], ResourceArns=['*'],
                ContextEntries=[{'ContextKeyName': 'aws:RequestedRegion',
                                 'ContextKeyValues': [requested], 'ContextKeyType': 'string'}],
            )
            evaluations = result.get('EvaluationResults', [])
            if (result.get('IsTruncated') or len(evaluations) != 1
                    or evaluations[0].get('EvalDecision') != expected):
                raise RuntimeError(f'{family}: unexpected simulation result for {action}')
            cases += 1
    return {'family': family, 'policyCharacters': len(json.dumps(policy, separators=(',', ':'))),
            'analyzerFindings': 0, 'simulationCasesPassed': cases,
            'attached': False, 'deploymentReady': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()
    # Validate the target before opening any SDK session.
    build_policy(args.account, args.region,
                 f'allops-lma-ci-{args.account}-production-{args.region}', capabilities=['Logs'])
    session = boto3.Session(profile_name='default', region_name=args.region)
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('Authenticated account differs from target')
    for family in FAMILIES:
        print(json.dumps(check(session.client('iam'), session.client('accessanalyzer'),
                               args.account, args.region, family)))


if __name__ == '__main__':
    main()
