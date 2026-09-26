from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..domain.models import CatalogObject, CatalogObjectAlias
from .catalog import CatalogRegistrationItem, CatalogRegistrationReport, normalize_catalog_alias
from .sqlite.catalog import CatalogRepository

OPENNGC_PROVIDER = "OpenNGC"
OPENNGC_RELEASE_TAG = "v20260501"
OPENNGC_COMMIT_SHA = "36cb178a0f69dba8bfc03a99c10512831edf1c6b"
OPENNGC_SOURCE_VERSION = f"{OPENNGC_RELEASE_TAG}+{OPENNGC_COMMIT_SHA}"
OPENNGC_REPOSITORY = "https://github.com/mattiaverga/OpenNGC"
OPENNGC_LICENSE = "CC-BY-SA-4.0"
OPENNGC_TRANSFORM_VERSION = "tsn-dss-openngc-transform-v1"
OPENNGC_COORDINATE_FRAME = "OpenNGC J2000 equatorial"
OPENNGC_COORDINATE_EPOCH = "J2000"

OPENNGC_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "catalogs" / "openngc"
OPENNGC_RECORDS_PATH = OPENNGC_SNAPSHOT_DIR / "objects.json"
OPENNGC_MANIFEST_PATH = OPENNGC_SNAPSHOT_DIR / "manifest.json"

REQUIRED_COLUMNS = (
    "Name",
    "Type",
    "RA",
    "Dec",
    "MajAx",
    "MinAx",
    "V-Mag",
    "M",
    "NGC",
    "IC",
    "Identifiers",
    "Common names",
)

EXPECTED_SOURCE_FILES = {
    "database_files/NGC.csv": {
        "rows": 13969,
        "sha256": "840fe0c9ee1332e551b2e722a0e92726cd7b157914a3d2177602832aadd3aa9e",
        "url": (
            "https://raw.githubusercontent.com/mattiaverga/OpenNGC/"
            f"{OPENNGC_COMMIT_SHA}/database_files/NGC.csv"
        ),
    },
    "database_files/addendum.csv": {
        "rows": 64,
        "sha256": "1d8f0914e643ada325a5a94d88d8fefad6a4937a2f77cc34f21483af22b11983",
        "url": (
            "https://raw.githubusercontent.com/mattiaverga/OpenNGC/"
            f"{OPENNGC_COMMIT_SHA}/database_files/addendum.csv"
        ),
    },
}

OPENNGC_TYPE_MAP = {
    "*": "star",
    "**": "double_star",
    "*Ass": "stellar_association",
    "OCl": "open_cluster",
    "GCl": "globular_cluster",
    "Cl+N": "cluster_nebula",
    "G": "galaxy",
    "GPair": "galaxy_pair",
    "GTrpl": "galaxy_triplet",
    "GGroup": "galaxy_group",
    "PN": "planetary_nebula",
    "HII": "hii_region",
    "DrkN": "dark_nebula",
    "EmN": "emission_nebula",
    "Neb": "nebula",
    "RfN": "reflection_nebula",
    "SNR": "supernova_remnant",
    "Nova": "nova",
    "Other": "other",
}

_PRIMARY_NAME = re.compile(r"^(NGC|IC)0*([1-9][0-9]*)([A-Z]?)$")
_MESSIER_VALUE = re.compile(r"^0*([1-9][0-9]*)$")
_AUTHORITATIVE_ALIAS_KINDS = frozenset(
    {"canonical_designation", "openngc_name", "primary_designation", "messier_designation"}
)
_ALIAS_KIND_PRIORITY = {
    "canonical_designation": 0,
    "openngc_name": 1,
    "primary_designation": 2,
    "messier_designation": 3,
}


@dataclass(frozen=True, slots=True)
class OpenNgcSourceRow:
    source_path: str
    row_number: int
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class OpenNgcRecord:
    source_path: str
    row_number: int
    name: str
    type_code: str
    messier: int | None
    ngc: int | None
    ic: int | None
    common_names: tuple[str, ...]
    identifiers: tuple[str, ...]
    canonical_designation: str
    display_name: str
    ra_deg: float
    dec_deg: float
    object_type: str
    angular_major_arcmin: float | None
    angular_minor_arcmin: float | None
    magnitude: float | None

    @property
    def catalog_object_id(self) -> str:
        return openngc_catalog_object_id(self.name)


@dataclass(frozen=True, slots=True)
class OpenNgcAliasCollision:
    normalized_alias: str
    aliases: tuple[str, ...]
    object_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OpenNgcRegistrationPlan:
    items: tuple[CatalogRegistrationItem, ...]
    total_source_rows: int
    exclusions_by_reason: dict[str, int]
    alias_collisions: tuple[OpenNgcAliasCollision, ...]
    exact_aliases: int
    messier_objects: int
    objects_with_angular_dimensions: int
    objects_with_v_magnitude: int
    object_type_counts: dict[str, int]


def parse_ra_to_degrees(value: str) -> float:
    parts = _parse_sexagesimal(value, expected=3, label="RA")
    hours, minutes, seconds = parts
    if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60):
        raise ValueError(f"Invalid OpenNGC RA: {value!r}")
    return round((hours + minutes / 60 + seconds / 3600) * 15, 8)


def parse_dec_to_degrees(value: str) -> float:
    text = value.strip()
    sign = -1 if text.startswith("-") else 1
    if text.startswith(("+", "-")):
        text = text[1:]
    degrees, minutes, seconds = _parse_sexagesimal(text, expected=3, label="Dec")
    if not (0 <= degrees <= 90 and 0 <= minutes < 60 and 0 <= seconds < 60):
        raise ValueError(f"Invalid OpenNGC Dec: {value!r}")
    if degrees == 90 and (minutes != 0 or seconds != 0):
        raise ValueError(f"Invalid OpenNGC Dec: {value!r}")
    return round(sign * (degrees + minutes / 60 + seconds / 3600), 8)


def openngc_catalog_object_id(name: str) -> str:
    stable = name.strip().casefold().replace(" ", "")
    if not stable:
        raise ValueError("OpenNGC Name is required for catalog identity.")
    return f"catalog-object:openngc:{stable}"


def load_bundled_openngc_records() -> tuple[OpenNgcRecord, ...]:
    manifest = _read_json(OPENNGC_MANIFEST_PATH)
    _validate_manifest(manifest)
    artifact_sha = _sha256(OPENNGC_RECORDS_PATH)
    if artifact_sha != manifest["generated_artifact"]["sha256"]:
        raise ValueError(
            "Bundled OpenNGC artifact digest mismatch: "
            f"expected {manifest['generated_artifact']['sha256']}, found {artifact_sha}"
        )
    payload = _read_json(OPENNGC_RECORDS_PATH)
    if payload["metadata"]["source_version"] != OPENNGC_SOURCE_VERSION:
        raise ValueError("Bundled OpenNGC artifact source version mismatch.")
    return tuple(_record_from_json(row) for row in payload["records"])


def build_openngc_records_from_sources(source_root: Path) -> tuple[OpenNgcRecord, ...]:
    rows = _load_source_rows(source_root)
    records: list[OpenNgcRecord] = []
    for row in rows:
        record = _record_from_source_row(row)
        if record is not None:
            records.append(record)
    return tuple(records)


def write_openngc_snapshot(source_root: Path, output_dir: Path) -> dict:
    rows = _load_source_rows(source_root)
    records: list[OpenNgcRecord] = []
    exclusions: Counter[str] = Counter()
    for row in rows:
        record = _record_from_source_row(row)
        if record is None:
            exclusions[_exclusion_reason(row)] += 1
        else:
            records.append(record)

    payload = {
        "metadata": _snapshot_metadata(total_source_rows=len(rows), exclusions_by_reason=dict(sorted(exclusions.items()))),
        "records": [_record_to_json(record) for record in records],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / "objects.json"
    records_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        **payload["metadata"],
        "generated_artifact": {
            "path": "tsn_dss/data/catalogs/openngc/objects.json",
            "sha256": _sha256(records_path),
            "records": len(records),
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def build_openngc_registration_plan(records: Iterable[OpenNgcRecord] | None = None) -> OpenNgcRegistrationPlan:
    materialized = tuple(records) if records is not None else load_bundled_openngc_records()
    candidates: dict[str, list[tuple[OpenNgcRecord, str, str]]] = defaultdict(list)
    for record in materialized:
        for alias, kind in _candidate_aliases(record):
            normalized = normalize_catalog_alias(alias)
            if normalized:
                candidates[normalized].append((record, alias, kind))

    safe_aliases: dict[str, list[CatalogObjectAlias]] = defaultdict(list)
    collisions: list[OpenNgcAliasCollision] = []
    for normalized, claims in sorted(candidates.items()):
        object_ids = sorted({claim[0].catalog_object_id for claim in claims})
        aliases = tuple(sorted({claim[1] for claim in claims}, key=lambda item: (item.casefold(), item)))
        if len(object_ids) > 1:
            collisions.append(OpenNgcAliasCollision(normalized, aliases, tuple(object_ids)))
            authoritative = [claim for claim in claims if claim[2] in _AUTHORITATIVE_ALIAS_KINDS]
            authoritative_object_ids = {claim[0].catalog_object_id for claim in authoritative}
            if len(authoritative_object_ids) == 1:
                record, alias, kind = sorted(authoritative, key=lambda claim: _ALIAS_KIND_PRIORITY[claim[2]])[0]
                safe_aliases[record.catalog_object_id].append(
                    CatalogObjectAlias(record.catalog_object_id, alias, normalized, kind)
                )
            continue
        record, alias, kind = claims[0]
        safe_aliases[record.catalog_object_id].append(
            CatalogObjectAlias(record.catalog_object_id, alias, normalized, kind)
        )

    items = tuple(
        CatalogRegistrationItem(_catalog_object(record), safe_aliases[record.catalog_object_id])
        for record in materialized
    )
    type_counts = Counter(record.object_type for record in materialized)
    return OpenNgcRegistrationPlan(
        items=items,
        total_source_rows=sum(info["rows"] for info in _source_files_metadata().values()),
        exclusions_by_reason=_bundled_exclusions(),
        alias_collisions=tuple(collisions),
        exact_aliases=sum(len(item.aliases) for item in items),
        messier_objects=sum(1 for record in materialized if record.messier is not None),
        objects_with_angular_dimensions=sum(
            1 for record in materialized if record.angular_major_arcmin is not None or record.angular_minor_arcmin is not None
        ),
        objects_with_v_magnitude=sum(1 for record in materialized if record.magnitude is not None),
        object_type_counts=dict(sorted(type_counts.items())),
    )


def register_bundled_openngc_catalog(repository: CatalogRepository) -> tuple[CatalogRegistrationReport, OpenNgcRegistrationPlan]:
    plan = build_openngc_registration_plan()
    report = repository.register_catalog_objects(list(plan.items))
    return report, plan


def _load_source_rows(source_root: Path) -> tuple[OpenNgcSourceRow, ...]:
    rows: list[OpenNgcSourceRow] = []
    for source_path, expected in EXPECTED_SOURCE_FILES.items():
        path = source_root / source_path
        if not path.exists():
            raise FileNotFoundError(f"Missing pinned OpenNGC source file: {path}")
        actual_sha = _sha256(path)
        if actual_sha != expected["sha256"]:
            raise ValueError(f"OpenNGC source digest mismatch for {source_path}: {actual_sha}")
        text = path.read_text(encoding="utf-8-sig")
        reader = csv.DictReader(text.splitlines(), delimiter=";")
        if reader.fieldnames is None:
            raise ValueError(f"OpenNGC source has no header: {source_path}")
        missing = [column for column in REQUIRED_COLUMNS if column not in reader.fieldnames]
        if missing:
            raise ValueError(f"OpenNGC source {source_path} is missing required columns: {', '.join(missing)}")
        parsed = [OpenNgcSourceRow(source_path, index, _clean_row(row)) for index, row in enumerate(reader, start=2)]
        if len(parsed) != expected["rows"]:
            raise ValueError(f"OpenNGC source row count mismatch for {source_path}: {len(parsed)}")
        rows.extend(parsed)
    return tuple(rows)


def _record_from_source_row(row: OpenNgcSourceRow) -> OpenNgcRecord | None:
    reason = _exclusion_reason(row)
    if reason:
        return None
    values = row.values
    type_code = values["Type"]
    try:
        object_type = OPENNGC_TYPE_MAP[type_code]
    except KeyError:
        raise ValueError(f"Unknown OpenNGC Type {type_code!r} at {row.source_path}:{row.row_number}") from None

    return OpenNgcRecord(
        source_path=row.source_path,
        row_number=row.row_number,
        name=values["Name"],
        type_code=type_code,
        messier=_parse_optional_number(values["M"]),
        ngc=_parse_optional_number(values["NGC"]) or _primary_catalog_number(values["Name"], "NGC"),
        ic=_parse_optional_number(values["IC"]) or _primary_catalog_number(values["Name"], "IC"),
        common_names=_split_multi_value(values["Common names"]),
        identifiers=_split_multi_value(values["Identifiers"]),
        canonical_designation=_canonical_designation(values),
        display_name=_display_name(values),
        ra_deg=parse_ra_to_degrees(values["RA"]),
        dec_deg=parse_dec_to_degrees(values["Dec"]),
        object_type=object_type,
        angular_major_arcmin=_parse_optional_float(values["MajAx"]),
        angular_minor_arcmin=_parse_optional_float(values["MinAx"]),
        magnitude=_parse_optional_float(values["V-Mag"]),
    )


def _exclusion_reason(row: OpenNgcSourceRow) -> str:
    values = row.values
    if values["Type"] == "Dup":
        return "duplicate"
    if values["Type"] == "NonEx":
        return "nonexistent"
    if not values["RA"] or not values["Dec"]:
        return "missing_coordinates"
    return ""


def _canonical_designation(values: dict[str, str]) -> str:
    messier = _parse_optional_number(values["M"])
    if messier is not None:
        return f"M{messier}"
    primary = _format_primary_designation(values["Name"])
    if primary is not None:
        return primary
    return _stable_addendum_designation(values["Name"])


def _display_name(values: dict[str, str]) -> str:
    names = _split_multi_value(values["Common names"])
    return names[0] if names else _canonical_designation(values)


def _candidate_aliases(record: OpenNgcRecord) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = [
        (record.canonical_designation, "canonical_designation"),
        (record.name, "openngc_name"),
    ]
    primary = _format_primary_designation(record.name)
    if primary is not None:
        aliases.append((primary, "primary_designation"))
    if record.messier is not None:
        aliases.extend(
            [
                (f"M{record.messier}", "messier_designation"),
                (f"M {record.messier}", "messier_designation"),
                (f"Messier {record.messier}", "messier_designation"),
            ]
        )
    if record.ngc is not None:
        aliases.append((f"NGC {record.ngc}", "cross_designation"))
    if record.ic is not None:
        aliases.append((f"IC {record.ic}", "cross_designation"))
    aliases.extend((name, "common_name") for name in record.common_names)
    aliases.extend((identifier, "identifier") for identifier in record.identifiers)
    return _dedupe_aliases(aliases)


def _catalog_object(record: OpenNgcRecord) -> CatalogObject:
    return CatalogObject(
        id=record.catalog_object_id,
        canonical_designation=record.canonical_designation,
        display_name=record.display_name,
        ra_deg=record.ra_deg,
        dec_deg=record.dec_deg,
        object_type=record.object_type,
        coordinate_frame=OPENNGC_COORDINATE_FRAME,
        coordinate_epoch=OPENNGC_COORDINATE_EPOCH,
        angular_major_arcmin=record.angular_major_arcmin,
        angular_minor_arcmin=record.angular_minor_arcmin,
        magnitude=record.magnitude,
        source_provider=OPENNGC_PROVIDER,
        source_version=OPENNGC_SOURCE_VERSION,
        source_external_id=record.name,
    )


def _source_files_metadata() -> dict:
    return {
        path: {
            "rows": expected["rows"],
            "sha256": expected["sha256"],
            "url": expected["url"],
        }
        for path, expected in EXPECTED_SOURCE_FILES.items()
    }


def _snapshot_metadata(*, total_source_rows: int, exclusions_by_reason: dict[str, int]) -> dict:
    return {
        "provider": OPENNGC_PROVIDER,
        "repository": OPENNGC_REPOSITORY,
        "release_tag": OPENNGC_RELEASE_TAG,
        "commit_sha": OPENNGC_COMMIT_SHA,
        "source_version": OPENNGC_SOURCE_VERSION,
        "license": OPENNGC_LICENSE,
        "transform_version": OPENNGC_TRANSFORM_VERSION,
        "source_files": _source_files_metadata(),
        "total_source_rows": total_source_rows,
        "exclusions_by_reason": exclusions_by_reason,
        "coordinate_frame": OPENNGC_COORDINATE_FRAME,
        "coordinate_epoch": OPENNGC_COORDINATE_EPOCH,
    }


def _validate_manifest(manifest: dict) -> None:
    expected = _snapshot_metadata(
        total_source_rows=sum(info["rows"] for info in EXPECTED_SOURCE_FILES.values()),
        exclusions_by_reason=manifest.get("exclusions_by_reason", {}),
    )
    for key in ("provider", "repository", "release_tag", "commit_sha", "source_version", "license", "transform_version"):
        if manifest.get(key) != expected[key]:
            raise ValueError(f"OpenNGC manifest {key} mismatch.")
    for key in ("total_source_rows", "coordinate_frame", "coordinate_epoch"):
        if manifest.get(key) != expected[key]:
            raise ValueError(f"OpenNGC manifest {key} mismatch.")
    if manifest.get("source_files") != expected["source_files"]:
        raise ValueError("OpenNGC manifest source file metadata mismatch.")


def _bundled_exclusions() -> dict[str, int]:
    manifest = _read_json(OPENNGC_MANIFEST_PATH)
    return dict(sorted((manifest.get("exclusions_by_reason") or {}).items()))


def _record_to_json(record: OpenNgcRecord) -> dict:
    return {
        "source_path": record.source_path,
        "row_number": record.row_number,
        "name": record.name,
        "type_code": record.type_code,
        "messier": record.messier,
        "ngc": record.ngc,
        "ic": record.ic,
        "common_names": list(record.common_names),
        "identifiers": list(record.identifiers),
        "canonical_designation": record.canonical_designation,
        "display_name": record.display_name,
        "ra_deg": record.ra_deg,
        "dec_deg": record.dec_deg,
        "object_type": record.object_type,
        "angular_major_arcmin": record.angular_major_arcmin,
        "angular_minor_arcmin": record.angular_minor_arcmin,
        "magnitude": record.magnitude,
    }


def _record_from_json(row: dict) -> OpenNgcRecord:
    return OpenNgcRecord(
        source_path=row["source_path"],
        row_number=int(row["row_number"]),
        name=row["name"],
        type_code=row["type_code"],
        messier=row["messier"],
        ngc=row["ngc"],
        ic=row["ic"],
        common_names=tuple(row["common_names"]),
        identifiers=tuple(row["identifiers"]),
        canonical_designation=row["canonical_designation"],
        display_name=row["display_name"],
        ra_deg=float(row["ra_deg"]),
        dec_deg=float(row["dec_deg"]),
        object_type=row["object_type"],
        angular_major_arcmin=row["angular_major_arcmin"],
        angular_minor_arcmin=row["angular_minor_arcmin"],
        magnitude=row["magnitude"],
    )


def _format_primary_designation(name: str) -> str | None:
    match = _PRIMARY_NAME.match(name.strip())
    if not match:
        return None
    prefix, number, suffix = match.groups()
    return f"{prefix} {int(number)}{suffix}"


def _primary_catalog_number(name: str, prefix: str) -> int | None:
    match = _PRIMARY_NAME.match(name.strip())
    if not match or match.group(1) != prefix or match.group(3):
        return None
    return int(match.group(2))


def _stable_addendum_designation(name: str) -> str:
    messier = normalize_catalog_alias(name)
    if messier.startswith("messier:"):
        return f"M{messier.split(':', 1)[1]}"
    return name.strip()


def _parse_optional_number(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    match = _MESSIER_VALUE.match(text)
    if not match:
        return None
    return int(match.group(1))


def _parse_optional_float(value: str) -> float | None:
    text = value.strip()
    return float(text) if text else None


def _split_multi_value(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _dedupe_aliases(aliases: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for alias, kind in aliases:
        normalized = normalize_catalog_alias(alias)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append((alias, kind))
    return deduped


def _parse_sexagesimal(value: str, *, expected: int, label: str) -> tuple[float, ...]:
    parts = value.strip().split(":")
    if len(parts) != expected:
        raise ValueError(f"Invalid OpenNGC {label}: {value!r}")
    return tuple(float(part) for part in parts)


def _clean_row(row: dict[str, str | None]) -> dict[str, str]:
    return {key: (value or "").strip() for key, value in row.items()}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
