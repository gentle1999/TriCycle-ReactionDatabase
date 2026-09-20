# Real-world ingestion extreme fixtures

These fixtures are a compact, reproducible regression set of real Gaussian
outputs retained from previously imported scientific datasets. The log files
are gzip-compressed byte-for-byte copies of their source logs; the endpoint SDF
files contain signed TS endpoint molecules extracted from the indicated frame.
`manifest.json` pins each expanded source hash, size, frame/segment count, and
known reaction-inference count. The performance command validates those
contracts before reporting measurements.

| Fixture | Original size | Source SHA-256 | Regression covered |
| --- | ---: | --- | --- |
| `1-s2.0-S2451929422005617-mmc2__23.log.gz` | 2,886,805 bytes | `acd78c7d9ac27f08a17547c0373cf6fcd144ca6222331a35d2baa8b96b216d43` | 31-frame segmented Gaussian; TS inference |
| `1-s2.0-S2451929422005617-mmc2__24.log.gz` | 6,422,978 bytes | `f2c30cf0e1e69ae5b533ac82a7e0032ba736b8e45a90e6205a14961680c43c97` | Segmented Gaussian parse; frame 76 endpoints used for stereo-DAG persistence; TS inference |
| `1-s2.0-S2451929422005617-mmc2__25.log.gz` | 14,037,619 bytes | `fcda355e5ce1177f9a70dee8f0f1f67a015249f29f71a92e5117713d602c800d` | 156-frame large segmented Gaussian; TS inference |
| `1-s2.0-S2451929422005617-mmc2__26.log.gz` | 19,620,917 bytes | `6fbab2f871041d6cd27508995718c61261bd6e3f20f6487807062752c9c26401` | 219-frame / two-segment large-file parse; 102-atom graph-match guard; SMILES stereo round trip |
| `ajoc_201402070_sm_miscellaneous_information__44.log.gz` | 3,628,849 bytes | `3112ba908f9ecc0f1997ec23cd12df6ead03dbe0987268c4c35dedd60bda648f` | Independent Gaussian source; 104 frames and TS inference |
| `anie202216373-sup-0001-misc_information__6.log.gz` | 10,352,290 bytes | `a5cc887251af02328139e36b315374456ccc2673c0231a59035f9096b1bd0fc7` | Independent 128-frame large Gaussian; TS inference |
| `anie202217654-sup-0001-misc_information__15_modts.log.gz` | 521,713 bytes | `e704d6a077f1f6162d307301de3dae0a749ea04f114894ba00f074ca5d0ec4c7` | Four segments and two TS inferences in one source |
| `1-s2.0-S2451929422005617-mmc2__24.endpoints.sdf` | — | `653887dae41050609a76cf6566f5b433600bab93f733327b768de240132aa61e` | Real 78-atom endpoint structures from frame 76 |
| `1-s2.0-S2451929422005617-mmc2__26.endpoints.sdf` | — | `c677085456198d0b792c3618a6387e849f43b2cc1452ef260ae71bb07d7308ca` | Real 102-atom signed endpoint structures from frame 218 |

The suite exercises the actual shared parser/frame worker pipeline across all
seven logs, including the 19.6 MiB `__26` timeout regression and the multi-TS
`__15_modts` file. The RustFS integration test persists the real `__24`/`__26`
cases through the upload worker and checks profile refresh ordering. Source logs
remain compressed to keep checkout size reasonable; parsing uses the same
file-pipeline path as production.
