from figure_builder import build_subject_payload_parts


def build_payload(subject_id, name, unit, lo, hi, traces):
    data, layout = build_subject_payload_parts(traces, lo, hi, name, unit)
    return {"id": subject_id, "name": name, "data": data, "layout": layout}
