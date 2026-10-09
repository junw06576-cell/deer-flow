# Regional LLM Wiki schema-1 compatibility mount

This is an opt-in deployment overlay for the existing `regional-llm-wiki`
Agent on the accepted `main` baseline. It does not modify DeerFlow backend or
frontend source, the old `/mnt/knowledge` mount, or the sandbox mounts.

## Inputs and gate

The independent Wiki compiler must first publish and validate a schema-1
projection at `runtime/agent-access-compat-v1/`. This is a separate snapshot;
do not mount the canonical schema-2 `runtime/agent-access-runtime/` here.
The previously exposed model API key must be rotated before any DeerFlow mount;
the old key is permitted only in the isolated shadow run. Do not place a key
or model configuration in this Compose overlay.
The compat root must contain `current -> releases/<release_id>` and the current
manifest must have `schema_version: 1` and the same `release_id` as its release
directory name. The full offline validator must pass; `get_llm_wiki_release_state`
alone checks only the manifest's basic compatibility.

As `wikiuser`, run the compiler's local export command after deploying the
merged LLM Wiki compiler. It validates the pinned schema-2 source and the
schema-1 output; an unchanged valid export is reported as `reused: true`:

```bash
python3 /home/wikiuser/regional_llm_wiki-shadow/compiler/llm_wiki.py \
  export-agent-compat-v1 --root /home/wikiuser/regional_llm_wiki-shadow
```

Record its `release_id`, `source_agent_release_id`, page/evidence counts and
the source commit from `current/agent-access-manifest.json`. A failed export
or a non-`ok` `agent_compat_v1` maintenance status blocks mounting. The
compiler root shown above is the current shadow location; if the administrator
uses a different isolated root, substitute that exact path consistently.

The exporter currently creates private `0700` directories and `0600` files.
A read-only bind mount does not grant Unix read permission. Before restarting
Gateway, the administrator must inspect its effective UID/GID and provide
read-only access to this compatibility root only (for example a dedicated
reader group or host ACL). Do not loosen the canonical schema-2 export or give
the sandbox access. Check that Gateway can traverse `current` and read both a
page and an evidence shard inside the container.

## Compose activation

Set `REGIONAL_LLM_WIKI_COMPAT_ROOT` in the administrator's local deployment
environment to the absolute host path of `runtime/agent-access-compat-v1`.
It is a path, not a secret. Keep runtime `config.yaml` set to:

```yaml
regional_llm_wiki:
  enabled: true
  runtime_root: /mnt/regional-llm-wiki-runtime
```

Append `-f docker/docker-compose.regional-llm-wiki.yaml` after the normal
Compose file(s), including any existing DooD overlay if applicable. Check the
merged Compose configuration without printing secrets, then recreate **only**
Gateway. Do not add this overlay to the general sandbox or change the existing
production Markdown sync service.

In the merged Compose model, confirm that Gateway still has the old
`/mnt/knowledge` mount and additionally has exactly one read-only
`/mnt/regional-llm-wiki-runtime` mount; no sandbox or provisioner service may
receive the new mount. Compose merges service volumes by container target, so
the distinct target is intentional. Run `docker compose ... config` with the
same ordered `-f` files that deployment will use, and inspect only the volume
targets/read-only flags rather than printing the full configuration into logs.

The current `scripts/deploy.sh` does **not** auto-load this optional overlay.
An administrator using that script must explicitly incorporate the overlay in
their deployment command or approved wrapper; setting the path variable alone
does not activate the mount. The overlay uses `create_host_path: false`, so a
missing compatibility export is an error rather than an empty host directory.

## Acceptance and rollback

1. In Gateway, `current/agent-access-manifest.json` is readable and matches
   the expected immutable schema-1 release ID and source commit.
2. `get_llm_wiki_release_state`, `search_llm_wiki`, `read_llm_wiki_page`, and
   `read_llm_wiki_evidence` return `ok`. Include a long document's last shard.
3. The dedicated Agent exposes only its approved tools; an old Agent still
   behaves as before, including one regression question against its existing
   `/mnt/knowledge` source. The sandbox cannot read the compat mount.
4. After a publish switch, an in-flight answer can still read the prior
   immutable release. Retain at least one previous release.

If any gate fails, do not enable the new Agent. Remove the opt-in Compose
overlay and recreate Gateway only; the canonical Wiki and old agents stay
untouched. If the compiler's compat export fails, its `current` remains at the
previous validated release and maintenance status records the failure.

The mount is isolated by service and the Agent's tool allowlist, not by a
separate Gateway process for each Agent. Gateway recreation may briefly
interrupt in-flight requests; schedule the cutover accordingly.
