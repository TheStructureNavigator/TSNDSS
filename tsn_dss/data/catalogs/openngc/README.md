# TSN DSS OpenNGC Snapshot

This directory contains a transformed TSN DSS snapshot of OpenNGC.

- Upstream: https://github.com/mattiaverga/OpenNGC
- Release: `v20260501`
- Commit: `36cb178a0f69dba8bfc03a99c10512831edf1c6b`
- License: CC-BY-SA-4.0
- Raw source files: `database_files/NGC.csv`, `database_files/addendum.csv`
- Transform: `tsn-dss-openngc-transform-v1`

`objects.json` is generated from the pinned upstream CSV files. `manifest.json`
records the upstream URLs, SHA-256 digests, source row counts, transform version,
generated artifact digest, and inclusion/exclusion counts.

Rebuild with:

```text
python tools/build_openngc_snapshot.py <pinned-openngc-source-root>
```

The runtime import uses the bundled artifact and does not require network access.
