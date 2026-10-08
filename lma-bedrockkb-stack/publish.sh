#!/bin/bash
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

##############################################################################################
# Create new Cfn artifacts bucket if not already existing, and publish template and artifacts
# usage: ./publish.sh <cfn_bucket> <cfn_prefix> <region> [public]
##############################################################################################
# Stop the publish process on failures
set -e

# use current directory name as template name
NAME=$(basename `pwd`)

USAGE="$0 <cfn_bucket> <cfn_prefix> <region> [public]"

BUCKET=$1
[ -z "$BUCKET" ] && echo "Cfn bucket name is required parameter. Usage $USAGE" && exit 1

PREFIX=$2
[ -z "$PREFIX" ] && echo "Prefix is required parameter. Usage $USAGE" && exit 1

# Remove trailing slash from prefix if needed
[[ "${PREFIX}" == */ ]] && PREFIX="${PREFIX%?}"

REGION=$3
[ -z "$REGION" ] && echo "Region is a required parameter. Usage $USAGE" && exit 1
export AWS_DEFAULT_REGION=$REGION

ACL=$4
if [ "$ACL" == "public" ]; then
  echo "Published S3 artifacts will be acessible by public (read-only)"
  PUBLIC=true
else
  echo "Published S3 artifacts will NOT be acessible by public."
  PUBLIC=false
fi

# Check only the requested bucket. Access/network failures do not mean it is missing.
if bucket_error=$(aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>&1); then
  echo "Using existing bucket: $BUCKET"
else
  case "$bucket_error" in
    *"(404)"*|*"(NoSuchBucket)"*|*"(NotFound)"*)
      echo "Creating s3 bucket: $BUCKET"
      aws s3 mb "s3://${BUCKET}" --region "$REGION" || exit 1
      aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled --region "$REGION" || exit 1
      ;;
    *)
      printf 'Cannot access S3 bucket %s: %s\n' "$BUCKET" "$bucket_error" >&2
      exit 1
      ;;
  esac
fi

echo -n "Make temp dir: "
timestamp=$(date "+%Y%m%d_%H%M")
tmpdir=/tmp/$NAME
[ -d $tmpdir ] && rm -fr $tmpdir
mkdir -p $tmpdir

template=template.yaml
s3_template="s3://${BUCKET}/${PREFIX}/${NAME}/template.yaml"
https_template="https://s3.${REGION}.amazonaws.com/${BUCKET}/${PREFIX}/${NAME}/template.yaml"

echo "PACKAGING $NAME"
aws cloudformation package \
--template-file ${template} \
--output-template-file ${tmpdir}/${template} \
--s3-bucket $BUCKET --s3-prefix ${PREFIX}/${NAME} \
--region ${REGION} || exit 1
echo "Uploading template file to: ${s3_template}"
aws s3 cp ${tmpdir}/${template} ${s3_template}
echo "Validating template"
aws cloudformation validate-template --template-url ${https_template} > /dev/null || exit 1
echo "Validated: ${https_template}"

if $PUBLIC; then
  echo "Setting public read ACLs on published artifacts"
  files=$(aws s3api list-objects --bucket ${BUCKET} --prefix ${PREFIX} --query "(Contents)[].[Key]" --output text)
  for file in $files
    do
    aws s3api put-object-acl --acl public-read --bucket ${BUCKET} --key $file
    done
fi

echo Published $NAME - Template URL: $https_template
exit 0
