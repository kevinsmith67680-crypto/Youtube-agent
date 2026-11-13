"""Region and language code validation.

Embedded code lists avoid a heavy runtime dependency. Sources:
* ISO 3166-1 alpha-2 officially assigned codes (249).
* ISO 639-1 two-letter language codes.

YouTube's ``regionCode`` is ISO 3166-1 alpha-2 and ``relevanceLanguage`` is ISO 639-1
(with script subtags such as ``zh-Hans``), so these lists match the provider contract.
"""

from __future__ import annotations

import re

WORLD = "WORLD"  # sentinel for global / no geographic restriction

ISO_3166_1_ALPHA2: frozenset[str] = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
    BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
    DE DJ DK DM DO DZ
    EC EE EG EH ER ES ET
    FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
    HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT
    JE JM JO JP
    KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY
    MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
    NA NC NE NF NG NI NL NO NP NR NU NZ
    OM
    PA PE PF PG PH PK PL PM PN PR PS PT PW PY
    QA
    RE RO RS RU RW
    SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
    TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
    UA UG UM US UY UZ
    VA VC VE VG VI VN VU
    WF WS
    YE YT
    ZA ZM ZW
    """.split()
)

ISO_639_1: frozenset[str] = frozenset(
    """
    aa ab ae af ak am an ar as av ay az ba be bg bi bm bn bo br bs ca ce ch co cr cs cu cv
    cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho
    hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku
    kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn
    no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm
    sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi
    vo wa wo xh yi yo za zh zu
    """.split()
)

_COMMON_REGION_MISTAKES = {"UK": "GB", "EN": "GB"}
_SUBDIVISION = re.compile(r"^([A-Z]{2})-([A-Z0-9]{1,3})$")
_LANG_TAG = re.compile(
    r"^(?P<lang>[A-Za-z]{2})"
    r"(?:-(?P<script>[A-Za-z]{4}))?"
    r"(?:-(?P<region>[A-Za-z]{2}|\d{3}))?$"
)


class CodeValidationError(ValueError):
    """Raised when a region or language code is invalid."""


def normalize_region(code: str, *, allow_world: bool = True, allow_subdivision: bool = True) -> str:
    """Validate and normalise a region / geography code.

    Accepts ISO 3166-1 alpha-2 (``GB``), optionally ISO 3166-2 subdivisions (``GB-ENG``)
    and the ``WORLD`` sentinel. Returns the upper-cased canonical form.
    """
    if not isinstance(code, str) or not code.strip():
        raise CodeValidationError("region code is empty")
    value = code.strip().upper()
    if value == WORLD:
        if allow_world:
            return WORLD
        raise CodeValidationError("a specific ISO 3166-1 region is required, not WORLD")
    if value in ISO_3166_1_ALPHA2:
        return value
    match = _SUBDIVISION.match(value) if allow_subdivision else None
    if match and match.group(1) in ISO_3166_1_ALPHA2:
        return value
    hint = _COMMON_REGION_MISTAKES.get(value)
    suffix = f" (did you mean {hint!r}?)" if hint else ""
    raise CodeValidationError(f"invalid region code {code!r}{suffix}")


def normalize_language(tag: str) -> str:
    """Validate and normalise a language tag (``en``, ``pt-BR``, ``zh-Hans``, ``es-419``)."""
    if not isinstance(tag, str) or not tag.strip():
        raise CodeValidationError("language tag is empty")
    match = _LANG_TAG.match(tag.strip().replace("_", "-"))
    if not match:
        raise CodeValidationError(f"invalid language tag {tag!r}")
    lang = match.group("lang").lower()
    if lang not in ISO_639_1:
        raise CodeValidationError(f"unknown ISO 639-1 language code {lang!r}")
    parts = [lang]
    if script := match.group("script"):
        parts.append(script.title())
    if region := match.group("region"):
        region = region.upper()
        if not region.isdigit() and region not in ISO_3166_1_ALPHA2:
            raise CodeValidationError(f"invalid region subtag {region!r} in {tag!r}")
        parts.append(region)
    return "-".join(parts)


def is_valid_region(code: str) -> bool:
    try:
        normalize_region(code)
    except CodeValidationError:
        return False
    return True


def is_valid_language(tag: str) -> bool:
    try:
        normalize_language(tag)
    except CodeValidationError:
        return False
    return True
