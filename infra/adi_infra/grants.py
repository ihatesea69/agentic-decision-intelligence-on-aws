"""Least-privilege S3 grants. The CDK bucket.grant_* helpers add s3:DeleteObject*, s3:Abort* and, for
grant_put, s3:PutObjectRetention / PutObjectLegalHold, which no ADI component needs (and which would
let a writer extend locks on the audit bucket). These helpers grant exactly GetObject + prefix-scoped
ListBucket for readers and PutObject for writers, plus the KMS calls SSE-KMS needs."""
from __future__ import annotations

from aws_cdk import aws_iam as iam


def read(grantee: iam.IGrantable, bucket, prefix: str, key) -> None:
    iam.Grant.add_to_principal(grantee=grantee, actions=["s3:GetObject"],
                               resource_arns=[bucket.arn_for_objects(f"{prefix}*")])
    iam.Grant.add_to_principal(grantee=grantee, actions=["s3:ListBucket"], resource_arns=[bucket.bucket_arn],
                               conditions={"StringLike": {"s3:prefix": [f"{prefix}*"]}})
    iam.Grant.add_to_principal(grantee=grantee, actions=["kms:Decrypt"], resource_arns=[key.key_arn])


def write(grantee: iam.IGrantable, bucket, prefix: str, key) -> None:
    iam.Grant.add_to_principal(grantee=grantee, actions=["s3:PutObject"],
                               resource_arns=[bucket.arn_for_objects(f"{prefix}*")])
    iam.Grant.add_to_principal(grantee=grantee, actions=["kms:GenerateDataKey", "kms:Decrypt"],
                               resource_arns=[key.key_arn])


def read_write(grantee: iam.IGrantable, bucket, prefix: str, key) -> None:
    read(grantee, bucket, prefix, key)
    write(grantee, bucket, prefix, key)
