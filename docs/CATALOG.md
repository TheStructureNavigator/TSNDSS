# TSN DSS Catalog Objects

TSN DSS keeps catalog facts separate from workflow Targets.

- `CatalogObject` is an astronomical object from a stated catalog/source.
- `Target` is something selected or used by the observing workflow.
- Catalog registration is explicit and idempotent; read paths do not mutate the database.
- Alias resolution is deterministic exact lookup after normalization. It does not do fuzzy matching.
- `M42`, `M 42` and `Messier 42` normalize to the same Messier designation key. `NGC 1976` normalizes to an NGC key. Ordinary names are case-insensitive with collapsed whitespace.

The schema records source provider, source version, source external identifier and coordinate reference information. Provider-specific raw payloads are not required domain state.

This repository does not yet include a verified full Messier catalog source. Do not populate bundled Messier data from memory or arbitrary web pages; add a versioned source with clear provenance before registering Messier objects in production data.
