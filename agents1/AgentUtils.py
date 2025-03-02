def compute_collected_adjustments(victim_name: str, base_adjustment: float):
    if "mildly" in victim_name:
        return base_adjustment, base_adjustment
    elif "critically" in victim_name:
        return base_adjustment * 2, base_adjustment * 2
