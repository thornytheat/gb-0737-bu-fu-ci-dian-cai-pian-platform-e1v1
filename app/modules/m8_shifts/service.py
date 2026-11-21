from app.modules.m8_shifts.solver import evaluate, partition_pieces

SHIFT_LABELS = {1: "一班", 2: "二班"}
SHIFTS = (1, 2)


def segment_span(roll, point):
    """找到包含刀口坐标 point 的可用段，用于在作业单上写明从哪段开刀。"""
    for span in roll["available"]:
        if span["start"] <= point <= span["end"]:
            return span
    raise RuntimeError("刀口落在可用段之外")


def build_handoffs(groups, cuts, rolls):
    by_roll = {roll["id"]: roll for roll in rolls}
    handoffs = []
    for roll_id in groups:
        cut = cuts.get(roll_id, 0)
        items = groups[roll_id]
        if not (0 < cut < len(items)):
            continue
        head, tail = items[cut - 1], items[cut]
        if head["kerf_end"] != tail["start"]:
            raise RuntimeError("跨班切口未对齐刀口，禁止截断裁片")
        roll = by_roll[roll_id]
        span = segment_span(roll, head["kerf_end"])
        handoffs.append({
            "roll": roll_id, "handoff_mm": head["kerf_end"], "segment_start": span["start"], "segment_end": span["end"],
            "shift_one_ends": head["id"], "shift_two_starts": tail["id"],
            "remaining_mm": span["end"] - head["kerf_end"],
        })
    return handoffs


def shift_pieces(groups, cuts, priority):
    assigned = {1: [], 2: []}
    for roll_id, items in groups.items():
        cut = cuts.get(roll_id, 0)
        for index, piece in enumerate(items):
            record = {**piece, "shift": 1 if index < cut else 2, "urgent": priority[piece["order"]] == "急"}
            assigned[record["shift"]].append(record)
    return assigned


def build_shift(shift, pieces, roll_order):
    visited = []
    for roll_id in roll_order:
        if any(piece["roll"] == roll_id for piece in pieces) and roll_id not in visited:
            visited.append(roll_id)
    first = next((piece for piece in pieces if piece["roll"] == visited[0]), None) if visited else None
    return {
        "shift": shift, "label": SHIFT_LABELS[shift], "pieces": pieces,
        "rolls": visited, "roll_changes": max(0, len(visited) - 1),
        "piece_count": len(pieces),
        "urgent_pieces": sum(1 for piece in pieces if piece["urgent"]),
        "first_roll": first["roll"] if first else None,
        "first_piece": first["id"] if first else None,
        "first_cut_mm": first["start"] if first else None,
    }


def shifts(rolls, fit_result, planning):
    priority = {order["id"]: order["priority"] for order in fit_result["orders"]}
    cuts, groups = partition_pieces(planning["placements"], priority)
    roll_ids = tuple(groups)
    _, metrics = evaluate(tuple(cuts.get(roll_id, 0) for roll_id in roll_ids), roll_ids, groups)

    assigned = shift_pieces(groups, cuts, priority)
    # 每卷按排程顺序访问；一班先领、二班接刀，顺序由 M5 领卷次序稳定决定。
    order = [roll_id for roll_id in planning["roll_sequence"] if roll_id in groups]
    order += [roll_id for roll_id in roll_ids if roll_id not in order]
    shift_docs = [build_shift(shift, assigned[shift], order) for shift in SHIFTS]

    handoffs = build_handoffs(groups, cuts, rolls)
    urgent_requested = {order["order"]: order["requested"] for order in planning["orders"] if order["priority"] == "急"}
    urgent_done = {order_id: 0 for order_id in urgent_requested}
    for piece in planning["placements"]:
        if piece["order"] in urgent_done:
            urgent_done[piece["order"]] += 1
    urgent_at_risk = [order_id for order_id, quantity in urgent_requested.items() if urgent_done[order_id] < quantity]

    all_pieces = [piece for shift in shift_docs for piece in shift["pieces"]]
    assert_invariants(planning, all_pieces, handoffs, metrics)
    return {
        "kerf_mm": fit_result["kerf_mm"],
        "shifts": shift_docs,
        "roll_cuts": [{"roll": roll_id, "shift_one_pieces": cuts.get(roll_id, 0),
                       "total_pieces": len(groups[roll_id]), "split": 0 < cuts.get(roll_id, 0) < len(groups[roll_id])}
                      for roll_id in order],
        "handoffs": handoffs,
        "split_roll_count": len(metrics["split_rolls"]),
        "total_changes": sum(shift["roll_changes"] for shift in shift_docs),
        "both_busy": metrics["both_busy"],
        "piece_count": len(all_pieces),
        "urgent_pieces_shift_one": shift_docs[0]["urgent_pieces"],
        "urgent_at_risk": urgent_at_risk,
    }


def assert_invariants(planning, all_pieces, handoffs, metrics):
    # 每片只能落到一个班次。
    if sorted(piece["id"] for piece in all_pieces) != sorted(piece["id"] for piece in planning["placements"]):
        raise RuntimeError("分班裁片与排程不一致")
    if len({piece["id"] for piece in all_pieces}) != len(all_pieces):
        raise RuntimeError("同一裁片被重复分班")
    # 每段刀口只能落到一个班次：跨班卷必须沿既有刀口切开，不得在刀口中间断开。
    handoff_rolls = {item["roll"] for item in handoffs}
    if handoff_rolls != set(metrics["split_rolls"]):
        raise RuntimeError("跨班余料交接与切分校验不符")
    # 急单全部排在一班。
    if any(piece["shift"] == 2 and piece["urgent"] for piece in all_pieces):
        raise RuntimeError("急单被排入二班")
