"""Operations: schedules (monthly run, daily TIA review), alarms to the alert topic, cost budget."""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as targets
from aws_cdk import aws_sqs as sqs
from constructs import Construct

GE = cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD


class Operations(Construct):
    def __init__(self, scope: Construct, cid: str, *, cfg, key, state_machine, alerts, action_dlq,
                 action_fn, tia_fn, insight_fn) -> None:
        super().__init__(scope, cid)
        stack = cdk.Stack.of(self)
        self.dlq = sqs.Queue(self, "SchedulerDlq", encryption=sqs.QueueEncryption.KMS, encryption_master_key=key,
                             enforce_ssl=True, retention_period=cdk.Duration.days(14))
        role = iam.Role(self, "SchedulerRole", assumed_by=iam.ServicePrincipal(
            "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": stack.account}}))
        common = dict(dead_letter_queue=self.dlq, max_event_age=cdk.Duration.hours(24), retry_attempts=3, role=role)
        tz = cdk.TimeZone.ASIA_HO_CHI_MINH
        scheduler.Schedule(
            self, "MonthlyRun", description="Monthly ADI decision run, day 5 at 09:00 Vietnam time",
            schedule=scheduler.ScheduleExpression.cron(minute="0", hour="9", day="5", month="*", time_zone=tz),
            target=targets.StepFunctionsStartExecution(state_machine, input=scheduler.ScheduleTargetInput.from_object(
                {"scheduled_time": scheduler.ContextAttribute.scheduled_time}), **common))
        scheduler.Schedule(
            self, "DailyTiaReview", description="Daily check of TIA review dates (PDPL, 6-month review)",
            schedule=scheduler.ScheduleExpression.cron(minute="0", hour="8", time_zone=tz),
            target=targets.LambdaInvoke(tia_fn, **common))

        alarms = [
            ("WorkflowFailed", state_machine.metric_failed(), "A decision run failed closed"),
            ("WorkflowTimedOut", state_machine.metric_timed_out(), "A decision run timed out"),
            ("ActionDlq", action_dlq.metric_approximate_number_of_messages_visible(), "An action is in the dead-letter queue"),
            ("SchedulerDlq", self.dlq.metric_approximate_number_of_messages_visible(), "A schedule could not start its target"),
            ("ActionErrors", action_fn.metric_errors(), "The action handler raised an error"),
            ("InsightErrors", insight_fn.metric_errors(), "The insight agent raised an error"),
            ("TiaReviewErrors", tia_fn.metric_errors(), "The TIA review job raised an error"),
        ]
        for name, metric, text in alarms:
            alarm = cw.Alarm(self, f"{name}Alarm", metric=metric.with_(period=cdk.Duration.minutes(5), statistic="Maximum"),
                             threshold=1, evaluation_periods=1, comparison_operator=GE, alarm_description=text,
                             treat_missing_data=cw.TreatMissingData.NOT_BREACHING)
            alarm.add_alarm_action(cw_actions.SnsAction(alerts))
        self._budget(cfg, alerts, stack)

    def _budget(self, cfg, alerts, stack: cdk.Stack) -> None:
        """Account-level monthly cost budget (dev accounts are dedicated to ADI). For a shared account,
        activate the 'project' cost-allocation tag and add a TagKeyValue cost filter."""
        alerts.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal("budgets.amazonaws.com")], actions=["SNS:Publish"],
            resources=[alerts.topic_arn], conditions={"StringEquals": {"aws:SourceAccount": stack.account}}))
        B = budgets.CfnBudget

        def note(kind: str, threshold: float):
            return B.NotificationWithSubscribersProperty(
                notification=B.NotificationProperty(comparison_operator="GREATER_THAN", notification_type=kind,
                                                    threshold=threshold, threshold_type="PERCENTAGE"),
                subscribers=[B.SubscriberProperty(subscription_type="SNS", address=alerts.topic_arn)])

        B(self, "MonthlyBudget", budget=B.BudgetDataProperty(
            budget_name=f"{cfg.name}-monthly", budget_type="COST", time_unit="MONTHLY",
            budget_limit=B.SpendProperty(amount=cfg.monthly_budget_usd, unit="USD")),
          notifications_with_subscribers=[note("ACTUAL", 80), note("FORECASTED", 100)])
