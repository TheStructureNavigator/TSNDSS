# TSN DSS Catalog Objects

TSN DSS keeps catalog facts separate from workflow Targets.

- `CatalogObject` is an astronomical object from a stated catalog/source.
- `Target` is something selected or used by the observing workflow.
- Catalog registration is explicit and idempotent; read paths do not mutate the database.
- Alias resolution is deterministic exact lookup after normalization. It does not do fuzzy matching.
- `M42`, `M 42` and `Messier 42` normalize to the same Messier designation key. `NGC1976`, `NGC 1976`, `IC434` and `IC 434` normalize to NGC/IC keys. Ordinary names are case-insensitive with collapsed whitespace.

The schema records source provider, source version, source external identifier and coordinate reference information. Provider-specific raw payloads are not required domain state.

## Bundled OpenNGC snapshot

TSN DSS includes a transformed OpenNGC snapshot as its first bundled deep-sky catalog provider. OpenNGC is not treated as the only possible future catalog source.

- Upstream repository: <https://github.com/mattiaverga/OpenNGC>
- Release: `v20260501`
- Commit: `36cb178a0f69dba8bfc03a99c10512831edf1c6b`
- Source files: `database_files/NGC.csv` and `database_files/addendum.csv`
- License: CC-BY-SA-4.0; the bundled license text is in `tsn_dss/data/catalogs/openngc/LICENSE-CC-BY-SA-4.0.txt`.
- Bundled artifact: `tsn_dss/data/catalogs/openngc/objects.json`
- Manifest and digests: `tsn_dss/data/catalogs/openngc/manifest.json`
- Reproducible transform: `python tools/build_openngc_snapshot.py <pinned-openngc-source-root>`

The snapshot contains OpenNGC records with usable J2000 RA/Dec after excluding `Dup`, `NonEx`, and rows without usable coordinates. It maps only schema-v4 fields: designation, display name, coordinates, type, angular major/minor size, V magnitude, source provider/version, and source external ID. It does not silently substitute another magnitude band for missing V magnitude.

OpenNGC common names and cross-identifiers are exact aliases only when their normalized form has exactly one owner in the imported snapshot. Colliding aliases, such as `Eagle Nebula`, are reported and skipped so exact resolution never silently selects the wrong object.
