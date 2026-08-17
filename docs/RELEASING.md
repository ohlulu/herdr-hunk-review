---
summary: Release facts for the tag-ci channel (version source, tag format, sync files, gate)
read_when:
  - Shipping a release of this plugin
  - Bumping the plugin version or editing the changelog
---

# Releasing

Releases follow the user-level `release-flow` skill, channel `tag-ci`.

## Facts

```yaml
app: herdr-hunk-review
channel: tag-ci

gate: python3 -m unittest discover -s tests

version_source:
  file: herdr-plugin.toml
  marketing_key: version

tag:
  format: "v{version}"

changelog_heading: "## [{version}]"

sync_files:
  - herdr-plugin.toml   # version = "X.Y.Z"
  - README.md           # `--ref vX.Y.Z` install example

locales: [en]

notes:
  opener: false
```

## Deviations

- `tag.format` is `v{version}`, not the channel default bare semver: `v0.1.0`
  shipped before this file existed, is pinned by installed clients
  (`herdr plugin install --ref v0.1.0`), and is printed in README. The release
  workflow strips the leading `v` when matching the changelog heading, so
  `## [0.1.0]` pairs with tag `v0.1.0`.

## Related

- [.github/workflows/release.yml](../.github/workflows/release.yml) ← CI that
  publishes the GitHub Release from a pushed tag; changelog section is the body
