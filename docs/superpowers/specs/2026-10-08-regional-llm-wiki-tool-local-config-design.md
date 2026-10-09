# Regional LLM Wiki Tool-Local Configuration Design

## Goal

Keep the Regional LLM Wiki tools operational without registering a new field or
configuration model in DeerFlow's core `AppConfig` schema.

## Configuration boundary

The deployment continues to define `regional_llm_wiki` in the repository-root
`config.yaml`. DeerFlow preserves unknown top-level configuration fields because
`AppConfig` allows extras. Therefore, without a core schema registration,
`get_app_config().regional_llm_wiki` is a plain mapping.

`regional_llm_wiki_tool.py` will own parsing and validation of that mapping. It
will apply the existing defaults, validate booleans, paths, integer types, and
numeric ranges, and expose an immutable tool-private configuration object to the
release-reading code.

The exact tool-local contract is:

- `enabled`: defaults to `false` and must be a boolean;
- `runtime_root`: defaults to `/mnt/regional-llm-wiki-runtime`, is stripped, and
  must remain a POSIX absolute path;
- `max_search_results`: defaults to `5` and must be an integer in `1..10`;
- `max_page_chars`: defaults to `24000` and must be an integer in
  `1000..100000`;
- `max_evidence_chars`: defaults to `16000` and must be an integer in
  `1000..100000`.

Booleans are not accepted as integers. Numeric strings and floats are rejected.
Unknown keys are ignored so unrelated forward-compatible deployment metadata
does not break the reader.

Configuration is parsed every time a release is opened. It is not cached at
module import time, preserving `get_app_config()`'s existing `config.yaml` hot
reload behavior.

## Core-source rollback

Remove the `RegionalLlmWikiConfig` import and `regional_llm_wiki` field from
`app_config.py`. Remove the now-unused `regional_llm_wiki_config.py` module. No
other DeerFlow configuration loader behavior changes.

Keep the `regional_llm_wiki` section in `config.example.yaml` as documentation
for this bundled tool. This boundary prohibits a core schema/model dependency;
it does not prohibit a documented tool-specific top-level extension key.

## Error behavior

A single tool-local loader accepts only a `Mapping`. A missing attribute, null
value, wrong top-level shape, missing required runtime state, or any field
validation failure is converted to `_ReleaseError("release_unavailable")` before
field access. Missing, disabled, or malformed tool configuration therefore
produces the existing safe `release_unavailable` tool response. It must not
raise `AttributeError`, `TypeError`, `KeyError`, or validation exceptions, leak
filesystem paths, or expose configuration internals to the model.

## Tests

Extend the Regional LLM Wiki tool tests to cover:

- valid mapping-based configuration;
- missing, null, wrong-shaped, and disabled configuration;
- malformed types and out-of-range limits;
- all four public tool paths return safe status payloads for bad configuration;
- changing the mapping between calls is observed without a process restart;
- preservation of existing manifest, path, hash, and release safety behavior.

Run the targeted Wiki tool tests and configuration tests after implementation.
