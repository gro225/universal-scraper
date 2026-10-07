from harvester.hashing import (
    canonical_url,
    domain_of,
    hamming,
    registrable_domain,
    simhash,
    url_key,
)


def test_canonical_url_strips_tracking_and_normalizes():
    a = canonical_url("HTTPS://WWW.Example.com:443/path/?utm_source=x&b=2&a=1#frag")
    b = canonical_url("https://example.com/path?a=1&b=2")
    assert a == b == "https://example.com/path?a=1&b=2"


def test_canonical_url_keeps_root_slash():
    assert canonical_url("https://example.com") == "https://example.com/"


def test_url_key_is_stable_across_equivalent_urls():
    assert url_key("https://example.com/x?fbclid=1") == url_key("https://www.example.com/x")
    assert url_key("https://example.com/x") != url_key("https://example.com/y")


def test_domains():
    assert domain_of("https://www.sub.example.co.uk/a") == "sub.example.co.uk"
    assert registrable_domain("https://www.sub.example.co.uk/a") == "example.co.uk"
    assert registrable_domain("https://news.ycombinator.com/") == "ycombinator.com"


def test_simhash_near_duplicates_are_close():
    base = "Sol Lemon released a new album this spring and toured across Europe."
    edited = "Sol Lemon released a new album this spring and toured across Europe today."
    other = "Совершенно другой текст про погоду, огурцы и городской транспорт."

    assert hamming(simhash(base), simhash(edited)) < hamming(simhash(base), simhash(other))


def test_simhash_fits_in_signed_bigint():
    value = simhash("любой текст")
    assert -(2**63) <= value < 2**63
