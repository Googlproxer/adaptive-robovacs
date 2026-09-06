"""Tests for pure floor-plan command decoding and reducers."""

from __future__ import annotations

import unittest

from custom_components.adaptive_robovacs.floor_plans import (
    FloorPlanWrite,
    decode_floor_plan_write,
    replace_floor_plan,
    replace_room_adjacency,
)
from custom_components.adaptive_robovacs.state import (
    FloorPlanRectangle,
    FloorPlanSensorMarker,
    FloorPlanState,
)


class FloorPlanReducerTests(unittest.TestCase):
    def test_replaces_only_the_selected_floor_and_increments_revision(self) -> None:
        current = FloorPlanState(
            revision=2,
            rooms={
                "upstairs": FloorPlanRectangle("upper", 0, 0, 10, 10),
                "removed": FloorPlanRectangle("ground", 20, 0, 10, 10),
            },
            edges={("removed", "study")},
            sensors={
                "sensor-up": FloorPlanSensorMarker("upstairs", 5, 5),
                "sensor-removed": FloorPlanSensorMarker("removed", 25, 5),
            },
        )
        request = decode_floor_plan_write(
            "ground",
            2,
            {"study": {"x": 1, "y": 2, "width": 8, "height": 6}},
            (),
            {"sensor-study": {"area_id": "study", "x": 4, "y": 5}},
            forget_area_ids=("removed",),
            forget_sensor_registry_ids=("sensor-removed",),
        )

        updated = replace_floor_plan(
            current,
            request,
            room_floor_by_id={"study": "ground", "upstairs": "upper"},
            sensor_owner_by_registry_id={
                "sensor-study": "study",
                "sensor-up": "upstairs",
            },
        )

        self.assertEqual(updated.revision, 3)
        self.assertEqual(set(updated.rooms), {"study", "upstairs"})
        self.assertEqual(set(updated.sensors), {"sensor-study", "sensor-up"})
        self.assertEqual(current.revision, 2)
        self.assertIn("removed", current.rooms)

    def test_rejects_stale_revision_cross_floor_edges_and_sensor_rebinding(
        self,
    ) -> None:
        current = FloorPlanState(revision=3)
        rooms = {"study": "ground", "bedroom": "upper"}
        sensors = {"radar-study": "study"}
        cases = (
            decode_floor_plan_write("ground", 2, {}, (), {}),
            decode_floor_plan_write(
                "ground",
                3,
                {},
                (("study", "bedroom"),),
                {},
            ),
            decode_floor_plan_write(
                "ground",
                3,
                {},
                (),
                {"radar-study": {"area_id": "bedroom", "x": 1, "y": 1}},
            ),
        )

        for request in cases:
            with self.subTest(request=request), self.assertRaises(ValueError):
                replace_floor_plan(
                    current,
                    request,
                    room_floor_by_id=rooms,
                    sensor_owner_by_registry_id=sensors,
                )

    def test_room_adjacency_replaces_old_edges_without_mutating_input(self) -> None:
        current = FloorPlanState(
            revision=1,
            edges={("hall", "study"), ("hall", "kitchen")},
        )

        updated = replace_room_adjacency(
            current,
            "study",
            ("kitchen",),
            room_floor_by_id={
                "study": "ground",
                "hall": "ground",
                "kitchen": "ground",
            },
        )

        self.assertEqual(
            updated.edges,
            {("hall", "kitchen"), ("kitchen", "study")},
        )
        self.assertEqual(current.edges, {("hall", "study"), ("hall", "kitchen")})

    def test_decode_rejects_empty_floor_and_non_integer_revision(self) -> None:
        for floor_id, revision in (("", 0), ("ground", True), ("ground", "0")):
            with (
                self.subTest(floor_id=floor_id, revision=revision),
                self.assertRaises(ValueError),
            ):
                decode_floor_plan_write(floor_id, revision, {}, (), {})

    def test_replacement_rejects_unknown_or_cross_floor_payload_objects(self) -> None:
        current = FloorPlanState()
        base = FloorPlanWrite("ground", 0, (), (), ())
        with self.assertRaisesRegex(ValueError, "unknown floor"):
            replace_floor_plan(
                current,
                base,
                room_floor_by_id={"study": "upper"},
                sensor_owner_by_registry_id={},
            )

        wrong_room = FloorPlanWrite(
            "ground",
            0,
            (("study", FloorPlanRectangle("upper", 0, 0, 5, 5)),),
            (),
            (),
        )
        with self.assertRaisesRegex(ValueError, "stay on"):
            replace_floor_plan(
                current,
                wrong_room,
                room_floor_by_id={"study": "ground"},
                sensor_owner_by_registry_id={},
            )

        unknown_room = FloorPlanWrite(
            "ground",
            0,
            (("missing", FloorPlanRectangle("ground", 0, 0, 5, 5)),),
            (),
            (),
        )
        with self.assertRaisesRegex(ValueError, "discovered rooms"):
            replace_floor_plan(
                current,
                unknown_room,
                room_floor_by_id={"study": "ground"},
                sensor_owner_by_registry_id={},
            )

    def test_replacement_rejects_unknown_sensor_and_live_forget_requests(self) -> None:
        current = FloorPlanState(
            rooms={"stale": FloorPlanRectangle("ground", 0, 0, 5, 5)},
            sensors={"stale-sensor": FloorPlanSensorMarker("study", 1, 1)},
        )
        request = FloorPlanWrite(
            "ground",
            0,
            (),
            (),
            (("missing", FloorPlanSensorMarker("study", 1, 1)),),
        )
        with self.assertRaisesRegex(ValueError, "discovered sensors"):
            replace_floor_plan(
                current,
                request,
                room_floor_by_id={"study": "ground"},
                sensor_owner_by_registry_id={"radar": "study"},
            )

        for forget_rooms, forget_sensors, expected in (
            (("study",), (), "rooms"),
            ((), ("radar",), "sensors"),
        ):
            request = FloorPlanWrite(
                "ground",
                0,
                (),
                (),
                (),
                forget_area_ids=forget_rooms,
                forget_sensor_registry_ids=forget_sensors,
            )
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(ValueError, expected),
            ):
                replace_floor_plan(
                    current,
                    request,
                    room_floor_by_id={"study": "ground"},
                    sensor_owner_by_registry_id={"radar": "study"},
                )

    def test_adjacency_rejects_unknown_and_cross_floor_neighbors(self) -> None:
        for area_id, neighbors, expected in (
            ("missing", (), "unknown room"),
            ("study", ("bedroom",), "same floor"),
        ):
            with (
                self.subTest(area_id=area_id),
                self.assertRaisesRegex(ValueError, expected),
            ):
                replace_room_adjacency(
                    FloorPlanState(),
                    area_id,
                    neighbors,
                    room_floor_by_id={"study": "ground", "bedroom": "upper"},
                )


if __name__ == "__main__":
    unittest.main()
