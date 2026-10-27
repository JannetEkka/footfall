"""Which /v2/insights parameters each filter.type accepts.

Copied from Qloo's Entity Type Parameter Guide (docs.qloo.com, read 2026-10-26).
The hackathon API silently ignores parameters that a filter.type doesn't
support, so a request with a typo or an unsupported filter returns plausible
but unfiltered results. The client checks every request against this table and
refuses it instead.

Location signals are a special case. The guide lists filter.location and
filter.location.radius only for places, destinations and heatmaps. Qloo's
Location-Based Insights and Taste Analysis pages also show signal.location on
movies and tags. Those are kept in PROBE_ONLY until a live A/B check (same
request, two far-apart locations) shows they change the results.
"""

from __future__ import annotations

_COMMON = {
    "filter.type",
    "bias.trends",
    "filter.exclude.entities",
    "filter.exclude.tags",
    "operator.exclude.tags",
    "filter.external.exists",
    "operator.filter.external.exists",
    "filter.parents.types",
    "filter.popularity.min",
    "filter.popularity.max",
    "filter.results.entities",
    "filter.results.entities.query",
    "filter.tags",
    "operator.filter.tags",
    "offset",
    "signal.demographics.age",
    "signal.demographics.audiences",
    "signal.demographics.audiences.weight",
    "signal.demographics.gender",
    "signal.interests.entities",
    "signal.interests.tags",
    "take",
    "page",
}

_GEO_FILTERS = {
    "filter.geocode.name",
    "filter.geocode.admin1_region",
    "filter.geocode.admin2_region",
    "filter.geocode.country_code",
    "filter.location",
    "filter.location.radius",
    "filter.location.geohash",
    "filter.exclude.location.geohash",
}

SUPPORTED: dict[str, set[str]] = {
    "urn:entity:artist": set(_COMMON),
    "urn:entity:book": _COMMON | {"filter.publication_year.min", "filter.publication_year.max"},
    "urn:entity:brand": set(_COMMON),
    "urn:entity:destination": _COMMON | _GEO_FILTERS,
    "urn:entity:movie": _COMMON
    | {
        "filter.content_rating",
        "filter.release_year.min",
        "filter.release_year.max",
        "filter.release_country",
        "operator.filter.release_country",
        "filter.rating.min",
        "filter.rating.max",
    },
    "urn:entity:person": _COMMON
    | {
        "filter.date_of_birth.min",
        "filter.date_of_birth.max",
        "filter.date_of_death.min",
        "filter.date_of_death.max",
        "filter.gender",
    },
    "urn:entity:place": _COMMON
    | _GEO_FILTERS
    | {
        "bias.quality",
        "filter.address",
        "filter.external.tripadvisor.rating.count.max",
        "filter.external.tripadvisor.rating.count.min",
        "filter.external.tripadvisor.rating.max",
        "filter.external.tripadvisor.rating.min",
        "filter.hotel_class.max",
        "filter.hotel_class.min",
        "filter.hours",
        "filter.price_level.min",
        "filter.price_level.max",
        "filter.price_range.from",
        "filter.price_range.to",
        "filter.properties.business_rating.min",
        "filter.properties.business_rating.max",
        "filter.properties.resy.rating.min",
        "filter.properties.resy.rating.max",
        "filter.references_brand",
        "filter.resy.rating_count.min",
        "filter.resy.rating_count.max",
        "filter.resy.rating.party.min",
        "filter.resy.rating.party.max",
        "sort_by",
    },
    "urn:entity:podcast": set(_COMMON),
    "urn:entity:tv_show": _COMMON
    | {
        "filter.content_rating",
        "filter.finale_year.max",
        "filter.finale_year.min",
        "filter.latest_known_year.max",
        "filter.latest_known_year.min",
        "filter.release_year.max",
        "filter.release_year.min",
        "filter.release_country",
        "operator.filter.release_country",
        "filter.rating.max",
        "filter.rating.min",
    },
    "urn:entity:video_game": set(_COMMON),
    "urn:heatmap": {
        "filter.type",
        "filter.location",
        "filter.location.query",
        "filter.location.radius",
        "heatmap.dimensions",
        "filter.dimensions.hotel_class",
        "filter.dimensions.price_level",
        "output.heatmap.boundary",
        "signal.demographics.age",
        "signal.demographics.audiences",
        "signal.demographics.gender",
        "signal.interests.entities",
        "signal.interests.tags",
        "take",
    },
    # The guide's Tag table is short; the Taste Analysis page adds the
    # signal and tag-type filters used below.
    "urn:tag": {
        "filter.type",
        "diversify.by",
        "diversify.take",
        "offset",
        "take",
        "page",
        "filter.tag.types",
        "filter.parents.types",
        "signal.interests.entities",
        "signal.interests.tags",
        "signal.demographics.audiences",
    },
    "urn:demographics": {"filter.type", "signal.interests.entities", "signal.interests.tags"},
}

# Documented on use-case pages but not in the Entity Type Parameter Guide.
# Allowed only when the caller says it is probing, and the result is labelled.
PROBE_ONLY: dict[str, set[str]] = {
    "urn:entity:movie": {"signal.location", "signal.location.query", "signal.location.radius"},
    "urn:entity:artist": {"signal.location", "signal.location.query", "signal.location.radius"},
    "urn:entity:book": {"signal.location", "signal.location.query", "signal.location.radius"},
    "urn:entity:brand": {"signal.location", "signal.location.query", "signal.location.radius"},
    "urn:tag": {"signal.location", "signal.location.query", "signal.location.radius"},
}

ENTITY_TYPES = [t for t in SUPPORTED if t.startswith("urn:entity:")]


class UnsupportedParameter(ValueError):
    pass


def check(params: dict, probe: bool = False) -> None:
    """Raise if any parameter isn't documented for params['filter.type']."""
    ftype = params.get("filter.type")
    if ftype not in SUPPORTED:
        raise UnsupportedParameter(f"unknown filter.type {ftype!r}")
    allowed = SUPPORTED[ftype] | (PROBE_ONLY.get(ftype, set()) if probe else set())
    bad = sorted(k for k in params if k not in allowed)
    if bad:
        raise UnsupportedParameter(
            f"{', '.join(bad)} not supported for {ftype} (Qloo would silently ignore it)"
        )
