"""
Custom Resource Lambda to deploy Lambda@Edge function in us-east-1.
This function creates, updates, and deletes the Lambda@Edge function
that authenticates VNC WebSocket connections for the ECS/Fargate launch
type, using a VP-scoped HMAC token minted by MicrovmVncTokenFunction
(see lma_ai_stack.yaml and source/lambda_functions/microvm_vnc_token).
"""

import io
import json
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError, WaiterError

# cfnresponse module for Python 3.12+
# Based on https://github.com/aws-cloudformation/custom-resource-helper-python
SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(
    event, context, responseStatus, responseData, physicalResourceId=None, noEcho=False, reason=None
):
    """Send response to CloudFormation"""
    responseUrl = event["ResponseURL"]

    responseBody = {
        "Status": responseStatus,
        "Reason": reason or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physicalResourceId or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": noEcho,
        "Data": responseData,
    }

    json_responseBody = json.dumps(responseBody)

    headers = {"content-type": "", "content-length": str(len(json_responseBody))}

    try:
        # nosec B310 - URL is CloudFormation-provided event["ResponseURL"], always HTTPS, AWS-controlled (not attacker-influenced)
        req = urllib.request.Request(  # nosec B310
            responseUrl, data=json_responseBody.encode("utf-8"), headers=headers, method="PUT"
        )
        # URL is CloudFormation-provided event["ResponseURL"], always HTTPS, AWS-controlled.
        with urllib.request.urlopen(req) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            print(f"Status code: {response.status}")
    except Exception as e:
        print(f"send(..) failed executing request: {e}")


# Lambda@Edge function code.
#
# Auth model: the token is a VP-scoped HMAC, not a raw Cognito credential.
# MicrovmVncTokenFunction (a regular, non-edge Lambda behind AppSync) already
# does the real authorization check - caller is the VP's Owner/SharedWith or
# an Admin - before it mints one, binding it to this specific vpId with a
# short TTL. This function's only job is to verify that binding: that the
# token in the URL was produced by someone holding VNC_HMAC_SECRET for THIS
# vpId and hasn't expired. It deliberately does not re-derive caller
# identity from a Cognito token - there is no per-request Cognito check here
# at all, because the token itself already proves the caller passed one
# upstream, for this specific VP, not just some VP.
#
# Previously (until 2026-09-16) this function decoded a raw Cognito ID token
# and checked exp/iss/aud/token_use, but never verified the JWT signature -
# get_jwks() was defined and never called, and all three checked claim
# values are public, so anyone reaching the CloudFront URL could hand-craft
# a fake token with no Cognito account at all. Signature verification was
# added the same day, but even a genuine, correctly-signed token from ANY
# authenticated user still worked for ANY vpId - the check never looked at
# which VP the token was for. This HMAC scheme fixes both: forging a token
# requires the secret (never leaves AWS - see EdgeAuthDeployerFunction /
# MicrovmVncTokenFunction), and a genuine token only works for the one VP it
# was minted for.
EDGE_FUNCTION_CODE = '''
import hashlib
import hmac
import time

HMAC_SECRET = "HMAC_SECRET_PLACEHOLDER"

def verify_vp_scoped_token(token, vp_id):
    """Verify a `<vpId>.<expiry>.<hmac-hex>` token against HMAC_SECRET.

    Constant-time comparison (hmac.compare_digest) so response timing can't
    be used to brute-force the MAC byte by byte.
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            print("Malformed token (expected 3 dot-separated parts)")
            return False
        token_vp_id, expiry_str, mac_hex = parts

        # Bind to the vpId in the request path, not just the URL's own token.
        # Without this, a token minted for one VP would work for any other.
        if token_vp_id != vp_id:
            print(f"Token vpId {token_vp_id!r} does not match path vpId {vp_id!r}")
            return False

        try:
            expiry = int(expiry_str)
        except ValueError:
            print(f"Non-numeric expiry: {expiry_str!r}")
            return False
        if expiry < int(time.time()):
            print(f"Token expired: {expiry}")
            return False

        signing_input = f"{token_vp_id}.{expiry_str}".encode('ascii')
        expected_mac = hmac.new(HMAC_SECRET.encode('utf-8'), signing_input, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac_hex, expected_mac):
            print("HMAC mismatch")
            return False

        return True
    except Exception as ex:
        print(f"Token verification error: {ex}")
        return False

def lambda_handler(event, context):
    """Lambda@Edge handler for viewer request"""
    request = event['Records'][0]['cf']['request']
    uri = request.get('uri', '')
    querystring = request.get('querystring', '')

    print(f"Request URI: {uri}")

    # Only validate /vnc/<vpId> paths
    if not uri.startswith('/vnc/'):
        print("Not a VNC path, allowing through")
        return request

    # /vnc/<vpId> - the ALB listener rule for this vpId is what actually
    # routes to the right ECS task (see status-manager.ts), so the vpId
    # segment here is load-bearing for auth, not just routing: it is what
    # ties the token to this specific VP.
    vp_id = uri[len('/vnc/'):].split('/')[0]
    if not vp_id:
        print("No vpId in path")
        return {
            'status': '400',
            'statusDescription': 'Bad Request',
            'body': 'Missing Virtual Participant id in path',
            'headers': {
                'content-type': [{'key': 'Content-Type', 'value': 'text/plain'}]
            }
        }

    # Extract token from query string parameter
    token = None
    if querystring:
        import urllib.parse
        params = querystring.split('&')
        for param in params:
            if '=' in param:
                key, value = param.split('=', 1)
                if key == 'token':
                    token = urllib.parse.unquote(value)
                    break

    if not token:
        print("No token found in query string")
        return {
            'status': '401',
            'statusDescription': 'Unauthorized',
            'body': 'Authentication required - token parameter missing',
            'headers': {
                'content-type': [{'key': 'Content-Type', 'value': 'text/plain'}]
            }
        }

    if not verify_vp_scoped_token(token, vp_id):
        print("Token verification failed")
        return {
            'status': '403',
            'statusDescription': 'Forbidden',
            'body': 'Invalid or expired token',
            'headers': {
                'content-type': [{'key': 'Content-Type', 'value': 'text/plain'}]
            }
        }

    print("Authentication successful, allowing request")
    return request
'''


def create_zip_file(code_content):
    """Create a zip file containing the Lambda function code"""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("index.py", code_content)
    zip_buffer.seek(0)
    return zip_buffer.read()


def wait_for_function_updated(lambda_client, function_name):
    """
    Wait for $LATEST code update to finish applying (LastUpdateStatus == Successful).
    Required between successive update_function_code calls.
    """
    try:
        waiter = lambda_client.get_waiter("function_updated_v2")
        waiter.wait(
            FunctionName=function_name,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},  # up to 5 minutes
        )
        print(f"Function {function_name} $LATEST update completed (LastUpdateStatus=Successful)")
    except WaiterError as e:
        print(f"Timed out waiting for function {function_name} update to finish: {e}")
        raise


def wait_for_version_active(lambda_client, function_name, version):
    """
    Wait for a specific published Lambda version to reach State == Active.
    Required for Lambda@Edge: CloudFront rejects associations to versions
    that are still in Pending state.
    """
    try:
        waiter = lambda_client.get_waiter("function_active_v2")
        waiter.wait(
            FunctionName=function_name,
            Qualifier=version,
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},  # up to 5 minutes
        )
        print(f"Function {function_name}:{version} is now Active")
    except WaiterError as e:
        print(f"Timed out waiting for function {function_name}:{version} to become Active: {e}")
        raise


def create_edge_function(lambda_client, function_name, role_arn, hmac_secret):
    """Create Lambda@Edge function in us-east-1"""
    code = EDGE_FUNCTION_CODE.replace("HMAC_SECRET_PLACEHOLDER", hmac_secret)

    # Create zip file
    zip_content = create_zip_file(code)

    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="index.lambda_handler",
            Code={"ZipFile": zip_content},
            Description="Lambda@Edge function for VNC WebSocket authentication",
            Timeout=5,
            MemorySize=128,
            Publish=True,  # Must publish for Lambda@Edge
        )

        # When Publish=True, the response includes Version field
        # We need to construct the versioned ARN manually
        version = response.get("Version", "1")
        function_arn = response["FunctionArn"]

        # If the ARN doesn't already have a version, append it
        if not function_arn.split(":")[-1].isdigit():
            versioned_arn = f"{function_arn}:{version}"
        else:
            versioned_arn = function_arn

        print(f"Created function with version {version}, ARN: {versioned_arn}")

        # Newly-published Lambda versions start in Pending state. Lambda@Edge /
        # CloudFront associations require State=Active, so we must wait before
        # returning the versioned ARN to CloudFormation.
        wait_for_version_active(lambda_client, function_name, version)

        return versioned_arn

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            # Function already exists, update it
            return update_edge_function(lambda_client, function_name, hmac_secret)
        raise


def update_edge_function(lambda_client, function_name, hmac_secret):
    """Update existing Lambda@Edge function"""
    code = EDGE_FUNCTION_CODE.replace("HMAC_SECRET_PLACEHOLDER", hmac_secret)

    # Create zip file
    zip_content = create_zip_file(code)

    # Make sure any previous in-flight update on $LATEST has finished before we
    # try to publish a new version (otherwise we can hit ResourceConflictException).
    wait_for_function_updated(lambda_client, function_name)

    # Update function code and publish new version
    response = lambda_client.update_function_code(
        FunctionName=function_name,
        ZipFile=zip_content,
        Publish=True,  # Must publish for Lambda@Edge
    )

    # Construct versioned ARN
    version = response.get("Version", "1")
    function_arn = response["FunctionArn"]

    # If the ARN doesn't already have a version, append it
    if not function_arn.split(":")[-1].isdigit():
        versioned_arn = f"{function_arn}:{version}"
    else:
        versioned_arn = function_arn

    print(f"Updated function with version {version}, ARN: {versioned_arn}")

    # Newly-published Lambda versions start in Pending state. Lambda@Edge /
    # CloudFront associations require State=Active, so we must wait before
    # returning the versioned ARN to CloudFormation. Without this wait,
    # CloudFront rejects the association with:
    #   "The function must be in an Active state.
    #    The current state for function ...:N is Pending"
    wait_for_version_active(lambda_client, function_name, version)

    return versioned_arn


def delete_edge_function(lambda_client, function_name):
    """
    Delete Lambda@Edge function.
    Note: Lambda@Edge replicated functions can't be deleted immediately.
    They must be disassociated from CloudFront first and can take hours to replicate.
    """
    try:
        # List all versions
        versions = lambda_client.list_versions_by_function(FunctionName=function_name)

        # Delete all versions except $LATEST
        for version in versions.get("Versions", []):
            if version["Version"] != "$LATEST":
                try:
                    lambda_client.delete_function(
                        FunctionName=function_name, Qualifier=version["Version"]
                    )
                    print(f"Deleted version {version['Version']}")
                except ClientError as e:
                    error_code = e.response["Error"]["Code"]
                    if (
                        error_code == "InvalidParameterValueException"
                        and "replicated function" in str(e)
                    ):
                        print(
                            f"Version {version['Version']} is replicated - will be deleted automatically after CloudFront disassociation"
                        )
                    else:
                        print(f"Error deleting version {version['Version']}: {e}")

        # Try to delete the function
        try:
            lambda_client.delete_function(FunctionName=function_name)
            print(f"Deleted function: {function_name}")
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "InvalidParameterValueException" and "replicated function" in str(e):
                print(
                    f"Function {function_name} is replicated - will be deleted automatically (can take 1-2 hours)"
                )
                print("This is expected behavior for Lambda@Edge functions")
                # Don't raise - this is expected and will clean up automatically
            else:
                raise

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            print(f"Function {function_name} not found - already deleted")
        else:
            raise


def handler(event, context):
    """Custom resource handler"""
    # ResourceProperties only ever holds the secret's ARN, never its value -
    # that value is fetched below via get_secret_value() and embedded
    # straight into the zip, so it never appears in this event and never
    # gets logged here.
    print(f"Event: {json.dumps(event)}")

    response_data = {}
    physical_resource_id = event.get("PhysicalResourceId", "EdgeAuthFunction")

    try:
        # Get properties
        props = event["ResourceProperties"]
        function_name = props["FunctionName"]
        role_arn = props["RoleArn"]
        hmac_secret_arn = props["HmacSecretArn"]

        # Create clients for us-east-1 (this custom resource, the Lambda@Edge
        # function it deploys, and VncHmacSecret all live in the same
        # us-east-1 stack - see "Locked decisions" in LMA-HANDOFF.md).
        lambda_client = boto3.client("lambda", region_name="us-east-1")
        secretsmanager_client = boto3.client("secretsmanager", region_name="us-east-1")

        if event["RequestType"] in ["Create", "Update"]:
            hmac_secret = secretsmanager_client.get_secret_value(SecretId=hmac_secret_arn)[
                "SecretString"
            ]

            # Create or update function
            function_arn = create_edge_function(lambda_client, function_name, role_arn, hmac_secret)

            # Return the versioned ARN (required for Lambda@Edge)
            response_data["FunctionArn"] = function_arn
            physical_resource_id = function_arn

            print(f"Function ARN: {function_arn}")
            send(event, context, SUCCESS, response_data, physical_resource_id)

        elif event["RequestType"] == "Delete":
            # Delete function (may not complete immediately for Lambda@Edge)
            try:
                delete_edge_function(lambda_client, function_name)
                send(event, context, SUCCESS, response_data, physical_resource_id)
            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "InvalidParameterValueException" and "replicated function" in str(
                    e
                ):
                    # Lambda@Edge replication - this is expected, return success
                    print(
                        "Lambda@Edge function will be deleted automatically after replication cleanup"
                    )
                    send(
                        event,
                        context,
                        SUCCESS,
                        response_data,
                        physical_resource_id,
                        reason="Lambda@Edge function marked for deletion (will complete automatically)",
                    )
                else:
                    raise

    except Exception as e:
        print(f"Error: {str(e)}")
        send(event, context, FAILED, {}, physical_resource_id, reason=str(e))
