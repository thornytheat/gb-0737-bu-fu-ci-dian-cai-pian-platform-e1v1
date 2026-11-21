from app.modules.m8_shifts.service import shifts
from app.modules.m8_shifts.solver import partition_pieces


def test_urgent_pieces_all_land_in_shift_one(dirty):
    sheet = dirty["shifts"]
    for shift in sheet["shifts"]:
        if shift["shift"] == 2:
            assert shift["urgent_pieces"] == 0
    assert sheet["urgent_pieces_shift_one"] == 3
    assert all(piece["urgent"] for piece in sheet["shifts"][0]["pieces"]
               if piece["order"] in {"C001", "C004"})


def test_every_piece_assigned_to_exactly_one_shift(dirty):
    assigned = [(piece["id"], piece["shift"]) for shift in dirty["shifts"]["shifts"] for piece in shift["pieces"]]
    expected = [piece["id"] for piece in dirty["changeovers"]["placements"]]
    assert sorted(piece_id for piece_id, _ in assigned) == sorted(expected)
    assert len(assigned) == len(set(piece_id for piece_id, _ in assigned))
    assert {shift for _, shift in assigned} == {1, 2}


def test_split_prefers_whole_rolls_over_naive_prefix_cut(dirty):
    # 朴素前缀切分会把全部 8 片压到一班；本方案按卷切分，两班都有活且零跨班卷。
    sheet = dirty["shifts"]
    assert sheet["both_busy"] is True
    assert sheet["split_roll_count"] == 0
    assert sheet["handoffs"] == []
    assert [shift["rolls"] for shift in sheet["shifts"]] == [["R-002", "R-005"], ["R-001"]]
    assert sheet["total_changes"] == 1


def test_each_shift_shows_where_it_opens_the_first_roll(dirty):
    first, second = dirty["shifts"]["shifts"]
    assert (first["first_roll"], first["first_piece"], first["first_cut_mm"]) == ("R-002", "C001/01", 11)
    assert (second["first_roll"], second["first_piece"], second["first_cut_mm"]) == ("R-001", "C002/01", 0)


def test_clean_sample_balances_both_shifts_without_split_roll(clean):
    sheet = clean["shifts"]
    assert sheet["both_busy"] is True and sheet["split_roll_count"] == 0
    assert [shift["piece_count"] for shift in sheet["shifts"]] == [5, 4]
    assert sheet["shifts"][0]["urgent_pieces"] == 4 and sheet["shifts"][1]["urgent_pieces"] == 0


def test_split_roll_handoff_lands_on_a_kerf_and_names_remaining_segment():
    rolls = [{"id": "R-1", "fabric": "棉", "width": 1500, "length": 2000, "held": False,
              "blocked_mm": 0, "available": [{"start": 0, "end": 2000}], "blocked": []}]
    kerf = 5
    placements = []
    cursor = 0
    for order_id, length, qty in (("U1", 400, 2), ("N1", 300, 2)):
        for seq in range(1, qty + 1):
            placements.append({"id": f"{order_id}/{seq:02}", "order": order_id, "roll": "R-1",
                               "start": cursor, "end": cursor + length, "kerf_start": cursor + length,
                               "kerf_end": cursor + length + kerf, "length": length, "source": {"ref": "syn:1"}})
            cursor += length + kerf
    fit = {"kerf_mm": kerf, "orders": [
        {"id": "U1", "priority": "急", "length": 400, "quantity": 2},
        {"id": "N1", "priority": "常", "length": 300, "quantity": 2}]}
    planning = {"placements": placements, "roll_sequence": ["R-1"], "orders": [
        {"order": "U1", "priority": "急", "requested": 2, "planned": 2, "remaining": 0, "review": None},
        {"order": "N1", "priority": "常", "requested": 2, "planned": 2, "remaining": 0, "review": None}]}
    sheet = shifts(rolls, fit, planning)
    assert sheet["split_roll_count"] == 1
    handoff = sheet["handoffs"][0]
    assert (handoff["roll"], handoff["handoff_mm"]) == ("R-1", 810)
    assert (handoff["shift_one_ends"], handoff["shift_two_starts"]) == ("U1/02", "N1/01")
    assert (handoff["segment_start"], handoff["segment_end"], handoff["remaining_mm"]) == (0, 2000, 1190)
    assert sheet["roll_cuts"][0]["split"] is True


def test_solver_never_splits_an_urgent_piece_inside_one_roll():
    placements = [
        {"id": "U/01", "order": "U", "roll": "R", "start": 0, "end": 100, "kerf_start": 100, "kerf_end": 105},
        {"id": "N/01", "order": "N", "roll": "R", "start": 105, "end": 205, "kerf_start": 205, "kerf_end": 210},
    ]
    cuts, groups = partition_pieces(placements, {"U": "急", "N": "常"})
    assert cuts["R"] >= 1
    assert cuts["R"] in (1, 2)


def test_roll_with_only_a_blocked_gap_between_pieces_is_not_split():
    rolls = [{"id": "R-1", "fabric": "棉", "width": 1500, "length": 2000, "held": False,
              "blocked_mm": 200, "available": [{"start": 0, "end": 900}, {"start": 1100, "end": 2000}],
              "blocked": [{"start": 900, "end": 1100}]}]
    kerf = 5
    placements = [
        {"id": "U/01", "order": "U", "roll": "R-1", "start": 0, "end": 400, "kerf_start": 400,
         "kerf_end": 405, "length": 400, "source": {"ref": "s:1"}},
        {"id": "N/01", "order": "N", "roll": "R-1", "start": 1100, "end": 1500, "kerf_start": 1500,
         "kerf_end": 1505, "length": 400, "source": {"ref": "s:2"}},
    ]
    fit = {"kerf_mm": kerf, "orders": [{"id": "U", "priority": "急", "length": 400, "quantity": 1},
                                        {"id": "N", "priority": "常", "length": 400, "quantity": 1}]}
    planning = {"placements": placements, "roll_sequence": ["R-1"], "orders": [
        {"order": "U", "priority": "急", "requested": 1, "planned": 1, "remaining": 0, "review": None},
        {"order": "N", "priority": "常", "requested": 1, "planned": 1, "remaining": 0, "review": None}]}
    sheet = shifts(rolls, fit, planning)
    assert sheet["handoffs"] == []
    assert sheet["roll_cuts"][0]["split"] is False
    assert [piece["id"] for piece in sheet["shifts"][0]["pieces"]] == ["U/01", "N/01"]
    assert sheet["shifts"][1]["pieces"] == []


def test_urgent_shortfall_is_flagged_but_does_not_break_shift_invariants(dirty):
    sheet = dirty["shifts"]
    # C004 只排出 1 片（R-003 封存），欠料工单显式登记而不是塞进二班。
    assert sheet["urgent_at_risk"] == ["C004"]
    assert sheet["piece_count"] == len(dirty["changeovers"]["placements"])
