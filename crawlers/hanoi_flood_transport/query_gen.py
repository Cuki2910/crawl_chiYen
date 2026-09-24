"""Bounded, deterministic search-query generator for the Hanoi flood-transport trial.

Full Cartesian product (flood x transport x location) is tens of thousands of
combinations — too large for a bounded trial. Uses 2-axis pairing (flood_state x
hanoi_location as the primary pair) with transport_impact rotated round-robin,
capped by max_queries.
"""

from crawlers.hanoi_flood_transport.keywords import GROUPS, literal_terms


def generate_queries(groups=GROUPS, max_queries=30):
    """Deterministically build up to max_queries search strings.

    Pairing: flood_state[i % F] + hanoi_location[(i // F) % L] + transport_impact[i % T]
    """
    floods = literal_terms("flood_state", groups)
    locations = literal_terms("hanoi_location", groups)
    transports = literal_terms("transport_impact", groups)
    if not floods or not locations:
        return []
    queries, seen = [], set()
    i = 0
    total_pairs = len(floods) * len(locations)
    while len(queries) < max_queries and i < total_pairs:
        flood = floods[i % len(floods)]
        location = locations[(i // len(floods)) % len(locations)]
        transport = transports[i % len(transports)] if transports else ""
        query = " ".join(part for part in (flood, location, transport) if part)
        if query not in seen:
            seen.add(query)
            queries.append(query)
        i += 1
    return queries
