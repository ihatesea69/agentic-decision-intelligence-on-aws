"""Daily TIA register check. The TIA register holds dossier status only,
no personal data. Overdue reviews close the cross-border gate; reviews due within 30 days notify the DPO."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from adi_engines.residency import REVIEW_PERIOD

from . import aws

REMIND = timedelta(days=30)


def classify(tia: dict, now: datetime) -> str:
    last = datetime.fromisoformat(tia.get("last_reviewed_at") or tia["prepared_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    due = last + REVIEW_PERIOD
    if now > due:
        return "OVERDUE"
    if now > due - REMIND:
        return "DUE_SOON"
    return "OK"


def handler(event=None, _context=None, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    t = aws.table(aws.env("TIA_TABLE"))
    items, kwargs = [], {}
    while True:
        page = t.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    counts = {"OK": 0, "DUE_SOON": 0, "OVERDUE": 0}
    for tia in items:
        state = classify(tia, now)
        counts[state] += 1
        if state == "OVERDUE" and tia.get("gate_open", True):
            t.update_item(Key={"tia_id": tia["tia_id"]}, UpdateExpression="SET gate_open = :f",
                          ExpressionAttributeValues={":f": False})
        if state != "OK":
            aws.client("sns").publish(TopicArn=aws.env("DPO_TOPIC_ARN"),
                                      Subject=f"TIA {tia['tia_id']} review {state}",
                                      Message=f"TIA {tia['tia_id']} review status {state}. Overdue reviews close the transfer gate.")
    return counts
