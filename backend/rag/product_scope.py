"""Resolve scope language against an account's readable product catalog."""

import re
import unicodedata
from collections import defaultdict


_MARKET_NAMES = {
    "US": "美国|美國|United States|United States of America|USA|U.S.|U.S.A.|American",
    "CA": "加拿大|Canada|Canadian",
    "GB UK": "英国|英國|United Kingdom|Great Britain|U.K.|British",
    "DE": "德国|德國|Germany|Deutschland",
    "FR": "法国|法國|France",
    "IT": "意大利|義大利|Italy",
    "ES": "西班牙|Spain",
    "JP": "日本|Japan",
    "AU": "澳大利亚|澳大利亞|澳洲|Australia",
    "MX": "墨西哥|Mexico",
    "BR": "巴西|Brazil",
    "NL": "荷兰|荷蘭|Netherlands",
    "SE": "瑞典|Sweden",
    "PL": "波兰|波蘭|Poland",
    "BE": "比利时|比利時|Belgium",
    "IE": "爱尔兰|愛爾蘭|Ireland",
    "IN": "印度|India",
    "SG": "新加坡|Singapore",
    "AE": "阿联酋|阿聯酋|United Arab Emirates|UAE",
    "SA": "沙特阿拉伯|Saudi Arabia",
    "TR": "土耳其|Turkey|Türkiye",
    "EG": "埃及|Egypt",
    "CN": "中国|中國|China",
    "ZA": "南非|South Africa",
}
_DASHES = str.maketrans("‐‑‒–—−", "------")
_WORDS = re.compile(r"[^\W_]+")
_SEP = r"(?:[^\w。！？；\n]|_)+"
_BOUNDARY = r"[a-z0-9]"
_MODEL_TOKEN = r"[A-Z0-9]+(?:[._-]+[A-Z0-9]+)*"
TOKEN = re.compile(rf"(?<![A-Z0-9_-]){_MODEL_TOKEN}(?![A-Z0-9_-])", re.I | re.ASCII)
# Consume the label before looking for its value.
LABELED_MODEL = re.compile(
    r"(?:\bSKU\b(?:\s+(?:is|number)(?![A-Z0-9]|[._-]+[A-Z0-9]))?+"
    r"|(?:商品|产品)?型号|\b(?:product )?model\b)"
    rf"\s*[:：#=]?\s*[`'\"“]?\s*(?=(?P<value>{TOKEN.pattern}))",
    re.I | re.ASCII,
)
_LABEL = re.compile(
    r"(?:店铺\s*[:=#]?|店舖\s*[:=#]?|\bshop\s*[:=#])\s*(?P<shop>[^\n。！？；]+?)[\s,/、]+"
    r"(?:站点|站點|\bmarketplace|\bsite)\s*[:=#]?\s*(?=[\w.-])",
)
_PATH = re.compile(r"(?<![\w.-])([a-z0-9][\w.-]*)\s*/\s*([a-z0-9][\w.-]*)\s*/\s*"
                   r"([a-z0-9]+(?:[._-]+[a-z0-9]+)*)(?![a-z0-9_-])")
_CUE = re.compile(r"商品资料\s*:\s*|(?:请\s*)?(?:按(?:照)?|查(?:询)?|使用|以|用)\s*"
                  r"|\b(?:use|look up|check|query)\s+(?:the\s+)?")
_SITE_MARKER = re.compile(r"\s*(?:(?:电商)?(?:站点|站點|站|店铺|店舖|店|商城|市场|市場)(?:在售|販售|销售|銷售|上架)?(?:的)?"
                          r"|(?:offer|listing|store|marketplace|site)\b(?:\s+(?:of|for))?)")
_SKU_AFTER = re.compile(
    r"""\s*(?:(?:的|在售的|販售的|of\b|for\b)\s*)?(?:(?:sku\b|型号|\bmodel\b)\s*[:=#]?\s*)?[`'"“]?\s*""",
)
_SITE_PREPOSITION = re.compile(r"(?:在\s*|\b(?:on|for|from|in)\s+(?:the\s+)?)$")
_CLAUSE_START = re.compile(r"(?:^|[\n。！？；!?;])\s*$")
_SHOP_SUFFIX = re.compile(r"\s*(?:店铺|店舖|店|\bshop\b|\bstore\b)(?:在售|销售|銷售|上架)?")
_SHOP_LABEL = re.compile(r"(?:^|[\n。！？；,，]|请按|按|查(?:询)?|使用)\s*(?:店铺|店舖)\s*:\s*"
                         r"([^\n。！？；,，]+)|\bshop\s*:\s*([^\n。！？；,，]+)")
_MARKET_LABEL = re.compile(r"(?:站点|站點|\bmarketplace|\bsite)\s*[:=]\s*(?=[\w.-])")
_UNKNOWN_SITE = re.compile(r"[\w.-]+")
_OFFER_MARKER = re.compile(r"(?:店(?:铺|舖)?(?:在售)?的|(?:offer|listing)\s+of\b)")


def normalize(text, *, fold_case=True):
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    return text.casefold() if fold_case else text


def _name(text):
    return tuple(_WORDS.findall(normalize(text)))


def _phrase(words):
    text = rf"(?:{_SEP})".join(map(re.escape, words))
    if words[0][0].isascii():
        text = rf"(?<!{_BOUNDARY}){text}"
    if words[-1][-1].isascii():
        text += rf"(?!{_BOUNDARY})"
    return text


def _name_matches(text, names):
    """Prefer a complete longer mention at the same position."""
    matches = sorted(
        (match.start(), -match.end(), name)
        for name in names for match in re.finditer(_phrase(name), text)
    )
    end = 0
    for start, negative_end, name in matches:
        if start >= end:
            end = -negative_end
            yield start, end, name


def _sku_pattern(sku):
    parts = re.split(r"([._-])", normalize(sku).strip())
    body = "".join(r"\s*" + re.escape(part) + r"\s*" if part in "._-" and part else re.escape(part)
                   for part in parts)
    return rf"(?<![a-z0-9_-]){body}(?![a-z0-9_-]|[.][a-z0-9])"


def sku_mentions(text, skus):
    return {sku for sku in skus if re.search(_sku_pattern(sku), text)} if text else set()


def leading_skus(text, skus):
    return {sku for sku in skus if re.match(_sku_pattern(sku), text)}


def _shops(catalog):
    shops = defaultdict(set)
    for (shop, site, _), item in catalog.items():
        for label in (shop, *item["shop_aliases"]):
            if name := _name(label):
                shops[name].add((shop, site))
    return shops


def _markets(catalog, selected_site):
    names = defaultdict(set)
    for codes, aliases in _MARKET_NAMES.items():
        for alias in aliases.split("|"):
            names[_name(alias)].update(codes.split())
        for code in codes.split():
            names[_name(code)].update(codes.split())
    for site in {selected_site, *(scope[1] for scope in catalog)}:
        names[_name(site)].add(site.upper())
    return names


def _choose_coords(catalog, shops, site_ids, shop_name, site_name, bound):
    candidates = shops.get(shop_name, ()) if shop_name else {scope[:2] for scope in catalog if scope[0] == bound[0]}
    pairs = {pair for pair in candidates if pair[1].upper() in site_ids}
    exact = {pair for pair in pairs if site_name == _name(pair[1])}
    return exact or pairs


def catalog_coordinates(catalog, shop, site):
    """Resolve a qualified ID phrase recognized by the existing SKU gate."""
    names = _markets(catalog, site)
    return _choose_coords(catalog, _shops(catalog), names[_name(site)], _name(shop), _name(site), (shop, site))


def _introduced_name(prefix):
    clause = re.split(r"[\n。！？；!?;]|\.\s", prefix)[-1]
    cues = list(_CUE.finditer(clause))
    if cues:
        return _name(clause[cues[-1].end():].strip(" ,，:：/"))
    return None


def _left_shop(text, site_start, shop_mentions, site_marked):
    prefix = text[:site_start]
    if site_marked:
        if _SITE_PREPOSITION.search(prefix):
            return (), True
        if (name := _introduced_name(prefix)) is not None:
            return name, True
    for _, end, name in reversed(shop_mentions):
        if end <= site_start and re.fullmatch(r"[\s,/·、]*(?:店(?:铺|舖)?\s*)?", prefix[end:]):
            return name, True
    return (), False


def _site_at(text, start, sites):
    for offset, end, name in _name_matches(text[start:], sites):
        if offset == 0:
            return name, sites[name], start + end
        break
    unknown = _UNKNOWN_SITE.match(text, start)
    return _name(unknown[0]), {unknown[0].upper()}, unknown.end()


def explicit_coordinates(text, catalog, bound):
    """Yield candidate coordinates for each scope phrase, along with any qualified SKU."""
    shops = _shops(catalog)
    sites = _markets(catalog, bound[1])
    labeled = list(_LABEL.finditer(text))
    for match in labeled:
        site, ids, end = _site_at(text, match.end(), sites)
        yield _choose_coords(catalog, shops, ids, _name(match["shop"]), site, bound), _SKU_AFTER.match(text, end).end()
    for match in _PATH.finditer(text):
        shop, site, sku = match.groups()
        if _name(shop) not in shops and not any(normalize(scope[2]) == sku for scope in catalog) and not (
            re.search(r"[0-9]", sku) and re.search(r"[a-z]", sku)
        ):
            continue
        ids = sites.get(_name(site), {site.upper()})
        yield _choose_coords(catalog, shops, ids, _name(shop), _name(site), bound), match.start(3)

    shop_mentions = list(_name_matches(text, shops))
    known_skus = {scope[2] for scope in catalog}
    market_ends = set()
    for start, end, site in _name_matches(text, sites):
        if any(match.start() <= start <= match.end() for match in labeled):
            continue
        marker = _SITE_MARKER.match(text, end)
        shop, scoped = _left_shop(text, start, shop_mentions, marker)
        sku_start = _SKU_AFTER.match(text, marker.end() if marker else end).end()
        anchored_sku = leading_skus(text[sku_start:], known_skus)
        prefix = text[:start]
        direct_site = _introduced_name(prefix) == () or (
            _CLAUSE_START.search(prefix) and text[end:].lstrip().startswith("的")
        )
        if (marker and (scoped or anchored_sku or _CLAUSE_START.search(prefix))) or (
            anchored_sku and (shop or direct_site)
        ):
            if marker:
                market_ends.add(marker.end())
            yield _choose_coords(catalog, shops, sites[site], shop, site, bound), sku_start

    # A shop/site label can also select a scope without repeating the selected SKU.
    local_market = {scope[:2] for scope in catalog if scope[1] == bound[1]}
    for offer in _OFFER_MARKER.finditer(text):
        sku_start = _SKU_AFTER.match(text, offer.end()).end()
        name = _introduced_name(text[:offer.start()])
        if (name and leading_skus(text[sku_start:], known_skus)
                and not any(offer.start() < end <= sku_start for end in market_ends)):
            yield shops.get(name, set()) & local_market, sku_start

    for start, end, shop in shop_mentions:
        marker = _SHOP_SUFFIX.match(text, end)
        sku_start = _SKU_AFTER.match(text, marker.end() if marker else end).end()
        introduced = _introduced_name(text[:start]) == ()
        if (marker and introduced) or (introduced and leading_skus(text[sku_start:], known_skus)):
            yield shops[shop] & local_market, sku_start

    for match in _SHOP_LABEL.finditer(text):
        if any(qualified.start() <= match.start() < qualified.end() for qualified in labeled):
            continue
        label = next(group for group in match.groups() if group is not None)
        names = [(end, name) for start, end, name in _name_matches(label, shops) if start == 0]
        if not names:
            yield set(), match.end()
        elif not any(re.match(_phrase(site), label[names[0][0]:].lstrip()) for site in sites):
            yield shops[names[0][1]] & local_market, match.end() - len(label) + names[0][0]
    for match in _MARKET_LABEL.finditer(text):
        if not any(qualified.start() <= match.start() < qualified.end() for qualified in labeled):
            site, ids, end = _site_at(text, match.end(), sites)
            yield _choose_coords(catalog, shops, ids, (), site, bound), _SKU_AFTER.match(text, end).end()


def clarification_choices(catalog, bound, query):
    text = normalize(query)
    products = {scope[2]: item for scope, item in catalog.items() if scope[:2] == bound}
    if any(pairs != {bound} for pairs, _ in explicit_coordinates(text, catalog, bound)):
        return []
    matches = sku_mentions(text, products)
    if not matches:
        if LABELED_MODEL.search(text) or sku_mentions(text, {scope[2] for scope in catalog}):
            return []
        names = defaultdict(set)
        for sku, item in products.items():
            for title in (item["title"], *item["product_aliases"]):
                if name := _name(title):
                    names[name].add(sku)
        for _, _, name in _name_matches(text, names):
            matches.update(names[name])
    return [{"sku": sku, "title": products[sku]["title"]} for sku in sorted(matches)]
