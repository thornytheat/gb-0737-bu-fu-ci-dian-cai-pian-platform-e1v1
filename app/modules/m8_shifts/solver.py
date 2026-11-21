from itertools import product


def group_by_roll(placements):
    """按卷归集裁片，保持 M5 排程在每卷内自卷首向卷尾的刀口顺序。"""
    groups = {}
    for piece in placements:
        groups.setdefault(piece["roll"], []).append(piece)
    return groups


def urgent_last_index(items, priority):
    return max((index for index, piece in enumerate(items) if priority[piece["order"]] == "急"), default=-1)


def cut_ranges(groups, priority):
    """每卷的切口候选：急单裁片必须整段落在一班，且跨班只能沿同段内的刀口切开。

    禁切区两侧的裁片之间不存在共用刀口，禁止在段边界分班；整卷归入一班
    （索引 = 总片数）始终可行，故候选集合不会为空。
    """
    ranges = {}
    for roll_id, items in groups.items():
        floor = urgent_last_index(items, priority) + 1
        candidates = [index for index in range(floor, len(items) + 1)
                      if index in (0, len(items)) or items[index - 1]["kerf_end"] == items[index]["start"]]
        ranges[roll_id] = candidates
    return ranges


def evaluate(vector, roll_ids, groups):
    shift_one_rolls, shift_two_rolls = set(), set()
    split_rolls, count_one, count_two = [], 0, 0
    for roll_id, cut in zip(roll_ids, vector):
        items = groups[roll_id]
        if cut > 0:
            shift_one_rolls.add(roll_id)
        if cut < len(items):
            shift_two_rolls.add(roll_id)
        if 0 < cut < len(items):
            split_rolls.append(roll_id)
        count_one += cut
        count_two += len(items) - cut
    both_busy = bool(shift_one_rolls) and bool(shift_two_rolls)
    changes = max(0, len(shift_one_rolls) - 1) + max(0, len(shift_two_rolls) - 1)
    # 排序目标：两班都有活 → 跨班卷最少（少截断卷、少换卷）→ 换卷次数最少 → 班间片数均衡 → 稳定字典序。
    rank = (0 if both_busy else 1, len(split_rolls), changes, abs(count_one - count_two), vector)
    metrics = {"both_busy": both_busy, "split_rolls": split_rolls, "changes": changes,
               "shift_one_rolls": shift_one_rolls, "shift_two_rolls": shift_two_rolls,
               "count_one": count_one, "count_two": count_two}
    return rank, metrics


def partition_pieces(placements, priority):
    """返回 {卷号: 一班裁片数} 的切分向量。每片只落一个班次，跨班只允许沿刀口切开。"""
    groups = group_by_roll(placements)
    roll_ids = tuple(groups)
    if not roll_ids:
        return {}, groups
    ranges = cut_ranges(groups, priority)
    best_vector, best_rank = None, None
    for vector in product(*(ranges[roll_id] for roll_id in roll_ids)):
        rank, _ = evaluate(vector, roll_ids, groups)
        if best_rank is None or rank < best_rank:
            best_rank, best_vector = rank, vector
    return dict(zip(roll_ids, best_vector)), groups
