"""cdk-nag suppressions. Findings were fixed first (S3 grants narrowed to GetObject / PutObject /
prefix-scoped ListBucket, Glue asset access narrowed to two objects). What remains is intended,
and each suppression says why. Resource-level where the finding is unique to one construct."""
from __future__ import annotations

import aws_cdk as cdk
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo

P = "Policy::arn:<AWS::Partition>:iam::aws:policy/"


def S(rule: str, reason: str, applies=None) -> NagPackSuppression:
    return NagPackSuppression(id=rule, reason=reason, applies_to=applies)


STACK_WIDE = [
    S("AwsSolutions-IAM5", "KMS actions from CDK grant helpers (DynamoDB, SNS, SQS), scoped to the one customer-managed "
      "key ARN; GenerateDataKey* covers the WithoutPlaintext variant the SDKs call.",
      ["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"]),
    S("AwsSolutions-IAM5", "Object access is limited to one fixed prefix of one bucket (runs/, config/, actions/, "
      "deidentified/); object keys under it are generated per run.",
      [RegexAppliesTo(regex="/^Resource::<[A-Za-z0-9]+\\.Arn>\\/(runs|config|actions|deidentified)\\/\\*$/g")]),
    S("AwsSolutions-IAM5", "Step Functions and EventBridge Scheduler invoke a fixed function; the :* suffix only "
      "covers its version qualifiers.", [RegexAppliesTo(regex="/^Resource::<[A-Za-z0-9]+\\.Arn>:\\*$/g")]),
    S("AwsSolutions-IAM4", "AWS-maintained Lambda execution policies: log writes plus the ENI management a function "
      "needs to run inside the isolated VPC subnets.",
      [P + "service-role/AWSLambdaBasicExecutionRole", P + "service-role/AWSLambdaVPCAccessExecutionRole"]),
]

BY_PATH = {
    "DecisionApi/Api/Default/decisions/{decision_id}/GET/Resource": [
        S("AwsSolutions-COG4", "IAM (SigV4) authorization with Identity Center credentials; the handler also checks "
          "the approver permission-set role. No separate Cognito user directory.")],
    "DecisionApi/Api/Default/decisions/{decision_id}/decision/POST/Resource": [
        S("AwsSolutions-COG4", "IAM (SigV4) authorization with Identity Center credentials; the handler also checks "
          "the approver permission-set role. No separate Cognito user directory.")],
    "DecisionApi/Api/CloudWatchRole/Resource": [
        S("AwsSolutions-IAM4", "Account-level API Gateway logging role; AWS-maintained policy for log delivery.",
          [P + "service-role/AmazonAPIGatewayPushToCloudWatchLogs"])],
    "Compute/GlueRole/Resource": [
        S("AwsSolutions-IAM4", "AWS-maintained Glue service policy: job logs, metrics and the ENIs for the VPC connection.",
          [P + "service-role/AWSGlueServiceRole"])],
    "Compute/GlueRole/DefaultPolicy/Resource": [
        S("AwsSolutions-IAM5", "Glue names its Python shell log groups under /aws-glue/; AssociateKmsKey encrypts them.",
          ["Resource::arn:<AWS::Partition>:logs:<AWS::Region>:<AWS::AccountId>:log-group:/aws-glue/*",
           RegexAppliesTo(regex="/^Resource::arn:<AWS::Partition>:logs:.*:log-group:\\/aws-glue\\/\\*$/g")])],
    "Compute/BatchExecRole/DefaultPolicy/Resource": [
        S("AwsSolutions-IAM5", "ecr:GetAuthorizationToken does not support resource-level permissions.", ["Resource::*"])],
    "Compute/ProcessingRole/DefaultPolicy/Resource": [
        S("AwsSolutions-IAM5", "ecr:GetAuthorizationToken and the EC2 network-interface calls SageMaker makes for VPC "
          "jobs have no resource-level permissions; PutMetricData is limited by a namespace condition.", ["Resource::*"]),
        S("AwsSolutions-IAM5", "SageMaker writes one log stream per processing job under its fixed log group.",
          [RegexAppliesTo(regex="/^Resource::arn:<AWS::Partition>:logs:.*:log-group:\\/aws\\/sagemaker\\/ProcessingJobs:\\*$/g")])],
    "Workflow/StateMachine/Role/DefaultPolicy/Resource": [
        S("AwsSolutions-IAM5", "redshift-data:DescribeStatement and the X-Ray trace calls have no resource-level "
          "permissions.", ["Resource::*"]),
        S("AwsSolutions-IAM5", "Batch .sync integration: job-definition revisions change on every deploy.",
          [RegexAppliesTo(regex="/^Resource::arn:<AWS::Partition>:batch:.*:job-definition\\/\\*$/g")]),
        S("AwsSolutions-IAM5", "Processing jobs are named per run (adi-markov-<run>, adi-mdp-<run>-r<n>).",
          [RegexAppliesTo(regex="/^Resource::arn:<AWS::Partition>:sagemaker:.*:processing-job\\/adi-\\*$/g")])],
    "Ops/SchedulerDlq/Resource": [
        S("AwsSolutions-SQS3", "This queue is itself the dead-letter queue of the EventBridge Scheduler targets.")],
    "EndpointSg/Resource": [
        S("CdkNagValidationFailure", "Ingress source is the VPC CIDR token (Fn::GetAtt), not 0.0.0.0/0; nag cannot "
          "resolve the token.")],
    "TokenizerRole/Resource": [
        S("AwsSolutions-IAM4", "Session Manager access to the Local Zone instance (no SSH, no inbound rules); "
          "AWS-maintained SSM agent policy.", [P + "AmazonSSMManagedInstanceCore"])],
}


def suppress(stack: cdk.Stack) -> None:
    NagSuppressions.add_stack_suppressions(stack, STACK_WIDE)
    for rel, items in BY_PATH.items():
        parts = rel.split("/")
        node = stack
        for p in parts:
            node = node.node.try_find_child(p)
            if node is None:
                break
        if node is not None:
            NagSuppressions.add_resource_suppressions_by_path(stack, f"/{stack.node.path}/{rel}", items)
