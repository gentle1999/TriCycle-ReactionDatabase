# 256-file real-world MolOP load corpus

This corpus contains 256 distinct Gaussian calculation outputs selected from
the local `complete_set` dataset: 128 product logs and 128 transition-state
logs, with one of each from 128 reaction pairs spread across the sorted source
collection. The source tree is not downloaded by CI. To keep the repository
checkout compact without exceeding common single-file hosting limits, eight
`corpus-NNN.tar.gz` shards store the files in compressed archives; their 256
members are ordinary, uncompressed `.log` text files. CI extracts the shards
before invoking the parser, so gzip decompression is not part of the measured
MolOP work. `manifest.json` pins each raw log's source path, SHA-256, and byte
size, and is included in each shard.

The raw inputs total about 519 MiB. CI parses these 256 files together with the
seven focused extreme-case fixtures through the shared MolOP pipeline. The
benchmark expands any gzip fixtures before starting its timer, so all 263
parser inputs are uncompressed text. The load job uses every CPU visible to the
runner and keeps four times that number of file pipelines eligible, so the
shared process pool remains saturated without launching a separate pool per
upload session.

To regenerate the sample set from a local `complete_set` checkout, choose a new
or empty destination (the builder refuses to overwrite a non-empty directory):

```bash
uv run --frozen python scripts/build_real_world_batch_corpus.py \
  --source-root /path/to/tricycle-data/complete_set \
  --output-dir .tmp/real_world_batch_256 \
  --file-count 256
```

The sample plan can be inspected before writing with `--dry-run`. The builder
creates eight `corpus-NNN.tar.gz` shards containing raw `.log` members and a
matching manifest.
The benchmark validates every raw file hash against the manifest and reports
each file's frame/segment counts, inference outcomes, queue delay, and parse
time.
