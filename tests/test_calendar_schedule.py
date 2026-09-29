"""Offline tests for atomic calendar claims, authorization and MCP/CFN contracts."""
import copy
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.exceptions import ClientError
from test_google_sso import template

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "lma-ai-stack/source/lambda_functions/mcp_analytics"
START = "2099-01-01T10:00:00Z"
NEXT = "2099-01-08T10:00:00Z"


class AtomicStore:
    """Small transactional fake: validate ALL conditions before changing any row."""
    def __init__(self, module):
        self.module = module
        self.tables = {"vps": {}, "claims": {}}
        self.lock = threading.RLock()
        self.race = None
        self.writes = 0
        self.transactions = []

    def get_item(self, TableName, Key, **kwargs):
        assert kwargs["ConsistentRead"] is True
        key = next(iter(Key.values()))["S"]
        with self.lock:
            row = copy.deepcopy(self.tables[TableName].get(key))
        return {"Item": self.module._encode(row)} if row else {}

    def transact_write_items(self, TransactItems):
        self.transactions.append(copy.deepcopy(TransactItems))
        if self.race:
            barrier = self.race
            barrier.wait(timeout=5)
            self.race = None
        with self.lock:
            pending = []
            for operation in TransactItems:
                put = operation.get("Put", operation.get("Update"))
                if "Put" in operation:
                    item = {k: self.module.DESERIALIZER.deserialize(v) for k, v in put["Item"].items()}
                    key = item.get("id", item.get("key"))
                else:
                    key = put["Key"]["id"]["S"]
                    item = copy.deepcopy(self.tables[put["TableName"]].get(key, {}))
                existing = self.tables[put["TableName"]].get(key)
                values = {k: self.module.DESERIALIZER.deserialize(v)
                          for k, v in put.get("ExpressionAttributeValues", {}).items()}
                if "Update" in operation:
                    for alias, field in put["ExpressionAttributeNames"].items():
                        if alias.startswith("#field"):
                            item[field] = values[":value" + alias.removeprefix("#field")]
                condition = put["ConditionExpression"]
                if condition == "attribute_not_exists(id)":
                    valid = existing is None
                elif "scheduleRevision" in condition:
                    valid = (existing is not None and existing["status"] == values[":pending"]
                             and existing["scheduleRevision"] == values[":revision"])
                else:
                    valid = existing is None or existing["virtualParticipantId"] == values[":vp"]
                if not valid:
                    raise ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
                pending.append((put["TableName"], key, item))
            for table, key, item in pending:
                self.tables[table][key] = item
            self.writes += 1


class CalendarSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"VP_TABLE_NAME": "vps", "CALENDAR_SCHEDULE_TABLE_NAME": "claims"})
        self.env.start()
        self.addCleanup(self.env.stop)
        package = types.ModuleType("tools")
        package.__path__ = [str(SOURCE / "tools")]
        url_helper = types.ModuleType("tools.url_helper")
        url_helper.get_virtual_participant_url = lambda vp: "https://lma.test/vp/" + vp
        self.modules = patch.dict(sys.modules, {"tools": package, "tools.url_helper": url_helper})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("tools.calendar_schedule", SOURCE / "tools/calendar_schedule.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.module
        spec.loader.exec_module(self.module)
        self.store = AtomicStore(self.module)
        self.module._client = lambda: self.store
        self.notify = self.module._notify
        self.module._notify = Mock(return_value=True)

    def create(self, **kwargs):
        fields = dict(meeting_name="Weekly sync", meeting_platform="Google Meet",
                      meeting_id="abc-defg-hij", scheduled_time=START, user_id="alice")
        fields.update(kwargs)
        return self.module.create(**fields)

    def manage(self, action, vp, **kwargs):
        return self.module.manage(action, user_id="alice", virtual_participant_id=vp, **kwargs)

    def test_retry_and_other_attendee_share_one_vp(self):
        first = self.create()
        second = self.create(user_id="bob", meeting_platform="GOOGLE_MEET",
                             meeting_id="https://meet.google.com/abc-defg-hij?authuser=1",
                             scheduled_time="2099-01-01T11:00:00+01:00")
        self.assertEqual(first["virtualParticipantId"], second["virtualParticipantId"])
        self.assertTrue(second["alreadyScheduled"])
        self.assertEqual(second["owner"], "alice")
        self.assertEqual(len(self.store.tables["vps"]), 1)

    def test_simultaneous_first_claims_are_atomic(self):
        self.store.race = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda user: self.create(user_id=user), ["alice", "bob"]))
        self.assertEqual(results[0]["virtualParticipantId"], results[1]["virtualParticipantId"])
        self.assertEqual(len(self.store.tables["vps"]), 1)

    def test_series_occurrences_get_separate_vps(self):
        first = self.create(calendar_event_uid="series", calendar_occurrence_start=START)
        second = self.create(calendar_event_uid="series", calendar_occurrence_start=NEXT, scheduled_time=NEXT)
        self.assertNotEqual(first["virtualParticipantId"], second["virtualParticipantId"])

    def test_one_off_uid_survives_rescheduling_without_saved_start(self):
        first = self.create(calendar_event_uid="one-off")
        updated = self.module.manage("UPDATE", "alice", calendar_event_uid="one-off", scheduled_time=NEXT)
        self.assertEqual(updated["virtualParticipantId"], first["virtualParticipantId"])
        self.assertEqual(self.create(calendar_event_uid="one-off", scheduled_time=NEXT)["virtualParticipantId"],
                         first["virtualParticipantId"])

    def test_reschedule_keeps_identity_and_old_slot_tombstone(self):
        first = self.create(calendar_event_uid="uid", calendar_occurrence_start=START)
        vp = first["virtualParticipantId"]
        updated = self.module.manage("UPDATE", "alice", calendar_event_uid="uid",
            calendar_occurrence_start=START, scheduled_time=NEXT, meeting_name="Moved",
            meeting_id="xyz-abcd-efg")
        self.assertEqual(updated["virtualParticipantId"], vp)
        for kwargs in ({}, {"scheduled_time": NEXT, "meeting_id": "xyz-abcd-efg"},
                       {"calendar_event_uid": "uid", "calendar_occurrence_start": START}):
            retry = self.create(**kwargs)
            self.assertEqual(retry["virtualParticipantId"], vp)
            self.assertEqual(retry["meetingName"], "Moved")
        self.assertEqual(len(self.store.tables["vps"]), 1)

    def test_different_calendar_ids_bind_to_same_slot(self):
        first = self.create(calendar_event_uid="alice-uid", calendar_occurrence_start=START)
        second = self.create(user_id="bob", calendar_event_uid="bob-uid", calendar_occurrence_start=START)
        self.assertEqual(first["virtualParticipantId"], second["virtualParticipantId"])
        updated = self.module.manage("UPDATE", "admin", is_admin=True, calendar_event_uid="bob-uid",
                                     calendar_occurrence_start=START, scheduled_time=NEXT)
        self.assertEqual(updated["virtualParticipantId"], first["virtualParticipantId"])

    def test_cancel_retry_and_stale_schedule_never_resurrect(self):
        first = self.create()
        vp = first["virtualParticipantId"]
        self.assertEqual(self.manage("CANCEL", vp)["status"], "CANCELLED")
        self.assertEqual(self.manage("CANCEL", vp)["status"], "CANCELLED")
        self.assertEqual(self.create(user_id="bob")["status"], "CANCELLED")
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.manage("UPDATE", vp, scheduled_time=NEXT)

    def test_other_attendee_cannot_edit_owner_vp(self):
        vp = self.create()["virtualParticipantId"]
        for action in ("UPDATE", "CANCEL"):
            with self.assertRaisesRegex(ValueError, "owner or an admin"):
                self.module.manage(action, "bob", virtual_participant_id=vp)
        self.module.manage("CANCEL", "admin", is_admin=True, virtual_participant_id=vp)

    def test_update_cannot_take_another_vps_slot(self):
        first = self.create()["virtualParticipantId"]
        self.create(scheduled_time=NEXT)
        with self.assertRaisesRegex(ValueError, "another VP owns"):
            self.manage("UPDATE", first, scheduled_time=NEXT)
        self.assertEqual(self.store.tables["vps"][first]["meetingTime"], self.module.timestamp(START))

    def test_concurrent_edit_is_not_overwritten(self):
        vp = self.create()["virtualParticipantId"]
        original = self.store.transact_write_items
        def race(**kwargs):
            self.store.tables["vps"][vp]["scheduleRevision"] += 1
            original(**kwargs)
        self.store.transact_write_items = race
        with self.assertRaisesRegex(ValueError, "concurrently"):
            self.manage("UPDATE", vp, scheduled_time=NEXT)

    def test_calendar_edit_preserves_concurrent_sharing_changes(self):
        vp = self.create()["virtualParticipantId"]
        original = self.store.transact_write_items
        def share_concurrently(**kwargs):
            self.store.tables["vps"][vp]["SharedWith"] = "bob"
            original(**kwargs)
        self.store.transact_write_items = share_concurrently
        self.manage("UPDATE", vp, scheduled_time=NEXT)
        self.assertEqual(self.store.tables["vps"][vp]["SharedWith"], "bob")

    def test_running_or_prejoin_vp_cannot_be_changed(self):
        from datetime import datetime, timezone
        vp = self.create()["virtualParticipantId"]
        self.store.tables["vps"][vp]["status"] = "ACTIVE"
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.manage("CANCEL", vp)
        self.store.tables["vps"][vp]["status"] = "SCHEDULED"
        self.store.tables["vps"][vp]["meetingTime"] = int(datetime.now(timezone.utc).timestamp()) + 150
        with self.assertRaisesRegex(ValueError, "3 minutes"):
            self.manage("CANCEL", vp)

    def test_deleted_or_legacy_vp_not_recreated_or_migrated(self):
        vp = self.create()["virtualParticipantId"]
        self.store.tables["vps"][vp]["calendarManaged"] = False
        with self.assertRaisesRegex(ValueError, "calendar-aware"):
            self.manage("CANCEL", vp)
        del self.store.tables["vps"][vp]
        with self.assertRaisesRegex(ValueError, "deleted"):
            self.create()

    def test_invalid_identity_and_time_fail_before_write(self):
        for kwargs in ({"calendar_event_uid": " "}, {"calendar_occurrence_start": START},
                       {"scheduled_time": "2099-01-01T10:00:00"}, {"scheduled_time": "2000-01-01T10:00:00Z"},
                       {"meeting_id": "https://evil.test/abc-defg-hij"}, {"meeting_name": " "}):
            with self.assertRaises(ValueError):
                self.create(**kwargs)
        self.assertEqual(self.store.writes, 0)

    def test_notification_failure_does_not_rollback_or_duplicate(self):
        self.module._notify.return_value = False
        first = self.create()
        self.assertTrue(first["notificationPending"])
        retry = self.create()
        self.assertEqual(first["virtualParticipantId"], retry["virtualParticipantId"])
        self.assertEqual(len(self.store.tables["vps"]), 1)

    def test_notification_is_signed_readback_of_committed_state(self):
        from botocore.credentials import Credentials
        response = Mock()
        response.json.return_value = {"data": {"notifyScheduledVirtualParticipant": {"id": "vp"}}}
        session = Mock(region_name="us-east-1")
        session.get_credentials.return_value = Credentials("test-access", "test-secret")
        with (
            patch.dict(os.environ, {"APPSYNC_GRAPHQL_URL": "https://api.test/graphql"}),
            patch.object(boto3, "Session", return_value=session),
            patch("requests.post", return_value=response) as post,
        ):
            self.assertTrue(self.notify("vp"))
        self.assertEqual(json.loads(post.call_args.kwargs["data"])["variables"], {"id": "vp"})
        self.assertIn("Authorization", post.call_args.kwargs["headers"])

    def test_transactions_validate_against_real_sdk_shapes(self):
        from botocore.validate import validate_parameters
        client = boto3.client("dynamodb", region_name="us-east-1",
                              aws_access_key_id="test", aws_secret_access_key="test")
        shape = client.meta.service_model.operation_model("TransactWriteItems").input_shape
        vp = self.create(calendar_event_uid="one-off")["virtualParticipantId"]
        self.manage("UPDATE", vp, scheduled_time=NEXT)
        self.manage("CANCEL", vp)
        for transaction in self.store.transactions:
            validate_parameters({"TransactItems": transaction}, shape)


class CalendarWiringTests(unittest.TestCase):
    def test_claims_table_and_least_privilege_wiring(self):
        ai = template("lma-ai-stack/deployment/lma-ai-stack.yaml")
        resource = ai["Resources"]["CalendarScheduleClaimsTable"]
        self.assertEqual(resource["Condition"], "ShouldEnableMCPServer")
        self.assertEqual(resource["DeletionPolicy"], "Retain")
        self.assertEqual(resource["UpdateReplacePolicy"], "Retain")
        self.assertTrue(resource["Properties"]["SSESpecification"]["SSEEnabled"])
        mcp = ai["Resources"]["MCPServerAnalyticsFunction"]["Properties"]
        self.assertEqual(mcp["Environment"]["Variables"]["VP_TABLE_NAME"], {"Ref": "VirtualParticipantTable"})
        self.assertEqual(mcp["Environment"]["Variables"]["CALENDAR_SCHEDULE_TABLE_NAME"], {"Ref": "CalendarScheduleClaimsTable"})
        vp = template("lma-virtual-participant-stack/template.yaml")
        role = vp["Resources"]["VirtualParticipantSchedulerRole"]["Properties"]
        actions = [a for p in role["Policies"] for s in p["PolicyDocument"]["Statement"]
                   for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        self.assertIn("scheduler:UpdateSchedule", actions)
        self.assertIn("dynamodb:GetItem", actions)

    def test_both_mcp_transports_route_calendar_tools_and_expose_same_schema(self):
        package = types.ModuleType("tools")
        names = ("calendar_schedule", "get_summary", "get_transcript", "get_virtual_participant_status",
                 "list_meetings", "schedule_meeting", "search_meetings", "start_meeting_now")
        for name in names:
            setattr(package, name, Mock())
        with patch.dict(sys.modules, {"tools": package}):
            spec = importlib.util.spec_from_file_location("calendar_mcp", SOURCE / "index.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        python_tools = {t["name"]: t for t in module.MCP_TOOLS}
        ai = template("lma-ai-stack/deployment/lma-ai-stack.yaml")
        cfn_tools = {t["Name"]: t for t in ai["Resources"]["MCPServerGatewayTarget"]["Properties"][
            "TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]}
        for tool, action in (("update_scheduled_meeting", "UPDATE"), ("cancel_scheduled_meeting", "CANCEL")):
            self.assertEqual(set(python_tools[tool]["inputSchema"]["properties"]),
                             set(cfn_tools[tool]["InputSchema"]["Properties"]))
            package.calendar_schedule.manage.return_value = {"status": "SCHEDULED"}
            args = {"scheduleAction": action, "virtualParticipantId": "vp"}
            module.execute_tool_call(1, tool, args, "alice", "Alice", False)
            self.assertEqual(package.calendar_schedule.manage.call_args.args, (action,))
            package.calendar_schedule.manage.reset_mock()
            module.lambda_handler({**args, "requestContext": {"authorizer": {"claims": {"sub": "alice"}}}}, None)
            self.assertEqual(package.calendar_schedule.manage.call_args.args, (action,))
            self.assertFalse(package.calendar_schedule.manage.call_args.kwargs["is_admin"])
        for key in ("calendarEventUid", "calendarOccurrenceStart"):
            self.assertIn(key, python_tools["schedule_meeting"]["inputSchema"]["properties"])
            self.assertIn(key, cfn_tools["schedule_meeting"]["InputSchema"]["Properties"])


if __name__ == "__main__":
    unittest.main()
