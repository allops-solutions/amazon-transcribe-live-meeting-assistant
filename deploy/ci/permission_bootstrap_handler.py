#!/usr/bin/env python3
"""Inactive administrator-only Lambda adapter. Invocation cannot set authority."""
import json
import os

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config

from permission_bootstrap import reconcile

LOGGER = Logger(location='%(filename)s:%(lineno)d - %(funcName)s()')
SESSION = boto3.Session()
CLIENT_CONFIG = Config(retries={'mode': 'adaptive', 'max_attempts': 3})
CFN = SESSION.client('cloudformation', config=CLIENT_CONFIG)
IAM = SESSION.client('iam', config=CLIENT_CONFIG)
BEDROCK = SESSION.client('bedrock', config=CLIENT_CONFIG)
ANALYZER = SESSION.client('accessanalyzer', config=CLIENT_CONFIG)


@LOGGER.inject_lambda_context(log_event=False)
def handler(event, context):
    """Administrator pins are deployment configuration, never caller payloads."""
    if event not in ({}, None):
        raise ValueError('Bootstrap invocation accepts no input fields')
    config = json.loads(os.environ['ADMIN_BOOTSTRAP_CONFIG'])
    result = reconcile(CFN, IAM, BEDROCK, ANALYZER, config,
                       write_enabled=os.environ.get('ENABLE_POLICY_WRITES') == 'true')
    LOGGER.info('Bootstrap reconciliation completed', extra=result)
    return result
