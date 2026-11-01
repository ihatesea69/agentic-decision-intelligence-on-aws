"""Step Functions Standard workflow: the seven decision steps.

0 run context -> 1 Glue DQ gate -> mart load -> 2 Markov -> 3 MDP -> 4 Monte Carlo -> package
-> 5 insight -> 6 approval (.waitForTaskToken) -> 7 emit actions. Every gate fails closed: the run
stops, keeps its evidence under the run prefix, and alerts the owner. Revise loops back to step 3.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

J = sfn.JsonPath
COLS = ("snapshot_month, account_id, from_state, to_state, mob, product, segment, age_band, province, "
        "opening, new_disbursement, prepayment, write_off, closing, recorded_payment")
MART_DDL = [
    "CREATE SCHEMA IF NOT EXISTS adi",
    "CREATE TABLE IF NOT EXISTS adi.snapshot (snapshot_month CHAR(7) NOT NULL, account_id VARCHAR(80) NOT NULL, "
    "from_state VARCHAR(4) NOT NULL, to_state VARCHAR(4) NOT NULL, mob INTEGER, product VARCHAR(64), "
    "segment VARCHAR(64), age_band VARCHAR(16), province VARCHAR(64), opening DECIMAL(18,6), "
    "new_disbursement DECIMAL(18,6), prepayment DECIMAL(18,6), write_off DECIMAL(18,6), closing DECIMAL(18,6), "
    "recorded_payment DECIMAL(18,6), loaded_at TIMESTAMP DEFAULT GETDATE())",
]


class Workflow(Construct):
    def __init__(self, scope: Construct, cid: str, *, cfg, key, data, compute, fns: dict, alerts,
                 subnet_ids: list, sg_id: str) -> None:
        super().__init__(scope, cid)
        self.cfg, self.compute, self.data, self.fns = cfg, compute, data, fns
        self.subnet_ids, self.sg_id, self.key = subnet_ids, sg_id, key
        stack = cdk.Stack.of(self)
        self.part, self.region, self.account = stack.partition, stack.region, stack.account

        self.alert = tasks.SnsPublish(
            self, "AlertFailClosed", topic=alerts, subject="ADI run failed closed", result_path=J.DISCARD,
            message=sfn.TaskInput.from_text(J.format(
                "ADI run {} failed closed at step {} ({}). Evidence is kept under the run prefix; no production action ran.",
                J.string_at("$$.Execution.Name"), J.string_at("$.failed_step"), J.string_at("$.error.Error"))))
        self.alert.next(sfn.Fail(self, "FailClosed", error="ADI.FailClosed",
                                 cause="A gate failed; the run stopped and no production action was taken"))

        definition = self._definition()
        log_group = logs.LogGroup(self, "Logs", encryption_key=key, retention=logs.RetentionDays.ONE_YEAR,
                                  log_group_name=f"/aws/vendedlogs/states/{cfg.name}-decision",
                                  removal_policy=cdk.RemovalPolicy.RETAIN)
        # Execution data stays out of the logs: the approval step carries the task token.
        self.state_machine = sfn.StateMachine(
            self, "StateMachine", state_machine_name=f"{cfg.name}-decision",
            state_machine_type=sfn.StateMachineType.STANDARD, definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=cdk.Duration.days(35), tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=False))
        self._grants()

    # ---- helpers -------------------------------------------------------------------------------
    def _guard(self, state, step: str):
        mark = sfn.Pass(self, f"Mark{step.title().replace('-', '')}", result=sfn.Result.from_string(step),
                        result_path="$.failed_step")
        mark.next(self.alert)
        state.add_catch(mark, errors=["States.ALL"], result_path="$.error")
        return state

    def _processing(self, cid: str, task: str, name_fmt: str, with_round: bool):
        env = {"ADI_RUN.$": "$.ctx.run_prefix", "ADI_CONFIG.$": "$.ctx.config_prefix"}
        name = f"States.Format('{name_fmt}', $.ctx.decision_id)"
        if with_round:
            env["ADI_OUT.$"] = "$.paths.round_prefix"
            name = f"States.Format('{name_fmt}', $.ctx.decision_id, $.round.n)"
        return sfn.CustomState(self, cid, state_json={
            "Type": "Task",
            "Resource": f"arn:{self.part}:states:::sagemaker:createProcessingJob.sync",
            "Parameters": {
                "ProcessingJobName.$": name,
                "RoleArn": self.compute.processing_role.role_arn,
                "AppSpecification": {"ImageUri": self.compute.image.image_uri,
                                     "ContainerEntrypoint": ["python", "-m", "adi_jobs", task]},
                "Environment": env,
                "ProcessingResources": {"ClusterConfig": {"InstanceCount": 1, "InstanceType": "ml.m5.large",
                                                          "VolumeSizeInGB": 30, "VolumeKmsKeyId": self.key.key_arn}},
                "NetworkConfig": {"EnableInterContainerTrafficEncryption": True, "EnableNetworkIsolation": False,
                                  "VpcConfig": {"SecurityGroupIds": [self.sg_id], "Subnets": self.subnet_ids}},
                "StoppingCondition": {"MaxRuntimeInSeconds": 3600},
            },
            "ResultPath": None,
        })

    def _batch(self, cid: str, task: str, array: bool):
        env = {"ADI_RUN": J.string_at("$.ctx.run_prefix"), "ADI_CONFIG": J.string_at("$.ctx.config_prefix"),
               "ADI_OUT": J.string_at("$.paths.round_prefix"), "MC_CHUNKS": str(self.cfg.mc_chunks)}
        size = self.cfg.mc_chunks if array and self.cfg.mc_chunks > 1 else None
        return tasks.BatchSubmitJob(
            self, cid, job_name=J.format("adi-" + task + "-{}-r{}", J.string_at("$.ctx.decision_id"), J.string_at("$.round.n")),
            job_queue_arn=self.compute.queue.job_queue_arn, job_definition_arn=self.compute.job_definition.job_definition_arn,
            array_size=size, container_overrides=tasks.BatchContainerOverrides(command=[task], environment=env),
            result_path=J.DISCARD, task_timeout=sfn.Timeout.duration(cdk.Duration.hours(3)))

    def _round_prefix(self, name: str) -> str:
        return J.format("{}/" + name, J.string_at("$.paths.round_prefix"))

    def _mart(self, next_state):
        """Load the validated snapshot into the Redshift portfolio mart (idempotent per month)."""
        # JsonPath values cannot sit inside a plain array, so the statement list is a States.Array.
        # snapshot_month and run_prefix are validated by step 0 (YYYY-MM, [A-Za-z0-9-]), so the
        # formatted SQL cannot carry injected text.
        sqls = J.array(*MART_DDL,
                       J.format("DELETE FROM adi.snapshot WHERE snapshot_month = '{}'", J.string_at("$.ctx.snapshot_month")),
                       J.format("COPY adi.snapshot (" + COLS + ") FROM '{}/snapshot.jsonl' IAM_ROLE default "
                                "FORMAT AS JSON 'auto ignorecase'", J.string_at("$.ctx.run_prefix")))
        start = tasks.CallAwsService(
            self, "LoadMart", service="redshiftdata", action="batchExecuteStatement",
            iam_action="redshift-data:BatchExecuteStatement", iam_resources=[self.data.workgroup_arn],
            additional_iam_statements=[iam.PolicyStatement(actions=["redshift-serverless:GetCredentials"],
                                                           resources=[self.data.workgroup_arn])],
            parameters={"WorkgroupName": self.data.workgroup_name, "Database": "adi", "Sqls": sqls,
                        "StatementName": J.format("adi-mart-{}", J.string_at("$.ctx.decision_id"))},
            result_selector={"id": J.string_at("$.Id")}, result_path="$.mart")
        wait = sfn.Wait(self, "WaitMart", time=sfn.WaitTime.duration(cdk.Duration.seconds(10)))
        check = tasks.CallAwsService(
            self, "CheckMart", service="redshiftdata", action="describeStatement",
            iam_action="redshift-data:DescribeStatement", iam_resources=["*"],
            parameters={"Id": J.string_at("$.mart.id")},
            result_selector={"id": J.string_at("$.Id"), "status": J.string_at("$.Status")}, result_path="$.mart")
        mark = sfn.Pass(self, "MarkMart", result=sfn.Result.from_string("mart-load"), result_path="$.failed_step")
        mark.next(self.alert)
        failed = sfn.Pass(self, "MartFailed", result=sfn.Result.from_object({"Error": "ADI.MartLoadFailed"}),
                          result_path="$.error")
        failed.next(mark)
        for s in (start, check):
            s.add_catch(mark, errors=["States.ALL"], result_path="$.error")
        done = sfn.Choice(self, "MartLoaded?")
        done.when(sfn.Condition.string_equals("$.mart.status", "FINISHED"), next_state)
        done.when(sfn.Condition.or_(sfn.Condition.string_equals("$.mart.status", "FAILED"),
                                    sfn.Condition.string_equals("$.mart.status", "ABORTED")), failed)
        done.otherwise(wait)
        start.next(wait)
        wait.next(check)
        check.next(done)
        return start

    def _grants(self) -> None:
        sm = self.state_machine
        # SNS publish to the encrypted alert topic; KMS grant for the processing volume key.
        self.key.grant(sm.role, "kms:GenerateDataKey*", "kms:Decrypt", "kms:CreateGrant", "kms:DescribeKey")
        self.key.grant(self.compute.processing_role, "kms:CreateGrant", "kms:DescribeKey", "kms:Encrypt",
                       "kms:Decrypt", "kms:GenerateDataKey*")
        base = f"arn:{self.part}:sagemaker:{self.region}:{self.account}"
        sm.add_to_role_policy(iam.PolicyStatement(
            sid="ProcessingJobs", resources=[f"{base}:processing-job/adi-*"],
            actions=["sagemaker:CreateProcessingJob", "sagemaker:DescribeProcessingJob",
                     "sagemaker:StopProcessingJob", "sagemaker:AddTags"]))
        sm.add_to_role_policy(iam.PolicyStatement(
            sid="PassProcessingRole", actions=["iam:PassRole"], resources=[self.compute.processing_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "sagemaker.amazonaws.com"}}))
        sm.add_to_role_policy(iam.PolicyStatement(
            sid="ProcessingSyncRule", actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
            resources=[f"arn:{self.part}:events:{self.region}:{self.account}:rule/"
                       "StepFunctionsGetEventsForSageMakerProcessingJobsRule"]))

    def _definition(self):
        f, cfg = self.fns, self.cfg
        run_ctx = self._guard(tasks.LambdaInvoke(
            self, "Step0RunContext", lambda_function=f["run"], payload_response_only=True, result_path="$.ctx",
            payload=sfn.TaskInput.from_object({"execution_name": J.string_at("$$.Execution.Name"),
                                               "start_time": J.string_at("$$.Execution.StartTime"),
                                               "input": J.object_at("$$.Execution.Input")})), "run-context")
        init_round = sfn.Pass(self, "InitRound", parameters={"n": J.number_at("$.ctx.round")}, result_path="$.round")
        dq = self._guard(tasks.GlueStartJobRun(
            self, "Step1DataQuality", glue_job_name=self.compute.glue_job_name,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB, result_path=J.DISCARD,
            task_timeout=sfn.Timeout.duration(cdk.Duration.minutes(45)),
            arguments=sfn.TaskInput.from_object({"--source_prefix": J.string_at("$.ctx.source_prefix"),
                                                 "--run_prefix": J.string_at("$.ctx.run_prefix"),
                                                 "--snapshot_month": J.string_at("$.ctx.snapshot_month")})), "data-quality")
        markov = self._guard(self._processing("Step2Markov", "markov", "adi-markov-{}", False), "markov")
        paths = sfn.Pass(self, "RoundPaths", result_path="$.paths", parameters={
            "round_prefix": J.format("{}/r{}", J.string_at("$.ctx.run_prefix"), J.string_at("$.round.n"))})
        mdp = self._guard(self._processing("Step3Mdp", "mdp", "adi-mdp-{}-r{}", True), "mdp")
        mc = self._guard(self._batch("Step4MonteCarlo", "mc-chunk", True), "monte-carlo")
        package = self._guard(self._batch("EvidencePackage", "package", False), "package")

        # Step 5: an agent failure is not a run failure. The package still goes to the approver,
        # with production actions disabled.
        insight = tasks.LambdaInvoke(
            self, "Step5Insight", lambda_function=f["insight"], payload_response_only=True, result_path="$.insight",
            payload=sfn.TaskInput.from_object({"package_uri": self._round_prefix("package.json"),
                                               "evidence_prefix": self._round_prefix("evidence")}))
        agent_failed = sfn.Pass(self, "AgentCheckFailed", result_path="$.insight",
                                result=sfn.Result.from_object({"agent_status": "FAILED", "insight_uri": None}))
        insight.add_catch(agent_failed, errors=["States.ALL"], result_path="$.agent_error")

        approval = tasks.LambdaInvoke(
            self, "Step6Approval", lambda_function=f["publish"], integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            result_path="$.decision", task_timeout=sfn.Timeout.duration(cdk.Duration.hours(cfg.approval_hours)),
            payload=sfn.TaskInput.from_object({
                "decision_id": J.string_at("$.ctx.decision_id"), "round": J.number_at("$.round.n"),
                "task_token": J.task_token, "package_uri": self._round_prefix("package.json"),
                "insight_uri": J.string_at("$.insight.insight_uri"), "agent_status": J.string_at("$.insight.agent_status"),
                "approval_hours": cfg.approval_hours}))
        expire = tasks.LambdaInvoke(self, "ExpireDecision", lambda_function=f["expire"], payload_response_only=True,
                                    result_path="$.expired",
                                    payload=sfn.TaskInput.from_object({"decision_id": J.string_at("$.ctx.decision_id")}))
        approval.add_catch(expire, errors=["States.Timeout"], result_path="$.error")
        self._guard(approval, "approval")
        expire.next(sfn.Succeed(self, "ExpiredNoAction", comment="Approval window closed; no production action ran"))

        emit = self._guard(tasks.LambdaInvoke(
            self, "Step7EmitActions", lambda_function=f["emit"], payload_response_only=True, result_path="$.emitted",
            payload=sfn.TaskInput.from_object({"decision_id": J.string_at("$.ctx.decision_id"),
                                               "package_uri": self._round_prefix("package.json"),
                                               "decision": J.object_at("$.decision")})), "actions")
        emit.next(sfn.Succeed(self, "ActionsEmitted"))

        def stop(cid: str, error: str, step: str):
            first = sfn.Pass(self, cid, result=sfn.Result.from_object({"Error": error}), result_path="$.error")
            first.next(sfn.Pass(self, f"Mark{cid}", result=sfn.Result.from_string(step),
                                result_path="$.failed_step")).next(self.alert)
            return first

        bump = sfn.Pass(self, "NextRound", parameters={"n": J.math_add(J.number_at("$.round.n"), 1)},
                        result_path="$.round")
        bump.next(paths)
        revise = sfn.Choice(self, "RevisionsLeft?")
        revise.when(sfn.Condition.number_greater_than_equals("$.round.n", cfg.max_revisions + 1),
                    stop("RevisionLimit", "ADI.RevisionLimit", "revision-limit"))
        revise.otherwise(bump)
        decision = sfn.Choice(self, "Decision?")
        decision.when(sfn.Condition.string_equals("$.decision.decision", "approve"), emit)
        decision.when(sfn.Condition.string_equals("$.decision.decision", "revise"), revise)
        decision.when(sfn.Condition.string_equals("$.decision.decision", "reject"),
                      sfn.Succeed(self, "RejectedNoAction", comment="Rejected; the outcome is in the ledger"))
        decision.otherwise(stop("UnknownDecision", "ADI.UnknownDecision", "approval-output"))

        markov.next(paths)
        paths.next(mdp)
        mdp.next(mc)
        mc.next(package)
        package.next(insight)
        insight.next(approval)
        agent_failed.next(approval)
        approval.next(decision)
        mart = self._mart(markov)
        run_ctx.next(init_round).next(dq).next(mart)
        return run_ctx
