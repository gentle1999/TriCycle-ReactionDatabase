"""Canonical text rules for quantum-chemistry protocol identities."""

import re

# MolOP and quantum-chemistry programs use both ``D3BJ`` and ``GD3BJ`` for
# the Grimme D3(BJ) correction. Keep one spelling in the database so the
# protocol hash does not depend on the program-specific alias.
_DISPERSION_MODEL_ALIASES: dict[str, str] = {
    "D3": "GD3",
    "GD3": "GD3",
    "D3BJ": "GD3BJ",
    "GD3BJ": "GD3BJ",
    "D3BJM": "GD3BJM",
    "GD3BJM": "GD3BJM",
    "D3BJABC": "GD3BJABC",
    "GD3BJABC": "GD3BJABC",
    "D3ZERO": "GD3ZERO",
    "GD3ZERO": "GD3ZERO",
    "D3ZEROM": "GD3ZEROM",
    "GD3ZEROM": "GD3ZEROM",
    "D4": "D4",
    "NL": "NL",
    "VV10": "VV10",
}
_DISPERSION_SUFFIX_KEYS = tuple(sorted(_DISPERSION_MODEL_ALIASES, key=len, reverse=True))


def normalize_protocol_text(value: str | None) -> str | None:
    """Return the canonical, case-insensitive spelling of a protocol field."""

    if value is None:
        return None
    normalized = re.sub(r"\s+", "", value).strip()
    return normalized.upper() or None


def _dispersion_lookup_key(value: str) -> str:
    return re.sub(r"[-_()\[\]\s]", "", value).upper()


def _canonical_dispersion_model(value: str | None) -> str | None:
    normalized = normalize_protocol_text(value)
    if normalized is None:
        return None
    return _DISPERSION_MODEL_ALIASES.get(
        _dispersion_lookup_key(normalized),
        normalized,
    )


def _split_functional_dispersion(functional: str) -> tuple[str, str] | None:
    """Return a functional base and canonical suffix when one is explicit."""

    normalized = normalize_protocol_text(functional)
    if normalized is None:
        return None
    # Parentheses are common in spellings such as ``D3(BJ)``. They are
    # punctuation around the suffix, not part of the canonical functional.
    compact = normalized.replace("(", "").replace(")", "")
    upper = compact.upper()
    for suffix_key in _DISPERSION_SUFFIX_KEYS:
        if not upper.endswith(suffix_key):
            continue
        base = compact[: -len(suffix_key)].rstrip("-_/ ")
        if not base:
            continue
        return normalize_protocol_text(base) or "", _DISPERSION_MODEL_ALIASES[suffix_key]
    return None


def normalize_functional_and_dispersion(
    functional: str | None,
    dispersion_model: str | None,
) -> tuple[str | None, str | None]:
    """Normalize functional and independent dispersion fields together.

    Functional names, basis names, and dispersion names are case-insensitive
    protocol tokens. A known dispersion suffix is also made program-neutral
    (for example ``D3BJ`` becomes ``GD3BJ``), and is appended to the
    functional projection when it is supplied as a separate field.
    """

    canonical_model = _canonical_dispersion_model(dispersion_model)
    normalized_functional = normalize_protocol_text(functional)
    if normalized_functional is None:
        return None, canonical_model

    explicit = _split_functional_dispersion(normalized_functional)
    if explicit is not None:
        base, explicit_model = explicit
        if canonical_model is not None and explicit_model != canonical_model:
            raise ValueError(
                "functional dispersion suffix conflicts with dispersion_model: "
                f"functional={functional!r} ({explicit_model}), "
                f"dispersion_model={dispersion_model!r} ({canonical_model})"
            )
        return f"{base}-{explicit_model}", canonical_model

    if canonical_model is not None:
        return f"{normalized_functional}-{canonical_model}", canonical_model
    return normalized_functional, None


__all__ = [
    "normalize_functional_and_dispersion",
    "normalize_protocol_text",
]
