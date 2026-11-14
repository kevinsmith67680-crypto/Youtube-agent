"""Signal classes: what a provider's data can legitimately be used to claim.

Every provider is classified by what it measures, not by what it is called. The class
decides which operating mode a provider can unlock (see :mod:`trend_agent.ops.modes`):

* ``DIRECT_YOUTUBE_SEARCH_INTEREST``  relative interest in YouTube Search itself. The only
  class that can support a claim of a YouTube Search surge.
* ``EXTERNAL_SEARCH_INTEREST``        search interest on another property (e.g. Google Web
  Search). Never YouTube Search demand.
* ``SEARCH_SUGGESTION_PRESENCE``      the phrase appears in a suggestion system. Presence only;
  no volume.
* ``VIDEO_ACTIVITY``                  video publication / attention (supply, not demand).
* ``COMMUNITY_ACTIVITY``              community discussion from authorised APIs.
* ``OFFICIAL_EVENT_SIGNAL``           official releases, announcements, patches.
* ``DEVELOPER_ACTIVITY``              external developer activity on GitHub (repositories,
  stars, forks, pushes). Never search interest.
* ``SYNTHETIC_TEST_DATA``             artificial data; never live intelligence.
"""

from __future__ import annotations

from enum import StrEnum

from trend_agent.schemas.enums import MeasurementType, ProviderType, SearchProperty
from trend_agent.schemas.provider import ProviderDescriptor


class SignalClass(StrEnum):
    DIRECT_YOUTUBE_SEARCH_INTEREST = "DIRECT_YOUTUBE_SEARCH_INTEREST"
    EXTERNAL_SEARCH_INTEREST = "EXTERNAL_SEARCH_INTEREST"
    SEARCH_SUGGESTION_PRESENCE = "SEARCH_SUGGESTION_PRESENCE"
    VIDEO_ACTIVITY = "VIDEO_ACTIVITY"
    COMMUNITY_ACTIVITY = "COMMUNITY_ACTIVITY"
    OFFICIAL_EVENT_SIGNAL = "OFFICIAL_EVENT_SIGNAL"
    DEVELOPER_ACTIVITY = "DEVELOPER_ACTIVITY"
    SYNTHETIC_TEST_DATA = "SYNTHETIC_TEST_DATA"


SEARCH_INTEREST_CLASSES = frozenset(
    {SignalClass.DIRECT_YOUTUBE_SEARCH_INTEREST, SignalClass.EXTERNAL_SEARCH_INTEREST}
)
PROXY_CLASSES = frozenset(
    {
        SignalClass.SEARCH_SUGGESTION_PRESENCE,
        SignalClass.VIDEO_ACTIVITY,
        SignalClass.COMMUNITY_ACTIVITY,
        SignalClass.OFFICIAL_EVENT_SIGNAL,
        SignalClass.DEVELOPER_ACTIVITY,
    }
)

# Providers whose class cannot be read from the descriptor alone (discovery providers
# declare a generic measurement type; what they observe is fixed by their API).
_KNOWN: dict[str, tuple[SignalClass, ...]] = {
    "youtube_data_api": (SignalClass.VIDEO_ACTIVITY,),
    "youtube_discovery": (SignalClass.VIDEO_ACTIVITY,),
    "feeds": (SignalClass.OFFICIAL_EVENT_SIGNAL,),
    "github": (SignalClass.OFFICIAL_EVENT_SIGNAL, SignalClass.DEVELOPER_ACTIVITY),
    "hackernews": (SignalClass.COMMUNITY_ACTIVITY,),
    "hackernews_firebase": (SignalClass.COMMUNITY_ACTIVITY,),
    "reddit": (SignalClass.COMMUNITY_ACTIVITY,),
    "google_trends_discovery": (SignalClass.SEARCH_SUGGESTION_PRESENCE,),
    # operator-curated seed phrases: an input list, not a measurement of anything
    "curated_seeds": (),
}


def classify(descriptor: ProviderDescriptor) -> tuple[SignalClass, ...]:
    """Signal classes a provider can supply (empty: it measures nothing)."""
    if descriptor.provider_type is ProviderType.SYNTHETIC:
        return (SignalClass.SYNTHETIC_TEST_DATA,)
    if descriptor.name in _KNOWN:
        return _KNOWN[descriptor.name]
    out: list[SignalClass] = []
    props = descriptor.search_properties
    types = descriptor.measurement_types
    if MeasurementType.DIRECT_SEARCH_INTEREST in types and SearchProperty.YOUTUBE_SEARCH in props:
        out.append(SignalClass.DIRECT_YOUTUBE_SEARCH_INTEREST)
    if MeasurementType.EXTERNAL_SEARCH_INTEREST in types:
        out.append(SignalClass.EXTERNAL_SEARCH_INTEREST)
    if MeasurementType.SEARCH_SUGGESTION in types:
        out.append(SignalClass.SEARCH_SUGGESTION_PRESENCE)
    if MeasurementType.VIDEO_ACTIVITY in types:
        out.append(SignalClass.VIDEO_ACTIVITY)
    return tuple(out)
