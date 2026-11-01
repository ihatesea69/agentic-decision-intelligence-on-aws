"""Decision API (step 6): API Gateway REST, IAM (SigV4) auth, request validation, WAF, access logs.

GET  /decisions/{decision_id}           ledger status (never the task token)
POST /decisions/{decision_id}/decision  approve | revise | reject, with rationale and effective period
Callers sign with Identity Center credentials; the Lambda checks the approver permission-set role.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_wafv2 as waf
from constructs import Construct

T = apigw.JsonSchemaType


class DecisionApi(Construct):
    def __init__(self, scope: Construct, cid: str, *, cfg, key, decide_fn, get_fn) -> None:
        super().__init__(scope, cid)
        stack = cdk.Stack.of(self)
        access = logs.LogGroup(self, "AccessLogs", encryption_key=key, retention=logs.RetentionDays.ONE_YEAR,
                               removal_policy=cdk.RemovalPolicy.RETAIN)
        self.api = apigw.RestApi(
            self, "Api", rest_api_name=f"{cfg.name}-decisions", description="ADI decision API (human approval)",
            endpoint_types=[apigw.EndpointType.REGIONAL], cloud_watch_role=True,
            default_method_options=apigw.MethodOptions(authorization_type=apigw.AuthorizationType.IAM),
            policy=iam.PolicyDocument(statements=[iam.PolicyStatement(
                principals=[iam.AccountPrincipal(stack.account)], actions=["execute-api:Invoke"],
                resources=["execute-api:/*"])]),
            deploy_options=apigw.StageOptions(
                stage_name="v1", access_log_destination=apigw.LogGroupLogDestination(access),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=True, http_method=True, ip=True, protocol=True, request_time=True,
                    resource_path=True, response_length=True, status=True, user=True),
                logging_level=apigw.MethodLoggingLevel.INFO, data_trace_enabled=False, tracing_enabled=True,
                metrics_enabled=True, throttling_rate_limit=10, throttling_burst_limit=20))
        body = self.api.add_model("DecisionBody", content_type="application/json", model_name="DecisionBody",
                                  schema=apigw.JsonSchema(
                                      schema=apigw.JsonSchemaVersion.DRAFT4, type=T.OBJECT, additional_properties=False,
                                      required=["decision", "rationale"], properties={
                                          "decision": apigw.JsonSchema(type=T.STRING, enum=["approve", "revise", "reject"]),
                                          "rationale": apigw.JsonSchema(type=T.STRING, min_length=10, max_length=2000),
                                          "effective_period": apigw.JsonSchema(type=T.STRING,
                                                                               pattern="^[0-9]{4}-(0[1-9]|1[0-2])$")}))
        validator = self.api.add_request_validator("Validator", validate_request_body=True,
                                                   validate_request_parameters=True)
        params = {"method.request.path.decision_id": True}
        one = self.api.root.add_resource("decisions").add_resource("{decision_id}")
        one.add_method("GET", apigw.LambdaIntegration(get_fn), request_validator=validator, request_parameters=params)
        one.add_resource("decision").add_method("POST", apigw.LambdaIntegration(decide_fn), request_validator=validator,
                                                request_parameters=params, request_models={"application/json": body})
        self._waf(cfg)

    def _waf(self, cfg) -> None:
        W = waf.CfnWebACL

        def vis(name: str):
            return W.VisibilityConfigProperty(cloud_watch_metrics_enabled=True, metric_name=name,
                                              sampled_requests_enabled=True)

        acl = W(self, "Acl", scope="REGIONAL", default_action=W.DefaultActionProperty(allow={}),
                visibility_config=vis(f"{cfg.name}-decision-api"), rules=[
                    W.RuleProperty(name="AwsCommonRules", priority=0, override_action=W.OverrideActionProperty(none={}),
                                   statement=W.StatementProperty(managed_rule_group_statement=W.ManagedRuleGroupStatementProperty(
                                       vendor_name="AWS", name="AWSManagedRulesCommonRuleSet")),
                                   visibility_config=vis("aws-common")),
                    W.RuleProperty(name="RatePerIp", priority=1, action=W.RuleActionProperty(block={}),
                                   statement=W.StatementProperty(rate_based_statement=W.RateBasedStatementProperty(
                                       limit=300, aggregate_key_type="IP")),
                                   visibility_config=vis("rate-per-ip")),
                ])
        waf.CfnWebACLAssociation(self, "AclAssociation", resource_arn=self.api.deployment_stage.stage_arn,
                                 web_acl_arn=acl.attr_arn)
